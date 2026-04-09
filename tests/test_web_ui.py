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
