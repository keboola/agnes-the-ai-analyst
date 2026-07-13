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

from tests._template_assertions import assert_element, ElementNotFound


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


def test_home_not_onboarded_hero_title_html_renders_unescaped(fresh_db, monkeypatch):
    """Regression: PR #375's `{% set _brand = instance_brand | e %}` +
    `{% set hero_title_html = _brand ~ \"…<span>…\" %}` Jinja idiom silently
    escaped the literal `<span class=\"accent\">` because `Markup ~ str`
    autoescapes the str operand. The `| safe` in `_home_hero_intro.html`
    can't undo that — by then the chars are already `&lt;span&gt;`.

    Pin: the rendered hero title MUST contain the LITERAL `<span class=\"accent\">`
    tag (so the accent styling applies) AND must NOT contain the escaped
    `&lt;span` variant. Operator-controlled `instance_brand` must still
    be autoescaped — covered by `test_home_not_onboarded_hero_title_html_escapes_brand`.
    """
    monkeypatch.setenv("AGNES_INSTANCE_BRAND", "Acme")
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn, onboarded=False)
    finally:
        conn.close()
        close_system_db()
    body = _client().get("/home", cookies={"access_token": sess}).text
    assert 'Acme is your team\'s <span class="accent">AI workspace.</span>' in body
    assert "&lt;span class=" not in body, (
        "literal `<span>` chars are HTML-encoded — the Markup~str concat "
        "anti-pattern is back; use `{% set hero_title_html %}…{% endset %}` "
        "block form instead."
    )


def test_home_not_onboarded_hero_title_html_escapes_brand(fresh_db, monkeypatch):
    """XSS guard: operator-set `instance_brand` must be autoescaped before
    being spliced into the hero title (which contains literal HTML the
    partial renders with `| safe`). The previous `instance_brand | e` +
    `~` concat got this right at the cost of breaking literal HTML — the
    new `{% set %}…{% endset %}` block must preserve the escape guarantee."""
    monkeypatch.setenv("AGNES_INSTANCE_BRAND", "<script>alert(1)</script>EvilCo")
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn, onboarded=False)
    finally:
        conn.close()
        close_system_db()
    body = _client().get("/home", cookies={"access_token": sess}).text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;EvilCo" in body
    assert "<script>alert(1)</script>EvilCo" not in body


def test_home_hero_call_me_when_short_brand_differs(fresh_db, monkeypatch):
    """When `instance_brand_short` differs from the full brand, the hero
    title keeps the FULL brand and appends "Call me {short}." — and the
    rest of the page's body copy switches to the short form."""
    monkeypatch.setenv("AGNES_INSTANCE_BRAND", "Acme Data Analyst")
    monkeypatch.setenv("AGNES_INSTANCE_BRAND_SHORT", "Acme")
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn, onboarded=False)
    finally:
        conn.close()
        close_system_db()
    body = _client().get("/home", cookies={"access_token": sess}).text
    assert (
        'Acme Data Analyst is your team\'s <span class="accent">AI workspace.</span>'
        " Call me Acme." in body
    )
    # Body copy uses the short brand, not the full one.
    assert "Set up Acme on your machine" in body
    assert "Set up Acme Data Analyst on your machine" not in body


def test_home_hero_no_call_me_when_short_equals_brand(fresh_db, monkeypatch):
    """Default: brand_short mirrors the full brand, so the hero renders
    exactly as before — no "Call me" sentence."""
    monkeypatch.setenv("AGNES_INSTANCE_BRAND", "Acme")
    monkeypatch.delenv("AGNES_INSTANCE_BRAND_SHORT", raising=False)
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn, onboarded=False)
    finally:
        conn.close()
        close_system_db()
    body = _client().get("/home", cookies={"access_token": sess}).text
    assert 'Acme is your team\'s <span class="accent">AI workspace.</span>' in body
    assert "Call me" not in body


def test_home_hero_short_brand_is_escaped(fresh_db, monkeypatch):
    """XSS guard for the short brand: it is operator-controlled and spliced
    into `hero_title_html` (rendered with `| safe`), so it must be
    autoescaped exactly like the full brand."""
    monkeypatch.setenv("AGNES_INSTANCE_BRAND", "Acme Data Analyst")
    monkeypatch.setenv("AGNES_INSTANCE_BRAND_SHORT", "<script>alert(1)</script>EvilCo")
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn, onboarded=False)
    finally:
        conn.close()
        close_system_db()
    body = _client().get("/home", cookies={"access_token": sess}).text
    assert "Call me &lt;script&gt;alert(1)&lt;/script&gt;EvilCo." in body
    assert "<script>alert(1)</script>EvilCo" not in body


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
    with pytest.raises(ElementNotFound):
        assert_element(body, "div", class_="install-hero")
    # Offboard escape strip + its button replace the in-hero self-mark control.
    assert_element(body, "div", class_="offboard-strip")
    assert "Mark me as offboarded" in body
    # All install-blocks are hidden post-onboarding — the labels rendered
    # inside the install-block divs go away. FAI-35 removed the manual
    # "Optional: create a one-word shortcut" block; agnes init now
    # auto-creates the shortcut.
    assert "Step 1 — Install Claude Code on your computer" not in body
    assert "Step 2 — Pick a folder for" not in body
    assert "Step 3 — Open a terminal inside that folder" not in body
    assert "Step 4 — Launch Claude with auto-approve on" not in body
    assert "Step 5 — Get the install script" not in body
    assert "Optional: create a one-word shortcut" not in body


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
    assert "Step 4 — Launch Claude with auto-approve on" not in body
    # Dedicated connectors block is gone from /home in BOTH states.
    assert 'class="connector-tiles"' not in body
    assert 'data-section="connectors"' not in body
    # Server-rendered HTML never carries the data-setup-minimized
    # attribute on the .home-mock root — that's a client-side
    # localStorage decision applied via JS on load.
    # attribute-presence semantics — not class-equality
    assert '<div class="home-mock" data-setup-minimized' not in body
    assert 'class="home-mock"\n' in body or '<div class="home-mock">' in body

    # Not-onboarded path: same — the section disappears regardless of
    # state. Lead-paragraph still surfaces the connector names so users
    # know the benefit exists before they kick off the install.
    conn = get_system_db()
    try:
        _, sess2 = _make_user_and_session(conn, email="not-onboarded@example.com", onboarded=False)
    finally:
        conn.close()
        close_system_db()
    body2 = _client().get("/home", cookies={"access_token": sess2}).text
    assert 'class="connector-tiles"' not in body2
    assert 'data-section="connectors"' not in body2
    # The install-prompt's finale step lists the configured connectors
    # by display_name — sourced from the seed manifest. Bundled snapshot
    # ships Asana, Atlassian (Jira / Confluence), Google Workspace (the
    # alphabetical sort order ``load_manifest`` enforces).
    assert "Asana, Atlassian (Jira / Confluence), Google Workspace" in body2


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
            _, sess = _make_user_and_session(conn, email=f"user-{onboarded}@example.com", onboarded=onboarded)
        finally:
            conn.close()
            close_system_db()
        c = _client()
        resp = c.get("/home", cookies={"access_token": sess})
        assert resp.status_code == 200
        with pytest.raises(ElementNotFound):
            assert_element(resp.text, "button", attrs={"id": "setupMinimizeToggle"})
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
    assert_element(post.text, "div", class_="offboard-strip")
    assert 'class="install-block"' not in post.text


def test_home_reads_onboarded_through_repo_factory_not_raw_duckdb(fresh_db, monkeypatch):
    """/home must read `onboarded` through `users_repo()`, not a raw
    `conn.execute` against DuckDB.

    Regression: on a Postgres-backed instance (db-state-machine CLOUD /
    SIDE_CAR state) POST /api/me/onboarded writes the flag to Postgres via
    `users_repo()`, but /home was reading it back with a raw DuckDB query
    on the request `conn`. The DuckDB row stays frozen at its pre-migration
    value, so the "Mark me as onboarded" button flips Postgres yet /home
    renders the setup view forever — independent of any browser cache.

    Simulate the split without a live Postgres: the DuckDB row says
    onboarded=FALSE, but the repo factory reports TRUE (the real backend).
    /home must honor the repo (show the nav hub), proving it no longer
    reads the stale raw-DuckDB value."""
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        uid, sess = _make_user_and_session(conn, onboarded=False)
    finally:
        conn.close()
        close_system_db()

    import app.web.router as router

    class _FakeUsersRepo:
        def get_by_id(self, user_id):
            # Mirrors the active (e.g. Postgres) backend's truth, which
            # diverges from the stale DuckDB row.
            return {"id": user_id, "email": "u@example.com", "onboarded": True}

    monkeypatch.setattr(router, "users_repo", lambda: _FakeUsersRepo())

    resp = _client().get("/home", cookies={"access_token": sess})
    assert resp.status_code == 200
    # Honors the repo (onboarded=True) → nav-hub view, not the stale
    # DuckDB FALSE → setup view.
    assert_element(resp.text, "div", class_="offboard-strip")
    assert 'class="install-block"' not in resp.text


def test_home_cowork_card_links_to_me_cowork(fresh_db):
    """The /home Cowork surface card carries real upload instructions and now
    points at /me/ai-connector for the per-plugin download list (the list + the
    package guideline were relocated there so there is a single home for them).
    Pin: the placeholder badge is gone, the inline download list is no longer
    on /home, and the card links to /me/ai-connector."""
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn, onboarded=False)
    finally:
        conn.close()
        close_system_db()

    body = _client().get("/home", cookies={"access_token": sess}).text
    # Placeholder removed.
    assert "INSTRUCTIONS NEEDED" not in body
    # The inline plugin list moved to /me/ai-connector; /home links there instead.
    assert 'id="cowork-plugin-list"' not in body
    assert 'href="/me/ai-connector"' in body


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
    assert "mailto:ops@example.com" not in body


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


# ── Setup section header + Overview + Usage modes ────────────────────────


def test_setup_section_renders_for_not_onboarded(fresh_db):
    """Not-onboarded users land on /home and see the setup section
    header (eyebrow + heading + lede) floating above the install hero
    card. The dismissible Getting Started shortcut block has been
    removed — its two links lived only as in-page jumps and duplicated
    the install-hero + /setup-advanced affordances already present on
    the page. Onboarded users see neither header nor install hero so
    the page reads as a hub, not a setup screen."""
    from src.db import get_system_db, close_system_db

    # Not-onboarded: setup header + install hero both render.
    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn, email="setup-not-onboarded@example.com", onboarded=False)
    finally:
        conn.close()
        close_system_db()
    body = _client().get("/home", cookies={"access_token": sess}).text
    # Header floats above the card with the design spec eyebrow + h2.
    assert_element(body, "div", class_="setup-section-header")
    assert ">First time here<" in body
    assert "Set up" in body and "on your machine" in body
    # Install hero card sits below the header.
    assert_element(body, "div", class_="install-hero")
    # Getting Started shortcut block is gone.
    assert "home-getting-started" not in body
    assert "agnes_home_gs_dismissed" not in body

    # Onboarded: install hero (and the setup header above it) are gone.
    conn = get_system_db()
    try:
        _, sess2 = _make_user_and_session(conn, email="setup-onboarded@example.com", onboarded=True)
    finally:
        conn.close()
        close_system_db()
    body2 = _client().get("/home", cookies={"access_token": sess2}).text
    with pytest.raises(ElementNotFound):
        assert_element(body2, "div", class_="install-hero")
    with pytest.raises(ElementNotFound):
        assert_element(body2, "div", class_="setup-section-header")


def test_step2_windows_command_is_single_line(fresh_db):
    """FAI-50 regression: Step 2 "Pick a folder" Windows/PowerShell command
    must be ONE physical line so a single paste both creates the folder and
    `cd`s into it. Previously it was two newline-separated statements; on
    paste, the embedded newline submitted only the first line (`New-Item`)
    and left `Set-Location` unsent in the PowerShell input buffer — the
    shell never entered the new folder. The two statements are joined with
    `;` (works in Windows PowerShell 5.1 and pwsh 7; `&&` would parse-fail
    on 5.1), mirroring the macOS/Linux tab's single-line `mkdir … && cd …`."""
    import re

    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn, email="fai50-step2@example.com", onboarded=False)
    finally:
        conn.close()
        close_system_db()

    body = _client().get("/home", cookies={"access_token": sess}).text

    m = re.search(r'id="install-cmd-mkdir-windows">(.*?)</span>', body, re.DOTALL)
    assert m, "Step 2 Windows command span not found"
    cmd = m.group(1)

    # Both statements present, joined by a semicolon on one line.
    assert "New-Item -ItemType Directory" in cmd
    assert "Set-Location" in cmd
    assert "| Out-Null; Set-Location" in cmd
    # The regression guard: no newline anywhere inside the command — a
    # multi-line paste is exactly what broke PowerShell execution.
    assert "\n" not in cmd.strip()


def test_welcome_footnotes_render_overview_when_set(fresh_db, monkeypatch):
    """Setting `AGNES_INSTANCE_OVERVIEW` (mirrors `instance.overview`
    yaml) injects raw HTML into the welcome-hero footnotes via the
    same `| safe` filter as the previous standalone Overview
    section. The marker text MUST appear inside
    `.home-hero-footnotes`, and the legacy `<section class="home-overview">`
    wrapper MUST stay absent — the operator-owned body now lives
    inside the welcome card, not as a separate section between the
    walkthrough and surfaces grid."""
    monkeypatch.setenv("AGNES_INSTANCE_OVERVIEW", "<p>OVERVIEW_TEST_MARKER</p>")
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn)
    finally:
        conn.close()
        close_system_db()
    body = _client().get("/home", cookies={"access_token": sess}).text
    with pytest.raises(ElementNotFound):
        assert_element(body, "section", class_="home-overview")
    assert_element(body, "div", class_="home-hero-footnotes")
    assert "OVERVIEW_TEST_MARKER" in body


def test_welcome_footnotes_hidden_when_overview_unset(fresh_db, monkeypatch):
    """Default empty `instance.overview` (no env override) hides the
    welcome-hero footnotes entirely so the OSS ships without a
    stray empty footnotes block in the welcome card."""
    monkeypatch.delenv("AGNES_INSTANCE_OVERVIEW", raising=False)
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn)
    finally:
        conn.close()
        close_system_db()
    body = _client().get("/home", cookies={"access_token": sess}).text
    with pytest.raises(ElementNotFound):
        assert_element(body, "div", class_="home-hero-footnotes")


def test_welcome_support_renders_when_set(fresh_db, monkeypatch):
    """Setting `AGNES_INSTANCE_SUPPORT` (mirrors `instance.support`
    yaml) injects raw HTML into the mint-accent Support callout
    inside the welcome hero. The marker text MUST appear inside
    `.home-hero-support-body`. Separate field from
    `instance.overview` so support/help pointers can be updated
    independently from the operator's product framing."""
    monkeypatch.setenv("AGNES_INSTANCE_SUPPORT", "<p>SUPPORT_TEST_MARKER</p>")
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn)
    finally:
        conn.close()
        close_system_db()
    body = _client().get("/home", cookies={"access_token": sess}).text
    assert_element(body, "div", class_="home-hero-support")
    assert "SUPPORT_TEST_MARKER" in body


def test_welcome_support_hidden_when_unset(fresh_db, monkeypatch):
    """Default empty `instance.support` (no env override) hides the
    Support callout entirely so the OSS ships without a stray
    empty mint panel in the welcome card."""
    monkeypatch.delenv("AGNES_INSTANCE_SUPPORT", raising=False)
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn)
    finally:
        conn.close()
        close_system_db()
    body = _client().get("/home", cookies={"access_token": sess}).text
    with pytest.raises(ElementNotFound):
        assert_element(body, "div", class_="home-hero-support")


def test_welcome_support_independent_of_overview(fresh_db, monkeypatch):
    """The Support callout MUST render even when `instance.overview`
    is empty — the two fields are independent. Catches a regression
    where the Support gate was accidentally wired to
    INSTANCE_OVERVIEW instead of INSTANCE_SUPPORT."""
    monkeypatch.delenv("AGNES_INSTANCE_OVERVIEW", raising=False)
    monkeypatch.setenv("AGNES_INSTANCE_SUPPORT", "<p>SUPPORT_ONLY_MARKER</p>")
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn)
    finally:
        conn.close()
        close_system_db()
    body = _client().get("/home", cookies={"access_token": sess}).text
    with pytest.raises(ElementNotFound):
        assert_element(body, "div", class_="home-hero-footnotes")
    assert_element(body, "div", class_="home-hero-support")
    assert "SUPPORT_ONLY_MARKER" in body
