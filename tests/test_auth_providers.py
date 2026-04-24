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


class TestGoogleCallbackGroupSync:
    """Google OAuth callback populates users.groups from Workspace.

    The real google.py module captures GOOGLE_CLIENT_ID/SECRET at import
    time and conditionally registers `oauth.google`. For tests we:
      1. Patch `is_available` so the callback's early-return guard doesn't fire
      2. Stub `oauth.google.authorize_access_token` with an AsyncMock
      3. Stub `fetch_user_groups` at the import site (app.auth.providers.google)
         to return a fixed list — no real Google traffic
    """

    @pytest.fixture
    def google_app(self, tmp_path, monkeypatch):
        import json as _json
        from unittest.mock import AsyncMock
        from types import SimpleNamespace

        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")

        from app.main import create_app
        import app.auth.providers.google as g_mod

        # (1) bypass the is_available guard
        monkeypatch.setattr(g_mod, "is_available", lambda: True)

        # (2) fake oauth.google with async authorize_access_token
        fake_oauth_google = SimpleNamespace(
            authorize_access_token=AsyncMock(
                return_value={
                    "userinfo": {
                        "email": "tester@groupon.com",
                        "name": "Tester",
                    }
                }
            )
        )
        monkeypatch.setattr(g_mod.oauth, "google", fake_oauth_google, raising=False)

        # (3) fake fetch_user_groups — also patches the import inside
        # google_callback because it does `from app.auth.group_sync import fetch_user_groups`
        # inside the function body, so patching the source module is enough.
        import app.auth.group_sync as gs_mod
        monkeypatch.setattr(
            gs_mod,
            "fetch_user_groups",
            lambda email: ["grp_a@groupon.com", "grp_b@groupon.com"],
        )

        app = create_app()
        client = TestClient(app, follow_redirects=False)
        return {"client": client, "json": _json}

    def test_callback_creates_user_with_groups(self, google_app):
        """First-time login → user row + groups populated + two user_groups rows."""
        c = google_app["client"]
        _json = google_app["json"]

        resp = c.get("/auth/google/callback?code=x&state=y")
        assert resp.status_code == 302
        assert resp.headers["location"] == "/dashboard"
        # access_token cookie set
        assert "access_token" in resp.cookies

        from src.db import get_system_db
        from src.repositories.users import UserRepository
        from src.repositories.plugin_access import UserGroupsRepository

        conn = get_system_db()
        try:
            user = UserRepository(conn).get_by_email("tester@groupon.com")
            assert user is not None
            assert user["role"] == "analyst"
            assert _json.loads(user["groups"]) == [
                "grp_a@groupon.com",
                "grp_b@groupon.com",
            ]
            names = {g["name"] for g in UserGroupsRepository(conn).list_all()}
            assert "grp_a@groupon.com" in names
            assert "grp_b@groupon.com" in names
            # non-system flag
            row = UserGroupsRepository(conn).get_by_name("grp_a@groupon.com")
            assert row["is_system"] is False
            assert row["created_by"] == "system:google-sync"
        finally:
            conn.close()

    def test_callback_updates_groups_on_relogin(self, google_app, monkeypatch):
        """Second login with a different group set overwrites the first."""
        c = google_app["client"]
        _json = google_app["json"]

        # First login — default stub returns [a, b]
        c.get("/auth/google/callback?code=x&state=y")

        # Swap the mock to return a single, different group on the next call
        import app.auth.group_sync as gs_mod
        monkeypatch.setattr(
            gs_mod, "fetch_user_groups", lambda email: ["grp_c@groupon.com"]
        )

        c.get("/auth/google/callback?code=x&state=y")

        from src.db import get_system_db
        from src.repositories.users import UserRepository

        conn = get_system_db()
        try:
            user = UserRepository(conn).get_by_email("tester@groupon.com")
            assert _json.loads(user["groups"]) == ["grp_c@groupon.com"]
        finally:
            conn.close()

    def test_callback_fails_soft_on_group_sync_exception(self, google_app, monkeypatch):
        """An exception inside fetch_user_groups does not block the login."""
        c = google_app["client"]
        _json = google_app["json"]

        def raise_boom(email):
            raise RuntimeError("Google API is down")

        import app.auth.group_sync as gs_mod
        monkeypatch.setattr(gs_mod, "fetch_user_groups", raise_boom)

        resp = c.get("/auth/google/callback?code=x&state=y")
        # Login still proceeds, redirect to dashboard with token cookie
        assert resp.status_code == 302
        assert resp.headers["location"] == "/dashboard"
        assert "access_token" in resp.cookies

        from src.db import get_system_db
        from src.repositories.users import UserRepository

        conn = get_system_db()
        try:
            user = UserRepository(conn).get_by_email("tester@groupon.com")
            assert user is not None
            # groups stays NULL (no previous value either)
            assert user["groups"] is None
        finally:
            conn.close()

    def test_callback_empty_groups_does_not_overwrite_existing(self, google_app, monkeypatch):
        """fetch_user_groups returning [] means 'no data' — don't wipe existing
           groups on a transient failure masked as empty."""
        c = google_app["client"]
        _json = google_app["json"]

        # First login populates groups
        c.get("/auth/google/callback?code=x&state=y")

        # Second login: Google returns empty
        import app.auth.group_sync as gs_mod
        monkeypatch.setattr(gs_mod, "fetch_user_groups", lambda email: [])
        c.get("/auth/google/callback?code=x&state=y")

        from src.db import get_system_db
        from src.repositories.users import UserRepository

        conn = get_system_db()
        try:
            user = UserRepository(conn).get_by_email("tester@groupon.com")
            # Previous groups preserved
            assert _json.loads(user["groups"]) == [
                "grp_a@groupon.com",
                "grp_b@groupon.com",
            ]
        finally:
            conn.close()
