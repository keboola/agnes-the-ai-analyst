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
        # Nav: "My tokens" (own) is in the user-menu dropdown; admin Tokens
        # entry (and Tables, Users, Groups, Resource access, Server config)
        # lives in the Admin dropdown.
        assert 'href="/tokens"' in body
        assert 'href="/admin/tokens"' in body
        assert 'href="/profile"' in body
        assert 'href="/admin/users"' in body
        # v12 modern UI markers — Role column was replaced by Groups chips,
        # so role-pill is gone. Confirm-modal pattern is shared by both.
        assert 'class="users-page"' in body
        assert 'id="confirm-modal"' in body

    def test_nav_shows_tokens_link_for_non_admin(self, web_client, analyst_cookie):
        """Non-admins see 'My tokens' + 'Profile' user-menu links — no admin Tokens entry."""
        resp = web_client.get("/dashboard", cookies=analyst_cookie)
        assert resp.status_code in (200, 302)
        if resp.status_code == 302:
            # Dashboard may redirect in some flows; follow it for nav check.
            resp = web_client.get(resp.headers["location"], cookies=analyst_cookie)
        body = resp.text
        assert 'href="/tokens"' in body
        assert 'href="/profile"' in body
        assert ">My tokens<" in body
        assert ">Profile<" in body
        # Non-admins must NOT see the admin Tokens link inside the Admin dropdown.
        assert 'href="/admin/tokens"' not in body

    def test_nav_shows_all_tokens_link_for_admin(self, web_client, admin_cookie):
        """Admins see the 'My tokens' user-menu link and the admin Tokens entry inside the Admin dropdown."""
        resp = web_client.get("/dashboard", cookies=admin_cookie)
        assert resp.status_code in (200, 302)
        if resp.status_code == 302:
            resp = web_client.get(resp.headers["location"], cookies=admin_cookie)
        body = resp.text
        assert 'href="/tokens"' in body
        assert 'href="/admin/tokens"' in body
        assert ">My tokens<" in body
        # Admin dropdown now lists Tables / Tokens / Users / Groups / Resource access / Server config.
        assert 'href="/admin/tables"' in body
        assert ">Tables<" in body
        assert ">Tokens<" in body

    def test_profile_renders_account_details(self, web_client, admin_cookie):
        """/profile renders a real profile page with email + tokens link.

        v12 changes: role-pill is replaced by an Admin-pill driven by Admin
        user_group membership; ``session.google_groups`` is gone (the
        OAuth callback writes Workspace memberships into
        ``user_group_members`` instead), so the "No Google groups available"
        empty state is no longer rendered.
        """
        resp = web_client.get("/profile", cookies=admin_cookie)
        assert resp.status_code == 200
        body = resp.text
        assert "admin@test.com" in body
        assert 'href="/tokens"' in body

    def test_profile_requires_auth(self, web_client):
        """/profile requires auth (was a 302 back-compat redirect before)."""
        resp = web_client.get("/profile", follow_redirects=False)
        # Auth dep raises 401; some configs may redirect to /login — accept either.
        assert resp.status_code in (401, 302)

    @pytest.mark.skip(
        reason=(
            "v12: /profile no longer renders an admin-self-management link. "
            "Admin can navigate to /admin/users/{id} from the top-nav Admin "
            "dropdown directly. Drop or rewrite this test once the profile "
            "page settles."
        )
    )
    def test_profile_shows_admin_detail_link_for_admin(self, web_client, admin_cookie):
        resp = web_client.get("/profile", cookies=admin_cookie)
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
        resp = web_client.get("/profile", cookies=analyst_cookie)
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
        resp = web_client.get("/profile", cookies=analyst_cookie)
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
        # New numbered headers + da diagnose step
        assert "1) Install the CLI" in body
        assert "4) Run diagnostics" in body
        assert "da diagnose" in body
        assert "da auth whoami" in body

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
        removed — there is no Agnes MCP server today.
        """
        resp = web_client.get("/setup")
        assert resp.status_code == 200
        body = resp.text
        assert "Use with Claude Code / MCP" not in body
        assert "MCP" not in body


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
