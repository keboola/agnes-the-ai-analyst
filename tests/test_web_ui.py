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
    password = "AdminPass1!"
    password_hash = PasswordHasher().hash(password)
    conn = get_system_db()
    UserRepository(conn).create(
        id="admin1", email="admin@test.com", name="Admin", role="admin",
        password_hash=password_hash,
    )
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
        id="analyst1", email="analyst@test.com", name="Analyst", role="analyst",
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

    def test_admin_permissions(self, web_client, admin_cookie):
        resp = web_client.get("/admin/permissions", cookies=admin_cookie)
        if resp.status_code == 404:
            pytest.skip("Route /admin/permissions does not exist")
        assert resp.status_code == 200


class TestAdminRoleGuards:
    def test_analyst_cannot_access_admin_tables(self, web_client, admin_cookie, analyst_cookie):
        resp = web_client.get("/admin/tables", cookies=analyst_cookie)
        assert resp.status_code == 403

    def test_analyst_cannot_access_admin_permissions(self, web_client, admin_cookie, analyst_cookie):
        resp = web_client.get("/admin/permissions", cookies=analyst_cookie)
        assert resp.status_code == 403

    def test_admin_can_access_admin_tables(self, web_client, admin_cookie):
        resp = web_client.get("/admin/tables", cookies=admin_cookie)
        assert resp.status_code == 200

    def test_admin_can_access_admin_permissions(self, web_client, admin_cookie):
        resp = web_client.get("/admin/permissions", cookies=admin_cookie)
        assert resp.status_code == 200

    def test_analyst_cannot_access_corporate_memory_admin(self, web_client, admin_cookie, analyst_cookie):
        resp = web_client.get("/corporate-memory/admin", cookies=analyst_cookie)
        assert resp.status_code == 403


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
            id="u1", email="u1@test.com", name="U1", role="admin",
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
            id="u2", email="u2@test.com", name="U2", role="admin",
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
            id=uid, email=f"{uid}@test.com", name=uid, role="admin",
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
