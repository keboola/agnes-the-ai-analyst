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
    """A TRUE-onboarded user gets the post-onboarding view: the blue
    install-hero is gone entirely (no welcome banner, no completion
    badge, no inline step commands), the offboard escape strip is the
    only setup-flow remnant rendered, and the rest of /home (connector
    tiles, news, etc.) stays. PR #289 collapsed the dual-state hero
    into a single not-onboarded-only render — pre-PR the onboarded
    branch reused the same `.install-hero` shell with welcome copy
    and a "Steps 1–4 done" badge."""
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
    # Install hero entirely absent for onboarded users.
    assert '<div class="install-hero">' not in body
    # Offboard escape strip + its button replace the in-hero self-mark control.
    assert '<div class="offboard-strip">' in body
    assert "Mark me as offboarded" in body
    # All four inline install-blocks are hidden post-onboarding — the
    # labels rendered inside the install-block divs go away.
    assert "Step 1 — install Claude Code" not in body
    assert "Step 2 — turn on auto-mode" not in body
    assert "Step 3 — create your workspace folder" not in body
    assert "Step 4 — install" not in body


def test_connectors_section_removed_from_home(fresh_db):
    """The dedicated `<details data-section="connectors">` block was
    dropped from `/home` — the install-hero's Step 4 clipboard payload
    (rendered via `_claude_setup_instructions.jinja` inside the manual
    fallback) already inlines the same Asana / GWS / Atlassian prompts
    from `app/web/connector_prompts.py` via
    `app/web/setup_instructions.py::_connectors_block`. Showing them
    twice on the same page was duplicate UX. The lead paragraph in the
    install-hero now mentions the connectors briefly so users still see
    the benefit before they hit the install.

    Co-asserts the auto-mode block removal that this test originally
    pinned — onboarded users still see neither the connectors block
    nor the legacy auto-mode peer section."""
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
    # Auto-mode peer section still gone (legacy guard, not regressed).
    assert 'class="automode-card"' not in body
    assert 'data-section="step3"' not in body
    assert "Step 2 — turn on auto-mode" not in body
    # Dedicated connectors block is gone from /home in BOTH states.
    assert 'class="connector-tiles"' not in body
    assert 'data-section="connectors"' not in body
    # Server-rendered HTML never carries the data-setup-minimized
    # attribute on the .home-mock root — that's a client-side
    # localStorage decision applied via JS on load.
    assert '<div class="home-mock" data-setup-minimized' not in body
    assert 'class="home-mock"\n' in body or '<div class="home-mock">' in body

    # Not-onboarded path: same — the section disappears regardless of
    # state. Lead-paragraph still surfaces the connector names so users
    # know the benefit exists before they kick off the install.
    conn = get_system_db()
    try:
        _, sess2 = _make_user_and_session(
            conn, email="not-onboarded@example.com", onboarded=False
        )
    finally:
        conn.close()
        close_system_db()
    body2 = _client().get("/home", cookies={"access_token": sess2}).text
    assert 'class="connector-tiles"' not in body2
    assert 'data-section="connectors"' not in body2
    # Lead-paragraph mentions the three connector families so the
    # benefit isn't lost when the dedicated section disappears.
    assert "Asana, Google Workspace, Atlassian" in body2


def test_minimize_toggle_no_longer_rendered(fresh_db):
    """The "Minimize setup view" toggle used to live inside the
    onboarded-branch of the install-hero. PR #289 hides the hero
    entirely once `users.onboarded=true`, so the minimize toggle
    has no rendering site anymore — verify the markup is absent
    from both states. (The localStorage `agnes_home_setup_minimized`
    flag and its applyMinimize() JS handler stay in the page so a
    stale flag from a pre-PR session no-ops cleanly.)"""
    from src.db import get_system_db, close_system_db

    for onboarded in (False, True):
        conn = get_system_db()
        try:
            _, sess = _make_user_and_session(
                conn, email=f"user-{onboarded}@example.com", onboarded=onboarded
            )
        finally:
            conn.close()
            close_system_db()
        c = _client()
        resp = c.get("/home", cookies={"access_token": sess})
        assert resp.status_code == 200
        assert '<button id="setupMinimizeToggle"' not in resp.text
        assert 'class="setup-minimize"' not in resp.text


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
    # PR #289: hero disappears entirely; offboard strip is the
    # only setup-flow remnant. Use either as the nav-hub view marker.
    assert '<div class="offboard-strip">' in post.text
    assert 'class="install-block"' not in post.text


# ── GWS Email-admin button render tests (admin_email knob coverage) ────────


def test_home_hides_email_admin_button_when_admin_email_unset(fresh_db, monkeypatch):
    """When ``instance.admin_email`` is unset, the GWS connector tile
    must NOT render the mailto link (template guards on truthiness;
    empty resolver value cleanly hides). Defends against a `mailto:?`
    link sneaking out as a render-time artifact."""
    monkeypatch.delenv("AGNES_INSTANCE_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("AGNES_GWS_CLIENT_ID", raising=False)
    monkeypatch.delenv("AGNES_GWS_CLIENT_SECRET", raising=False)
    from src.db import get_system_db, close_system_db
    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn)
    finally:
        conn.close()
        close_system_db()
    body = _client().get("/home", cookies={"access_token": sess}).text
    # No "Email admin" CTA, no mailto: link in the body.
    assert "Email admin" not in body
    assert "mailto:?" not in body  # specifically, no broken empty mailto


def test_home_no_longer_shows_email_admin_button(fresh_db, monkeypatch):
    """The Email-admin mailto CTA used to live inside the /home GWS
    connector tile. With the dedicated `<details data-section="connectors">`
    block removed (see test_connectors_section_removed_from_home above),
    the button has no rendering site even when admin_email is set + GWS
    is unconfigured. The escalation path lives inside the install
    script's GWS step now — Claude prompts the user with the admin
    email when the connector setup hits an OAuth gating wall, so the
    affordance moves to the surface where it's actually useful."""
    monkeypatch.setenv("AGNES_INSTANCE_ADMIN_EMAIL", "ops@example.com")
    monkeypatch.delenv("AGNES_GWS_CLIENT_ID", raising=False)
    monkeypatch.delenv("AGNES_GWS_CLIENT_SECRET", raising=False)
    from src.db import get_system_db, close_system_db
    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn)
    finally:
        conn.close()
        close_system_db()
    body = _client().get("/home", cookies={"access_token": sess}).text
    assert "Email admin" not in body
    assert 'mailto:ops@example.com' not in body


def test_home_hides_email_admin_button_when_gws_configured(fresh_db, monkeypatch):
    """Even with admin_email set, when GWS OAuth is operator-provisioned
    (gws_oauth.configured = True), the Email-admin CTA is redundant —
    user can just connect. Template gates on `not gws_oauth.configured`."""
    monkeypatch.setenv("AGNES_INSTANCE_ADMIN_EMAIL", "ops@example.com")
    monkeypatch.setenv("AGNES_GWS_CLIENT_ID", "abc.apps.googleusercontent.com")
    monkeypatch.setenv("AGNES_GWS_CLIENT_SECRET", "GOCSPX-secret")
    from src.db import get_system_db, close_system_db
    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn)
    finally:
        conn.close()
        close_system_db()
    body = _client().get("/home", cookies={"access_token": sess}).text
    assert "Email admin" not in body


# `test_home_renders_connector_prompts_from_shared_module` was dropped here
# alongside the removal of the /home `<details data-section="connectors">`
# block. The test pinned source-of-truth parity between the home tile
# `<code id="*-prompt">` blocks and `app/web/connector_prompts.py`. With the
# tiles gone, the only surface left for those strings is the install-hero's
# Step 4 clipboard payload (rendered via `_claude_setup_instructions.jinja`
# from `setup_instructions_lines`, which is built in
# `app/web/setup_instructions.py::_connectors_block` calling the same
# `connector_prompts.py` functions). One surface, no drift risk → the
# parity test is redundant. If a second surface ever re-renders these
# prompts, restore a parity test scoped to that new consumer.


# ── Getting Started + Overview + Usage modes (PR #289 home additions) ────


def test_getting_started_card_renders_on_home(fresh_db):
    """The dismissible Getting Started card now renders BEFORE the
    install-hero (chronologically first in the not-onboarded flow) as
    a <details> element — collapsed by default so the install hero
    stays visible on first paint. Disappears when the user is
    onboarded (no `<details class="home-getting-started">`) so the
    in-page #install-hero anchor on the first row never points at
    nothing. First row links to #install-hero (same-page jump to the
    blue setup hero); second row still leaves the page for
    /setup-advanced."""
    from src.db import get_system_db, close_system_db

    # Not-onboarded: GS is rendered + install-hero anchor target exists.
    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(
            conn, email="gs-not-onboarded@example.com", onboarded=False
        )
    finally:
        conn.close()
        close_system_db()
    body = _client().get("/home", cookies={"access_token": sess}).text
    assert '<details class="home-getting-started"' in body
    assert 'data-dismiss-key="agnes_home_gs_dismissed"' in body
    assert 'class="home-gs-item" href="#install-hero"' in body
    assert 'class="home-gs-item" href="/setup-advanced"' in body
    # Install-hero must carry the matching id so the first-row anchor
    # resolves. Co-asserted with the GS markup so a refactor that drops
    # one but not the other breaks here, not in the browser.
    assert '<div class="install-hero" id="install-hero">' in body

    # Onboarded: install-hero is gone, GS rides alongside it — neither
    # renders. Prevents a dangling #install-hero anchor.
    conn = get_system_db()
    try:
        _, sess2 = _make_user_and_session(
            conn, email="gs-onboarded@example.com", onboarded=True
        )
    finally:
        conn.close()
        close_system_db()
    body2 = _client().get("/home", cookies={"access_token": sess2}).text
    assert '<details class="home-getting-started"' not in body2
    assert '<div class="install-hero"' not in body2


def test_overview_section_renders_when_yaml_set(fresh_db, monkeypatch):
    """Setting `AGNES_INSTANCE_OVERVIEW` env (mirrors
    instance.overview yaml) injects raw HTML into the Overview section
    via the same `| safe` filter as news_intro. The marker text must
    appear inside the rendered section wrapper. Overview deliberately
    has NO dismiss button — it's operator-owned reference content
    (privacy posture, telemetry policy, product framing), and a
    per-device hide would leave returning users unable to re-read
    it without clearing localStorage."""
    monkeypatch.setenv("AGNES_INSTANCE_OVERVIEW", "<p>OVERVIEW_TEST_MARKER</p>")
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn)
    finally:
        conn.close()
        close_system_db()
    body = _client().get("/home", cookies={"access_token": sess}).text
    assert '<section class="home-overview">' in body
    assert "OVERVIEW_TEST_MARKER" in body
    # Overview must NOT carry a dismiss key — content stays
    # reachable on every visit so users can re-read it.
    assert 'data-dismiss-key="agnes_home_overview_dismissed"' not in body


def test_overview_section_hidden_when_yaml_empty(fresh_db, monkeypatch):
    """Default empty `instance.overview` (no env override) hides the
    section entirely so the OSS ships without a stray empty
    Overview placeholder."""
    monkeypatch.delenv("AGNES_INSTANCE_OVERVIEW", raising=False)
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn)
    finally:
        conn.close()
        close_system_db()
    body = _client().get("/home", cookies={"access_token": sess}).text
    assert '<section class="home-overview">' not in body
