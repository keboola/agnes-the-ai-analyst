"""Tests for auth providers — password, email magic link, google OAuth."""

import os
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")

    from app.main import create_app
    from src.db import get_system_db
    from src.repositories.users import UserRepository

    conn = get_system_db()
    ur = UserRepository(conn)
    # User with password
    try:
        from argon2 import PasswordHasher
        ph = PasswordHasher()
        pw_hash = ph.hash("testpass123")
    except ImportError:
        import hashlib
        pw_hash = hashlib.sha256(b"testpass123").hexdigest()

    ur.create(id="pw1", email="pw@test.com", name="PW User", role="analyst", password_hash=pw_hash)
    # User with setup token (and fresh created timestamp so the JSON /setup
    # endpoint's TTL check accepts it)
    from datetime import datetime, timezone
    ur.create(id="setup1", email="setup@test.com", name="Setup User", role="analyst")
    ur.update(id="setup1", setup_token="setup-token-123",
              setup_token_created=datetime.now(timezone.utc))
    # User for magic link
    ur.create(id="ml1", email="ml@test.com", name="ML User", role="analyst")
    conn.close()

    app = create_app()
    return TestClient(app)


class TestTokenEndpoint:
    """Tests for /auth/token — password bypass fix."""

    def test_token_empty_password_rejected_when_user_has_hash(self, client):
        """Empty password must be rejected when user has password_hash."""
        resp = client.post("/auth/token", json={"email": "pw@test.com", "password": ""})
        assert resp.status_code == 401

    def test_token_missing_password_rejected_when_user_has_hash(self, client):
        """Omitting password field (defaults to '') must be rejected when user has password_hash."""
        resp = client.post("/auth/token", json={"email": "pw@test.com"})
        assert resp.status_code == 401

    def test_token_wrong_password_rejected(self, client):
        """Wrong password must be rejected with 401."""
        resp = client.post("/auth/token", json={"email": "pw@test.com", "password": "wrongpass"})
        assert resp.status_code == 401

    def test_token_correct_password_succeeds(self, client):
        """Correct password must issue a token."""
        resp = client.post("/auth/token", json={"email": "pw@test.com", "password": "testpass123"})
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["email"] == "pw@test.com"

    def test_token_no_password_hash_user_gets_token(self, client):
        """User without password_hash (OAuth-only) must be rejected at /auth/token."""
        resp = client.post("/auth/token", json={"email": "ml@test.com"})
        assert resp.status_code == 401

    def test_token_rejected_for_oauth_only_user(self, client):
        """OAuth-only user (no password_hash) must not receive a token via /auth/token."""
        resp = client.post("/auth/token", json={"email": "ml@test.com"})
        assert resp.status_code == 401
        assert "external authentication" in resp.json()["detail"]

    def test_token_unknown_user_rejected(self, client):
        """Unknown email must return 401."""
        resp = client.post("/auth/token", json={"email": "nobody@test.com", "password": "anything"})
        assert resp.status_code == 401


class TestPasswordAuth:
    def test_login_success(self, client):
        resp = client.post("/auth/password/login", json={
            "email": "pw@test.com", "password": "testpass123",
        })
        assert resp.status_code == 200
        assert "access_token" in resp.json()

    def test_login_wrong_password(self, client):
        resp = client.post("/auth/password/login", json={
            "email": "pw@test.com", "password": "wrongpass",
        })
        assert resp.status_code == 401

    def test_login_unknown_user(self, client):
        resp = client.post("/auth/password/login", json={
            "email": "unknown@test.com", "password": "test",
        })
        assert resp.status_code == 401

    def test_setup_password(self, client):
        resp = client.post("/auth/password/setup", json={
            "email": "setup@test.com", "token": "setup-token-123", "password": "newpass456",
        })
        assert resp.status_code == 200
        assert "access_token" in resp.json()

    def test_setup_wrong_token(self, client):
        resp = client.post("/auth/password/setup", json={
            "email": "setup@test.com", "token": "wrong-token", "password": "newpass",
        })
        assert resp.status_code == 400


class TestEmailAuth:
    def test_send_link_registered(self, client):
        resp = client.post("/auth/email/send-link", json={"email": "ml@test.com"})
        assert resp.status_code == 200
        # Always returns same message (anti-enumeration)
        assert "If this email" in resp.json()["message"]

    def test_send_link_unregistered(self, client):
        resp = client.post("/auth/email/send-link", json={"email": "nobody@test.com"})
        assert resp.status_code == 200
        assert "If this email" in resp.json()["message"]

    def test_verify_invalid_token(self, client):
        resp = client.post("/auth/email/verify", json={
            "email": "ml@test.com", "token": "invalid",
        })
        assert resp.status_code == 401


class TestGoogleOAuth:
    def test_google_login_not_configured(self, client):
        """Without GOOGLE_CLIENT_ID, should redirect to login with error."""
        resp = client.get("/auth/google/login", follow_redirects=False)
        assert resp.status_code == 302 or resp.status_code == 307
        assert "error" in resp.headers.get("location", "")


class TestGoogleGroupsFetch:
    """Unit tests for _fetch_google_groups — the helper must be tolerant of
    every realistic failure mode (non-Workspace tenants return 403, expired
    tokens return 401, network errors bubble from httpx) and never raise."""

    def test_parses_groups_from_success_response(self, monkeypatch):
        import asyncio
        from app.auth.providers import google as gp

        fake_payload = {
            "groups": [
                {
                    "name": "groups/abc123",
                    "groupKey": {"id": "team-eng@example.com"},
                    "displayName": "Engineering",
                },
                {
                    "name": "groups/def456",
                    "groupKey": {"id": "everyone@example.com"},
                    # No displayName — falls back to id
                },
            ],
        }

        class _Resp:
            status_code = 200
            text = ""
            def json(self):
                return fake_payload

        class _FakeClient:
            def __init__(self, *a, **kw):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def get(self, url, params=None, headers=None):
                return _Resp()

        monkeypatch.setattr(gp.httpx, "AsyncClient", _FakeClient)

        groups = asyncio.run(gp._fetch_google_groups("fake-token", "user@example.com"))
        assert groups == [
            {"id": "team-eng@example.com", "name": "Engineering"},
            {"id": "everyone@example.com", "name": "everyone@example.com"},
        ]

    def test_returns_empty_on_403(self, monkeypatch):
        """Cloud Identity not enabled (non-Workspace tenant) → 403 → [] + warning."""
        import asyncio
        from app.auth.providers import google as gp

        class _Resp:
            status_code = 403
            text = "Cloud Identity API has not been enabled"

        class _FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, url, params=None, headers=None):
                return _Resp()

        monkeypatch.setattr(gp.httpx, "AsyncClient", _FakeClient)

        groups = asyncio.run(gp._fetch_google_groups("fake-token", "user@example.com"))
        assert groups == []

    def test_returns_empty_on_exception(self, monkeypatch):
        """Network error inside httpx must be swallowed, not propagated."""
        import asyncio
        from app.auth.providers import google as gp

        class _FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **kw):
                raise RuntimeError("boom")

        monkeypatch.setattr(gp.httpx, "AsyncClient", _FakeClient)

        groups = asyncio.run(gp._fetch_google_groups("fake-token", "user@example.com"))
        assert groups == []


class TestCookieAuth:
    def test_web_ui_with_cookie(self, client):
        """Test that web UI routes accept JWT from cookie."""
        from app.auth.jwt import create_access_token
        from src.db import get_system_db
        from src.repositories.users import UserRepository

        conn = get_system_db()
        ur = UserRepository(conn)
        # Use existing user
        user = ur.get_by_email("pw@test.com")
        conn.close()

        token = create_access_token(user["id"], user["email"], user["role"])
        # Set cookie and access dashboard
        client.cookies.set("access_token", token)
        resp = client.get("/dashboard")
        # Should not be 401 — cookie auth works
        assert resp.status_code != 401
