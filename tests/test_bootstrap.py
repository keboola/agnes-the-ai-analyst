"""Tests for bootstrap endpoint — first admin user creation."""

import os
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def fresh_client(tmp_path, monkeypatch):
    """Client with EMPTY database — no users."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
    from app.main import create_app
    app = create_app()
    return TestClient(app)


@pytest.fixture
def seeded_client(tmp_path, monkeypatch):
    """Client with one existing user."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
    from app.main import create_app
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    conn = get_system_db()
    UserRepository(conn).create(id="existing", email="existing@test.com", name="E", role="admin")
    conn.close()
    return TestClient(create_app())


class TestBootstrap:
    def test_bootstrap_on_empty_db(self, fresh_client):
        """First call creates admin and returns token."""
        resp = fresh_client.post("/auth/bootstrap", json={
            "email": "admin@test.com",
            "name": "Admin",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == "admin@test.com"
        assert data["role"] == "admin"
        assert "access_token" in data

    def test_bootstrap_with_password(self, fresh_client):
        """Bootstrap with password sets password hash."""
        resp = fresh_client.post("/auth/bootstrap", json={
            "email": "admin@test.com",
            "password": "securepass123",
        })
        assert resp.status_code == 200

        # Token works
        token = resp.json()["access_token"]
        resp2 = fresh_client.get("/api/health")
        assert resp2.status_code == 200

    def test_bootstrap_disabled_when_users_exist(self, seeded_client):
        """Bootstrap fails with 403 when users already exist."""
        resp = seeded_client.post("/auth/bootstrap", json={
            "email": "hacker@evil.com",
        })
        assert resp.status_code == 403
        assert "already exist" in resp.json()["detail"]

    def test_bootstrap_then_login(self, fresh_client):
        """After bootstrap, normal /auth/token login works."""
        # Bootstrap
        fresh_client.post("/auth/bootstrap", json={
            "email": "admin@test.com",
        })

        # Normal login
        resp = fresh_client.post("/auth/token", json={
            "email": "admin@test.com",
        })
        assert resp.status_code == 200
        assert resp.json()["role"] == "admin"

    def test_bootstrap_second_call_fails(self, fresh_client):
        """Second bootstrap call fails — endpoint self-deactivates."""
        fresh_client.post("/auth/bootstrap", json={"email": "admin@test.com"})

        resp = fresh_client.post("/auth/bootstrap", json={"email": "second@test.com"})
        assert resp.status_code == 403

    def test_full_agent_flow(self, fresh_client):
        """Simulate full AI agent deployment flow."""
        # 1. Health check (no auth)
        resp = fresh_client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "healthy"

        # 2. Bootstrap admin
        resp = fresh_client.post("/auth/bootstrap", json={
            "email": "agent@company.com", "name": "AI Agent",
        })
        assert resp.status_code == 200
        token = resp.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 3. Check manifest (empty, no data yet)
        resp = fresh_client.get("/api/sync/manifest", headers=headers)
        assert resp.status_code == 200
        assert len(resp.json()["tables"]) == 0

        # 4. List users
        resp = fresh_client.get("/api/users", headers=headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 1

        # 5. Add analyst user
        resp = fresh_client.post("/api/users", json={
            "email": "analyst@company.com", "name": "Analyst",
        }, headers=headers)
        assert resp.status_code == 201

        # 6. Verify
        resp = fresh_client.get("/api/health")
        assert resp.json()["services"]["users"]["count"] == 2
