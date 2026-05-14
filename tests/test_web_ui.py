"""Smoke tests for web UI pages."""
import os
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def web_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    (tmp_path / "state").mkdir()
    (tmp_path / "analytics").mkdir()
    (tmp_path / "extracts").mkdir()
    # Reset global DuckDB singleton to pick up new DATA_DIR
    from src.db import close_system_db
    close_system_db()
    from app.main import create_app
    app = create_app()
    yield TestClient(app)
    close_system_db()


@pytest.fixture
def admin_cookie(web_client, tmp_path, monkeypatch):
    from argon2 import PasswordHasher
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from tests.helpers.auth import grant_admin
    password = "AdminPass1!"
    password_hash = PasswordHasher().hash(password)
    conn = get_system_db()
    UserRepository(conn).create(
        id="admin1", email="admin@test.com", name="Admin",
        password_hash=password_hash,
    )
    grant_admin(conn, "admin1")
    conn.close()
    resp = web_client.post("/auth/token", json={"email": "admin@test.com", "password": password})
    assert resp.status_code == 200, f"Bootstrap failed: {resp.text}"
    token = resp.json()["access_token"]
    return {"access_token": token}


@pytest.fixture
def analyst_cookie(web_client, tmp_path, monkeypatch):
    from argon2 import PasswordHasher
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    password = "AnalystPass1!"
    password_hash = PasswordHasher().hash(password)
    conn = get_system_db()
    UserRepository(conn).create(
        id="analyst1", email="analyst@test.com", name="Analyst",
        password_hash=password_hash,
    )
    conn.close()
    resp = web_client.post("/auth/token", json={"email": "analyst@test.com", "password": password})
    assert resp.status_code == 200, f"Analyst token failed: {resp.text}"
    token = resp.json()["access_token"]
    return {"access_token": token}


class TestWebUISmoke:
    def test_login_page(self, web_client):
        resp = web_client.get("/login")
        assert resp.status_code == 200

    def test_dashboard(self, web_client, admin_cookie):
        resp = web_client.get("/dashboard", cookies=admin_cookie)
        assert resp.status_code in (200, 302)

    def test_catalog(self, web_client, admin_cookie):
        resp = web_client.get("/catalog", cookies=admin_cookie)
        assert resp.status_code == 200

    def test_corporate_memory(self, web_client, admin_cookie):
        resp = web_client.get("/corporate-memory", cookies=admin_cookie)
        assert resp.status_code == 200

    def test_activity_center(self, web_client, admin_cookie):
        resp = web_client.get("/activity-center", cookies=admin_cookie)
        assert resp.status_code == 200

    def test_admin_tables(self, web_client, admin_cookie):
        resp = web_client.get("/admin/tables", cookies=admin_cookie)
        if resp.status_code == 404:
            pytest.skip("Route /admin/tables does not exist")
        assert resp.status_code == 200

    def test_admin_permissions_route_removed(self, web_client, admin_cookie):
        """v19 dropped the half-shipped /admin/permissions page (replaced by
        the unified /admin/access page). Verify the route is gone."""
        resp = web_client.get("/admin/permissions", cookies=admin_cookie)
        assert resp.status_code == 404

    def test_admin_users_renders_modern_ui(self, web_client, admin_cookie):
        resp = web_client.get("/admin/users", cookies=admin_cookie)
        assert resp.status_code == 200
        body = resp.text
        # Shared header chrome
        assert "app-header" in body
        # User-self menu post-consolidation: Profile + My activity only.
        # Auth debug folded into /me/profile troubleshooting section; the
        # /me/debug nav entry is gone.
        assert 'href="/me/profile"' in body
        assert 'href="/me/activity"' in body
        assert 'href="/me/debug"' not in body
        # Admin dropdown still carries the cross-user PAT admin entry.
        assert 'href="/admin/tokens"' in body
        assert 'href="/admin/users"' in body
        # v12 modern UI markers — Role column was replaced by Groups chips,
        # so role-pill is gone. Confirm-modal pattern is shared by both.
        assert 'class="users-page"' in body
        assert 'id="confirm-modal"' in body

    def test_nav_shows_user_self_links_for_non_admin(self, web_client, analyst_cookie):
        """Non-admins see Profile + My activity user-menu links — no admin
        Tokens entry, no Auth debug entry (folded into /me/profile)."""
        resp = web_client.get("/dashboard", cookies=analyst_cookie)
        assert resp.status_code in (200, 302)
        if resp.status_code == 302:
            # Dashboard may redirect in some flows; follow it for nav check.
            resp = web_client.get(resp.headers["location"], cookies=analyst_cookie)
        body = resp.text
        assert 'href="/me/profile"' in body
        assert ">Profile<" in body
        assert 'href="/me/activity"' in body
        assert ">My activity<" in body
        # Auth debug entry is gone from the nav — folded into /me/profile.
        assert 'href="/me/debug"' not in body
        assert ">Auth debug<" not in body
        # Retired entries must not surface.
        assert ">My tokens<" not in body
        assert ">My sessions<" not in body
        # Non-admins must NOT see the admin Tokens link inside the Admin dropdown.
        assert 'href="/admin/tokens"' not in body

    def test_nav_shows_admin_dropdown_for_admin(self, web_client, admin_cookie):
        """Admins see the same user-self menu + the Admin dropdown with
        cross-user Tokens / Tables / Users entries."""
        resp = web_client.get("/dashboard", cookies=admin_cookie)
        assert resp.status_code in (200, 302)
        if resp.status_code == 302:
            resp = web_client.get(resp.headers["location"], cookies=admin_cookie)
        body = resp.text
        # User-self menu — same as non-admin; Auth debug gone from nav.
        assert 'href="/me/activity"' in body
        assert 'href="/me/debug"' not in body
        assert ">My tokens<" not in body
        # Admin dropdown — Tables / Tokens / Users / Groups / Resource access / Server config.
        assert 'href="/admin/tokens"' in body
        assert 'href="/admin/tables"' in body
        assert ">Tables<" in body
        assert ">Tokens<" in body

    def test_profile_renders_account_details(self, web_client, admin_cookie):
        """/me/profile renders a real profile page with email + inline PAT section.

        v12 changes: role-pill is replaced by an Admin-pill driven by Admin
        user_group membership; ``session.google_groups`` is gone (the
        OAuth callback writes Workspace memberships into
        ``user_group_members`` instead), so the "No Google groups available"
        empty state is no longer rendered.
        Task 3: /tokens link removed; PAT management is now inline on this page.
        """
        resp = web_client.get("/me/profile", cookies=admin_cookie)
        assert resp.status_code == 200
        body = resp.text
        assert "admin@test.com" in body
        assert 'href="/tokens"' not in body
        # Inline PAT section is present
        assert "Personal Authentication Tokens" in body
        assert 'id="new-token-btn"' in body
        # Session & troubleshooting partial is included — a broken
        # {% include %} or missing template var would drop this string.
        assert "User record" in body

    def test_profile_requires_auth(self, web_client):
        """/me/profile requires auth (was a 302 back-compat redirect before)."""
        resp = web_client.get("/me/profile", follow_redirects=False)
        # Auth dep raises 401; some configs may redirect to /login — accept either.
        assert resp.status_code in (401, 302)


class TestProfileSensitiveLeakage:
    """The /me/profile page absorbed the former /me/debug session-diagnostics
    surface (Session & troubleshooting section). The security invariant that
    protected that surface survives the move: the raw session JWT must never
    appear in the rendered page — only its decoded claims and a short
    fingerprint. Compensating test for the deleted
    test_me_debug.TestNoSensitiveLeakage.test_raw_jwt_not_in_body."""

    def test_raw_jwt_not_in_profile_body(self, web_client, analyst_cookie):
        """The full session JWT must never appear in the rendered /me/profile
        page — only its decoded claims and a short fingerprint."""
        raw_token = analyst_cookie["access_token"]
        resp = web_client.get("/me/profile", cookies=analyst_cookie)
        assert resp.status_code == 200
        assert raw_token not in resp.text, "raw JWT leaked into page body"

    @pytest.mark.skip(
        reason=(
            "v12: /me/profile no longer renders an admin-self-management link. "
            "Admin can navigate to /admin/users/{id} from the top-nav Admin "
            "dropdown directly. Drop or rewrite this test once the profile "
            "page settles."
        )
    )
    def test_profile_shows_admin_detail_link_for_admin(self, web_client, admin_cookie):
        resp = web_client.get("/me/profile", cookies=admin_cookie)
        assert resp.status_code == 200
        assert 'href="/admin/users/admin1"' in resp.text

    @pytest.mark.skip(
        reason=(
            "v12: profile page no longer surfaces /admin/users/* link at all, "
            "so the negative-assertion is moot. Header chrome unrelated to "
            "the profile body now contains the admin dropdown."
        )
    )
    def test_profile_hides_admin_detail_link_for_non_admin(self, web_client, analyst_cookie):
        resp = web_client.get("/me/profile", cookies=analyst_cookie)
        assert resp.status_code == 200
        assert "/admin/users/" not in resp.text

    @pytest.mark.skip(
        reason=(
            "v12: the four-level core.viewer/analyst/km_admin/admin hierarchy "
            "is gone. Profile now shows group memberships (user_group_members) "
            "and effective resource access (resource_grants), not internal "
            "role keys. Rewrite against the new sections — see "
            "templates/profile.html."
        )
    )
    def test_profile_shows_effective_roles_for_non_admin(self, web_client, analyst_cookie):
        resp = web_client.get("/me/profile", cookies=analyst_cookie)
        assert resp.status_code == 200
        body = resp.text
        assert "Effective roles" in body
        assert "core.analyst" in body
        assert "core.viewer" in body
        assert "Direct grants" in body


class TestClaudeSetupPreview:
    """/install and /dashboard render a visible, read-only preview of the
    'Setup a new Claude Code' clipboard payload. The real token is never
    rendered into the HTML — only a styled placeholder is.
    """

    def test_install_preview_visible_for_signed_in_user(self, web_client, admin_cookie):
        # /setup is now a single unified flow regardless of caller's role.
        # Admin sees the same layout as everyone else; the marketplace
        # block appears iff the caller has plugin grants in
        # `resource_grants` (the seeded admin in this fixture has none).
        resp = web_client.get("/setup", cookies=admin_cookie)
        assert resp.status_code == 200
        body = resp.text
        # Preview card + placeholder token render
        assert "setup-preview-pre" in body
        assert "What Claude Code will receive" in body
        assert "&lt;will be generated on click&gt;" in body
        assert 'class="placeholder-token"' in body
        # Setup payload text substituted with real server URL. The wheel URL
        # must be under /cli/wheel/ (uv tool install rejects a bare .whl alias
        # because it validates the PEP 427 filename in the URL before fetch).
        assert "/cli/wheel/" in body
        assert "/cli/agnes.whl" not in body
        # Unified always-on layout (Fix B + Fix C in 2026-05-10 init-report
        # response): preflight + marketplace + Atlassian MCP all unconditional.
        # Step 1 install, step 2 mkdir/cd, step 3 init, step 4 catalog,
        # step 5 preflight, step 6 marketplace, step 7 diagnose.
        assert "1) Install the CLI" in body
        assert "7) Run diagnostics" in body
        assert "agnes diagnose" in body
        # `agnes init` is now the mandatory bootstrap step.
        assert "agnes init" in body
        # The generated /setup prompt's "Log in" / "Verify the login"
        # admin-only headers are gone (agnes init subsumes them).
        # `agnes auth whoami` survives as a static manual-install
        # example elsewhere on the page (not in the generated prompt).
        assert "2) Log in" not in body
        assert "3) Verify the login" not in body

    def test_install_preview_unified_layout(self, web_client, admin_cookie):
        """The clipboard payload (SETUP_INSTRUCTIONS_TEMPLATE JS array)
        carries the unified layout for every caller — admin-vs-analyst
        is no longer a layout branch. Marketplace + Atlassian MCP blocks
        are always emitted (Fix B + Fix C in 2026-05-10 init-report
        response): the user-facing one-liner is `agnes refresh-marketplace
        --bootstrap` (the literal `claude plugin marketplace add` shows up
        only as a documentation comment listing what the binary does
        internally, never as an instruction to run by hand)."""
        import re
        resp = web_client.get("/setup", cookies=admin_cookie)
        assert resp.status_code == 200
        body = resp.text
        match = re.search(
            r"var\s+SETUP_INSTRUCTIONS_TEMPLATE\s*=\s*\[(.*?)\]\.join\(",
            body, re.DOTALL,
        )
        assert match, "SETUP_INSTRUCTIONS_TEMPLATE array missing"
        clipboard = match.group(1)
        assert "agnes init" in clipboard
        # User runs the bootstrap one-liner, not raw `claude plugin
        # marketplace add` — the latter is an internal step described in a
        # comment block, never an action line to run.
        assert "agnes refresh-marketplace --bootstrap" in clipboard
        # Atlassian MCP registration is always-on now.
        assert "claude mcp add --transport sse atlassian" in clipboard
        # Legacy admin-only auth verbs are gone from the generated prompt.
        assert "agnes auth import-token" not in clipboard
        # `agnes auth whoami` was the old admin step 3; subsumed by
        # `agnes init` + `agnes catalog` smoke verify.
        assert "3) Verify the login" not in clipboard
        assert "2) Log in" not in clipboard

    def test_dashboard_setup_cta_links_to_setup(self, web_client, admin_cookie):
        """Dashboard setup CTA shows env-setup-cta and a link to /setup instead
        of an inline collapsed preview."""
        resp = web_client.get("/dashboard", cookies=admin_cookie)
        assert resp.status_code == 200
        body = resp.text
        assert "env-setup-cta" in body
        assert "Open the full setup page" in body
        assert 'href="/setup"' in body
        # inline <details> preview block must no longer appear
        assert 'aria-label="Preview of the clipboard payload"' not in body

    def test_install_mcp_card_removed(self, web_client):
        """The stale 'Use with Claude Code / MCP' card on /setup has been
        removed — there is no Agnes-as-MCP-server today. The Atlassian
        MCP server registration step (Fix C in the 2026-05-10 init-report
        response) is registered FROM the setup script, not as a /setup-
        page card; that's an unrelated wiring direction.
        """
        resp = web_client.get("/setup")
        assert resp.status_code == 200
        body = resp.text
        assert "Use with Claude Code / MCP" not in body


class TestAdminRoleGuards:
    def test_analyst_cannot_access_admin_tables(self, web_client, admin_cookie, analyst_cookie):
        resp = web_client.get("/admin/tables", cookies=analyst_cookie)
        assert resp.status_code == 403

    def test_admin_can_access_admin_tables(self, web_client, admin_cookie):
        resp = web_client.get("/admin/tables", cookies=admin_cookie)
        assert resp.status_code == 200

    def test_analyst_cannot_access_admin_access_page(self, web_client, analyst_cookie):
        """The unified /admin/access page replaces the dropped
        /admin/permissions page. Non-admin must still be blocked."""
        resp = web_client.get("/admin/access", cookies=analyst_cookie)
        assert resp.status_code == 403

    def test_admin_can_access_admin_access_page(self, web_client, admin_cookie):
        resp = web_client.get("/admin/access", cookies=admin_cookie)
        assert resp.status_code == 200

    def test_analyst_cannot_access_corporate_memory_admin(self, web_client, admin_cookie, analyst_cookie):
        resp = web_client.get("/corporate-memory/admin", cookies=analyst_cookie)
        assert resp.status_code == 403

    def test_admin_agent_prompt_page_admin_only(self, web_client, admin_cookie, analyst_cookie):
        """The renamed Agent Setup Prompt page is gated by require_admin."""
        # Unauthenticated → 302 redirect to login
        r = web_client.get("/admin/agent-prompt", follow_redirects=False)
        assert r.status_code in (302, 401, 403)
        # Non-admin → 403
        r = web_client.get("/admin/agent-prompt", cookies=analyst_cookie, follow_redirects=False)
        assert r.status_code == 403
        # Admin → 200
        r = web_client.get("/admin/agent-prompt", cookies=admin_cookie, follow_redirects=False)
        assert r.status_code == 200

    def test_admin_scheduler_runs_page_admin_only(self, web_client, admin_cookie, analyst_cookie):
        """`/admin/scheduler-runs` collapsed into the unified Activity
        page as a `source=scheduler` filter. Route now 308-redirects;
        admin-only gate still applies before the redirect fires.
        """
        # Anonymous → not admin → 302 to login (require_admin runs first).
        r = web_client.get("/admin/scheduler-runs", follow_redirects=False)
        assert r.status_code in (302, 401, 403)
        # Analyst → 403 (require_admin fails before we hit the redirect).
        r = web_client.get("/admin/scheduler-runs", cookies=analyst_cookie, follow_redirects=False)
        assert r.status_code == 403
        # Admin → 308 to the unified page with the source filter pre-set.
        r = web_client.get("/admin/scheduler-runs", cookies=admin_cookie, follow_redirects=False)
        assert r.status_code == 308
        assert r.headers["location"] == "/admin/activity?source=scheduler"

    def test_profile_sessions_redirects_to_me_activity(self, web_client, analyst_cookie, admin_cookie):
        """/profile/sessions now 301-redirects to /me/activity?tab=sessions
        (consolidated in the /me/activity page)."""
        r = web_client.get("/profile/sessions", cookies=analyst_cookie, follow_redirects=False)
        assert r.status_code == 301
        assert r.headers["location"] == "/me/activity?tab=sessions"
        r = web_client.get("/profile/sessions", cookies=admin_cookie, follow_redirects=False)
        assert r.status_code == 301

    def test_profile_session_download_path_safety(self, web_client, analyst_cookie):
        """Per-session download endpoint must reject any filename that could
        escape the user's own session directory."""
        # NB: bare ".." is excluded — httpx normalises the URL to
        # /profile/sessions before sending, so it never reaches the
        # download handler. The %2F-encoded variant exercises the real
        # path-component value that does reach the handler.
        for bad in ["../etc/passwd", "subdir/file.jsonl", ".env",
                    "session.jsonl.bak", "..%2Fetc%2Fpasswd"]:
            r = web_client.get(f"/profile/sessions/{bad}", cookies=analyst_cookie, follow_redirects=False)
            assert r.status_code == 404, f"Expected 404 for {bad!r}, got {r.status_code}"
        # Unauthenticated → never the file
        r = web_client.get("/profile/sessions/anything.jsonl", follow_redirects=False)
        assert r.status_code in (302, 401, 403)

    def test_me_activity_page_renders(self, web_client, analyst_cookie):
        """/me/activity renders for authenticated users (consolidated view)."""
        r = web_client.get("/me/activity", cookies=analyst_cookie, follow_redirects=False)
        assert r.status_code == 200
        assert b"My activity" in r.content

    def test_me_activity_hero_renders_strong_email_unescaped(self, web_client, analyst_cookie):
        """Regression: the /me/activity hero subtitle embeds the user's email
        in <strong> tags. Building it via `~` concatenation with a Markup
        operand (`user.email | e`) made Jinja2's markup_join escape the
        literal tags too, so the page showed literal "<strong>...</strong>"
        text. The subtitle must render real <strong> HTML while still
        escaping the email itself."""
        r = web_client.get("/me/activity", cookies=analyst_cookie, follow_redirects=False)
        assert r.status_code == 200
        body = r.text
        assert "activity for <strong>analyst@test.com</strong>." in body
        assert "activity for &lt;strong&gt;" not in body

    def test_profile_session_download_returns_file_for_owner(self, web_client, analyst_cookie, tmp_path, monkeypatch):
        """Authenticated owner can fetch their own jsonl with proper Content-Disposition."""
        # The seeded analyst is "analyst1" (per conftest.seeded_app).
        user_sessions = tmp_path / "user_sessions" / "analyst1"
        user_sessions.mkdir(parents=True)
        sample = user_sessions / "abc-123.jsonl"
        sample.write_text('{"event": "test"}\n')
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        r = web_client.get("/profile/sessions/abc-123.jsonl", cookies=analyst_cookie, follow_redirects=False)
        assert r.status_code == 200
        assert r.headers.get("content-disposition", "").endswith('filename="abc-123.jsonl"')
        assert b'"event": "test"' in r.content


class TestUnauthenticatedHtmlRedirects:
    def test_dashboard_unauthenticated_redirects_to_login(self, web_client):
        resp = web_client.get("/dashboard", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"].startswith("/login")
        assert "next=%2Fdashboard" in resp.headers["location"]

    def test_catalog_unauthenticated_redirects_to_login(self, web_client):
        resp = web_client.get("/catalog", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"].startswith("/login")
        assert "next=%2Fcatalog" in resp.headers["location"]

    def test_api_route_still_returns_json_401(self, web_client):
        # /api/sync/manifest requires auth; must keep JSON 401 (no redirect).
        resp = web_client.get("/api/sync/manifest", follow_redirects=False)
        assert resp.status_code == 401
        assert resp.headers["content-type"].startswith("application/json")

    def test_password_login_honors_next(self, web_client, tmp_path):
        from argon2 import PasswordHasher
        from src.db import get_system_db
        from src.repositories.users import UserRepository
        password = "TestPass1!"
        conn = get_system_db()
        UserRepository(conn).create(
            id="u1", email="u1@test.com", name="U1",
            password_hash=PasswordHasher().hash(password),
        )
        conn.close()
        resp = web_client.post(
            "/auth/password/login/web",
            data={"email": "u1@test.com", "password": password, "next": "/catalog"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == "/catalog"

    def test_password_login_rejects_open_redirect(self, web_client, tmp_path):
        from argon2 import PasswordHasher
        from src.db import get_system_db
        from src.repositories.users import UserRepository
        password = "TestPass1!"
        conn = get_system_db()
        UserRepository(conn).create(
            id="u2", email="u2@test.com", name="U2",
            password_hash=PasswordHasher().hash(password),
        )
        conn.close()
        resp = web_client.post(
            "/auth/password/login/web",
            data={"email": "u2@test.com", "password": password, "next": "//evil.example/"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == "/dashboard"

    @pytest.mark.parametrize("hostile_next,expected_location", [
        ("javascript:alert(1)", "/dashboard"),
        ("http://evil.example/", "/dashboard"),
        ("//evil.example/", "/dashboard"),
        ("dashboard", "/dashboard"),           # missing leading slash
        ("/foo?bar=baz", "/foo?bar=baz"),       # valid same-origin with query
    ])
    def test_password_login_sanitizes_next(self, web_client, tmp_path, hostile_next, expected_location):
        from argon2 import PasswordHasher
        from src.db import get_system_db
        from src.repositories.users import UserRepository
        import uuid
        password = "TestPass1!"
        uid = f"u-{uuid.uuid4().hex[:8]}"
        conn = get_system_db()
        UserRepository(conn).create(
            id=uid, email=f"{uid}@test.com", name=uid,
            password_hash=PasswordHasher().hash(password),
        )
        conn.close()
        resp = web_client.post(
            "/auth/password/login/web",
            data={"email": f"{uid}@test.com", "password": password, "next": hostile_next},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == expected_location

    def test_non_api_post_still_returns_json_401(self, web_client):
        # POST to a JSON auth endpoint that lives outside /api/ — must NOT be redirected.
        resp = web_client.post("/auth/token", json={"email": "nope@x.com", "password": "wrong"},
                               follow_redirects=False)
        assert resp.status_code == 401
        assert resp.headers["content-type"].startswith("application/json")

    def test_auth_json_get_still_returns_json_401(self, web_client):
        # GET to a JSON endpoint under /auth/* (e.g. PAT CRUD) — must NOT be redirected,
        # so CLI clients calling api_get("/auth/tokens") get JSON they can parse.
        resp = web_client.get("/auth/tokens", follow_redirects=False)
        assert resp.status_code == 401
        assert resp.headers["content-type"].startswith("application/json")

    def test_login_page_propagates_next_to_password_button(self, web_client):
        resp = web_client.get("/login?next=/catalog")
        assert resp.status_code == 200
        body = resp.text
        # Password button URL should carry next.
        assert "/login/password?next=%2Fcatalog" in body, \
            f"Expected /login/password?next=%2Fcatalog in login page HTML; got snippet: {body[:500]}"

    def test_login_page_propagates_next_to_google_button(self, web_client, monkeypatch):
        """The Google OAuth button URL must also carry the ?next param so the
        post-login redirect honors the requested destination."""
        # Force Google provider to appear available so the button is rendered.
        monkeypatch.setattr(
            "app.auth.providers.google.is_available", lambda: True,
        )
        resp = web_client.get("/login?next=/catalog")
        assert resp.status_code == 200
        body = resp.text
        assert "/auth/google/login?next=%2Fcatalog" in body, \
            f"Expected google login URL with ?next in login page; snippet: {body[:800]}"

    def test_login_email_page_extracts_and_renders_next(self, web_client):
        """/login/email (magic link) must extract ?next from the URL and
        emit it into the hidden form field so it round-trips to the POST."""
        resp = web_client.get("/login/email?next=/catalog")
        assert resp.status_code == 200
        body = resp.text
        # The template renders <input type="hidden" name="next" value="/catalog">
        assert 'name="next" value="/catalog"' in body, \
            f"Expected /catalog in next hidden field; snippet: {body[:800]}"

    def test_login_email_page_rejects_open_redirect_in_next(self, web_client):
        """Hostile ?next values (e.g. //evil) must be sanitized away before
        the hidden field is rendered."""
        resp = web_client.get("/login/email?next=//evil.example/")
        assert resp.status_code == 200
        body = resp.text
        assert "evil.example" not in body
        # Empty string is the sanitized default.
        assert 'name="next" value=""' in body

    def test_google_login_stashes_safe_next_in_session(self, web_client, monkeypatch):
        """google_login() must stash the sanitized next_path in the session.

        We can't exercise the full OAuth flow without a Google mock, but we
        can verify the helper applies the sanitizer correctly."""
        from app.auth._common import safe_next_path
        # Valid same-origin paths pass through.
        assert safe_next_path("/catalog") == "/catalog"
        assert safe_next_path("/foo?bar=baz") == "/foo?bar=baz"
        # Open-redirect shapes get defaulted.
        assert safe_next_path("//evil.example/") == "/dashboard"
        assert safe_next_path("http://evil.example/") == "/dashboard"
        assert safe_next_path("javascript:alert(1)") == "/dashboard"
        assert safe_next_path("") == "/dashboard"
        assert safe_next_path(None) == "/dashboard"
        # Empty-default variant (used when computing query string).
        assert safe_next_path(None, default="") == ""
