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
    """Client with one existing seed user (no password_hash — like SEED_ADMIN_EMAIL seeding)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
    from app.main import create_app
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    conn = get_system_db()
    UserRepository(conn).create(id="existing", email="existing@test.com", name="E", role="admin")
    conn.close()
    return TestClient(create_app())


@pytest.fixture
def password_user_client(tmp_path, monkeypatch):
    """Client with a user who already has a password set — bootstrap must be disabled."""
    from argon2 import PasswordHasher
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
    from app.main import create_app
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    conn = get_system_db()
    UserRepository(conn).create(
        id="existing",
        email="existing@test.com",
        name="E",
        role="admin",
        password_hash=PasswordHasher().hash("pre-existing-pass"),
    )
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

    def test_bootstrap_activates_seed_user(self, seeded_client):
        """Bootstrap activates a password-less seed user (SEED_ADMIN_EMAIL scenario)."""
        resp = seeded_client.post("/auth/bootstrap", json={
            "email": "existing@test.com",
            "password": "newpass123",
        })
        assert resp.status_code == 200
        assert resp.json()["role"] == "admin"

        # Login now works
        login = seeded_client.post("/auth/password/login", json={
            "email": "existing@test.com",
            "password": "newpass123",
        })
        assert login.status_code == 200

    def test_bootstrap_disabled_when_password_user_exists(self, password_user_client):
        """Bootstrap fails with 403 when any user already has a password set."""
        resp = password_user_client.post("/auth/bootstrap", json={
            "email": "hacker@evil.com",
            "password": "should-not-work",
        })
        assert resp.status_code == 403
        assert "already have passwords" in resp.json()["detail"]

    def test_bootstrap_rejects_new_email_when_seed_exists(self, seeded_client):
        """Seed admin (passwordless) exists → attacker must not be able to
        register a brand-new admin account for a different email. They have
        to activate the existing seed instead, which requires knowing its
        email — and the endpoint itself must not disclose that email
        (see test_bootstrap_does_not_leak_seed_email_in_rejection below)."""
        resp = seeded_client.post("/auth/bootstrap", json={
            "email": "hacker@evil.com",
            "password": "takeover",
        })
        assert resp.status_code == 403
        detail = resp.json()["detail"].lower()
        assert "without a password" in detail

    def test_bootstrap_does_not_leak_seed_email_in_rejection(self, seeded_client):
        """/auth/bootstrap is unauthenticated. Listing the existing seed
        email in the 403 body would let an attacker probe once to discover
        the email, then bootstrap again with that email + their own
        password — a full takeover in two unauthenticated requests.

        The rejection body must stay generic; operators who need to know
        which seed exists use `da admin users list` (authenticated) or
        the audit log."""
        resp = seeded_client.post("/auth/bootstrap", json={
            "email": "hacker@evil.com",
            "password": "takeover",
        })
        assert resp.status_code == 403
        body = resp.text  # full response body, not just detail
        assert "existing@test.com" not in body
        assert "@test.com" not in body
        # Sanity: the generic guidance is still there.
        assert "without a password" in body.lower()

    def test_bootstrap_still_activates_matching_seed_when_seed_exists(self, seeded_client):
        """The legitimate path — bootstrap with the same email as the seed —
        keeps working. Covered by test_bootstrap_activates_seed_user; this
        is the adversarial sibling (above) confirming the allow-list stance."""
        resp = seeded_client.post("/auth/bootstrap", json={
            "email": "existing@test.com",
            "password": "legitimate-operator-pw",
        })
        assert resp.status_code == 200
        assert resp.json()["role"] == "admin"

    def test_bootstrap_then_login(self, fresh_client):
        """After bootstrap with password, /auth/token login works; without password it requires OAuth."""
        # Bootstrap with a password
        fresh_client.post("/auth/bootstrap", json={
            "email": "admin@test.com",
            "password": "adminpass123",
        })

        # Normal password login succeeds
        resp = fresh_client.post("/auth/token", json={
            "email": "admin@test.com",
            "password": "adminpass123",
        })
        assert resp.status_code == 200
        assert resp.json()["role"] == "admin"

    def test_bootstrap_no_password_token_rejected(self, fresh_client):
        """After passwordless bootstrap, /auth/token must reject the user (OAuth-only flow)."""
        fresh_client.post("/auth/bootstrap", json={
            "email": "admin@test.com",
        })

        resp = fresh_client.post("/auth/token", json={
            "email": "admin@test.com",
        })
        assert resp.status_code == 401

    def test_bootstrap_second_call_fails_once_password_set(self, fresh_client):
        """Endpoint self-deactivates once any user has a password."""
        # First call WITH password — locks bootstrap
        fresh_client.post("/auth/bootstrap", json={
            "email": "admin@test.com",
            "password": "realpass123",
        })

        # Any subsequent bootstrap attempt fails
        resp = fresh_client.post("/auth/bootstrap", json={
            "email": "second@test.com",
            "password": "other-pass",
        })
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
