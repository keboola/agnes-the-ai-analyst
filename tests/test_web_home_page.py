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
    assert "Step 1 &amp; Step 2 done" in body  # completion badge
    assert "Mark me as offboarded" in body  # offboard control visible
    # Inline Step 1 / Step 2 install-blocks are hidden post-onboarding —
    # the labels rendered inside the install-block divs go away.
    assert "Step 1 — install Claude Code" not in body
    assert "Step 2 — install Agnes from inside Claude Code" not in body


def test_step3_and_connectors_render_flat_when_onboarded_by_default(fresh_db):
    """Step 3 + Connect-your-tools sections must NOT auto-collapse on the
    server-side `users.onboarded=TRUE` flip. They render flat (in <details
    open>) by default; only an explicit user click on the in-hero
    "Minimize setup view" toggle (persisted in localStorage, not server)
    activates the collapsed bar layout."""
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
    # The full Step 3 + Connect-your-tools content is in the body.
    assert 'class="automode-card"' in body
    assert 'class="connector-tiles"' in body
    # Each section is wrapped in <details open> so the body is visible
    # without a click. The summary is rendered but CSS-hidden until
    # the page-level data-setup-minimized="1" attribute is set.
    assert 'class="setup-collapsible" data-section="step3" open' in body
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
