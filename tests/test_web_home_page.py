"""GET /home — state-aware landing page.

The boolean ``users.onboarded`` drives template selection. No
auto-transition: the not-onboarded view stays put until the user reloads
(the brainstorm called this out explicitly — quiet UI is preferable to a
surprise redirect mid-setup).

See origin: docs/brainstorms/home-page-requirements.md.
"""

from __future__ import annotations

import tempfile
import uuid

import pytest


@pytest.fixture
def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        yield tmp


def _make_user_and_session(conn, email="u@example.com", onboarded=False):
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token

    uid = str(uuid.uuid4())
    UserRepository(conn).create(id=uid, email=email, name=email.split("@")[0])
    if onboarded:
        conn.execute("UPDATE users SET onboarded = TRUE WHERE id = ?", [uid])
    return uid, create_access_token(user_id=uid, email=email)


def _client(follow_redirects: bool = True):
    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app, follow_redirects=follow_redirects)


def test_home_unauth_redirects_to_login(fresh_db):
    """Non-API HTML routes redirect 401→/login per app.main's
    StarletteHTTPException handler. /home follows that contract."""
    c = _client(follow_redirects=False)
    resp = c.get("/home")
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/login")


def test_home_not_onboarded_user_sees_setup_view(fresh_db):
    """A FALSE-onboarded user gets the install/setup template, identifiable
    by its 'Install Claude Code' heading and the self-mark button."""
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn, onboarded=False)
    finally:
        conn.close()
        close_system_db()

    c = _client()
    resp = c.get("/home", cookies={"access_token": sess})
    assert resp.status_code == 200
    body = resp.text
    assert "install Claude Code" in body  # step 1 label
    assert "install Agnes" in body  # step 2 label
    assert "self-mark-btn" in body  # self-acknowledged escape hatch
    assert "setupClaudeBtn" in body  # primary one-click CTA from shared partial


def test_home_onboarded_user_sees_nav_hub(fresh_db):
    """A TRUE-onboarded user gets the post-onboarding view, identifiable by
    the 'Welcome back' hero, the 'Step 1 & Step 2 done' completion badge,
    the offboard control, and the absence of the inline Step 1 / Step 2
    install commands. Step 3 (auto-mode), connectors, and the rest stay
    visible — they remain useful after onboarding."""
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn, onboarded=True)
    finally:
        conn.close()
        close_system_db()

    c = _client()
    resp = c.get("/home", cookies={"access_token": sess})
    assert resp.status_code == 200
    body = resp.text
    assert "Welcome back" in body
    # Banner copy updated when auto-mode moved into install-hero as a real
    # Step 2 (between Claude install and Agnes install). The completion
    # badge now names all three.
    assert "Step 1, 2 &amp; 3 done" in body  # completion badge
    assert "Mark me as offboarded" in body  # offboard control visible
    # All three inline install-blocks are hidden post-onboarding — the
    # labels rendered inside the install-block divs go away.
    assert "Step 1 — install Claude Code" not in body
    assert "Step 2 — turn on auto-mode" not in body
    assert "Step 3 — install Agnes from inside Claude Code" not in body


def test_connectors_render_flat_when_onboarded_by_default(fresh_db):
    """Connect-your-tools section must NOT auto-collapse on the
    server-side `users.onboarded=TRUE` flip. It renders flat (in <details
    open>) by default; only an explicit user click on the in-hero
    "Minimize setup view" toggle (persisted in localStorage, not server)
    activates the collapsed bar layout.

    Auto-mode used to be a peer `setup-collapsible` section
    (`data-section="step3"`) outside the install-hero. It moved into the
    install-hero as Step 2 of the install flow (so users enable it
    BEFORE Step 3's ~20-command install runs), and the standalone
    outside-hero copy was dropped to avoid duplicating reference
    content. Onboarded users no longer see the auto-mode block at all —
    consistent with Step 1 + Step 3 also hiding post-onboarding."""
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn, onboarded=True)
    finally:
        conn.close()
        close_system_db()

    c = _client()
    resp = c.get("/home", cookies={"access_token": sess})
    assert resp.status_code == 200
    body = resp.text
    # Auto-mode no longer renders for onboarded users — both the
    # in-hero install-block and the legacy outside-hero `<details>`
    # reference card are gated `{% if not onboarded %}` / removed.
    assert 'class="automode-card"' not in body
    assert 'data-section="step3"' not in body
    assert "Step 2 — turn on auto-mode" not in body
    # Connect-your-tools section is still flat-open by default.
    assert 'class="connector-tiles"' in body
    assert 'class="setup-collapsible" data-section="connectors" open' in body
    # Server-rendered HTML never carries the data-setup-minimized
    # attribute on the .home-mock root — that's a client-side
    # localStorage decision applied via JS on load. The token still
    # appears in inline CSS selectors and the JS body, which is fine.
    assert '<div class="home-mock" data-setup-minimized' not in body
    assert 'class="home-mock"\n' in body or '<div class="home-mock">' in body


def test_minimize_toggle_visible_only_when_onboarded(fresh_db):
    """The "Minimize setup view" toggle markup is rendered for onboarded
    users (so they can opt into the collapsed view) and absent for
    not-onboarded users (where the install steps already dominate)."""
    from src.db import get_system_db, close_system_db

    # Not-onboarded → no toggle button.
    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn, onboarded=False)
    finally:
        conn.close()
        close_system_db()
    c = _client()
    resp = c.get("/home", cookies={"access_token": sess})
    assert resp.status_code == 200
    assert '<button id="setupMinimizeToggle"' not in resp.text
    assert 'class="setup-minimize"' not in resp.text

    # Onboarded → toggle button rendered inside the install-hero.
    conn = get_system_db()
    try:
        _, sess2 = _make_user_and_session(conn, email="b@example.com", onboarded=True)
    finally:
        conn.close()
        close_system_db()
    c2 = _client()
    resp2 = c2.get("/home", cookies={"access_token": sess2})
    assert resp2.status_code == 200
    assert '<button id="setupMinimizeToggle"' in resp2.text
    assert 'class="setup-minimize"' in resp2.text


def test_home_no_auto_transition_after_post_until_reload(fresh_db):
    """POST /api/me/onboarded flips the flag in the DB but the in-flight
    /home response from before the POST keeps showing the setup view —
    the next GET /home picks up the new state. Verifies the manual-reload
    contract called out in the brainstorm."""
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn, onboarded=False)
    finally:
        conn.close()
        close_system_db()

    c = _client()

    pre = c.get("/home", cookies={"access_token": sess})
    # `class="install-block"` is the not-onboarded-only structural element
    # holding the inline Step-1 install pane. Use it as the discriminator
    # instead of a free-form string like "install Claude Code", which now
    # also appears in the always-on SETUP_INSTRUCTIONS_TEMPLATE clipboard
    # payload's preflight comment after the 2026-05-10 init-report fix.
    assert 'class="install-block"' in pre.text  # setup view

    flip = c.post("/api/me/onboarded", cookies={"access_token": sess})
    assert flip.status_code == 200

    post = c.get("/home", cookies={"access_token": sess})
    assert "Welcome back" in post.text  # nav hub view
    assert 'class="install-block"' not in post.text


def test_home_renders_connector_prompts_from_shared_module(fresh_db):
    """Single source of truth check: the prompt text the /home tiles
    paste must equal the strings ``app/web/connector_prompts.py`` returns.
    The same strings are also inlined into the setup script's step 9, so
    if they ever drift the two surfaces would tell users to do different
    things — this test catches that early."""
    import html as _html
    import re

    from src.db import get_system_db, close_system_db
    from app.web.connector_prompts import (
        asana_prompt, gws_prompt, atlassian_prompt,
    )
    from app.instance_config import (
        get_gws_oauth_credentials, get_instance_admin_email,
    )

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn)
    finally:
        conn.close()
        close_system_db()

    c = _client()
    body = c.get("/home", cookies={"access_token": sess}).text

    # Resolve the same gws_oauth dict the route uses so the parity check
    # exercises whichever branch (configured / manual) is active in the
    # current test environment.
    gws = get_gws_oauth_credentials()
    expected_gws = gws_prompt(
        gws_oauth_configured=bool(gws.get("configured")),
        gws_client_id=str(gws.get("client_id") or ""),
        gws_client_secret=str(gws.get("client_secret") or ""),
        gws_project_id=str(gws.get("project_id") or ""),
        oauthlib_insecure_transport=str(gws.get("oauthlib_insecure_transport") or "1"),
        instance_admin_email=get_instance_admin_email(),
    )

    for slug, expected in (
        ("asana", asana_prompt()),
        ("gws", expected_gws),
        ("jira", atlassian_prompt()),
    ):
        m = re.search(rf'<code id="{slug}-prompt">(.*?)</code>', body, re.DOTALL)
        assert m, f"{slug}-prompt block missing from /home"
        actual = _html.unescape(m.group(1))
        assert actual == expected, (
            f"{slug}-prompt body diverged from connector_prompts module — "
            f"the home tile and setup script will paste different text. "
            f"len(home)={len(actual)} len(module)={len(expected)}"
        )
