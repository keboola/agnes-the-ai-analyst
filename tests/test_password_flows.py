"""Tests for password reset + setup web flows (closes #34)."""

from __future__ import annotations

import tempfile
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        from src.db import close_system_db
        close_system_db()
        yield tmp
        close_system_db()


@pytest.fixture
def app_client(fresh_db, monkeypatch):
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
    from app.main import app
    return TestClient(app, follow_redirects=False)


def _seed_user(email: str, *, password_hash: str | None = None, setup_token: str | None = None,
               setup_token_created: datetime | None = None, reset_token: str | None = None,
               reset_token_created: datetime | None = None, role: str = "analyst") -> str:
    """Create a user, return its id."""
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    uid = str(uuid.uuid4())
    conn = get_system_db()
    try:
        repo = UserRepository(conn)
        repo.create(id=uid, email=email, name=email.split("@")[0], role=role,
                    password_hash=password_hash)
        updates: dict = {}
        if setup_token is not None:
            updates["setup_token"] = setup_token
        if setup_token_created is not None:
            updates["setup_token_created"] = setup_token_created
        if reset_token is not None:
            updates["reset_token"] = reset_token
        if reset_token_created is not None:
            updates["reset_token_created"] = reset_token_created
        if updates:
            repo.update(id=uid, **updates)
        return uid
    finally:
        conn.close()


def _get_user(email: str) -> dict:
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    conn = get_system_db()
    try:
        return UserRepository(conn).get_by_email(email)
    finally:
        conn.close()


def _seed_admin() -> str:
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token
    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="admin@test", name="Admin", role="admin")
        return create_access_token(user_id=uid, email="admin@test", role="admin")
    finally:
        conn.close()


# ---- GET pages ----

class TestResetGet:
    def test_renders_form_with_params(self, app_client, fresh_db):
        _seed_user("reset-me@test.com")
        resp = app_client.get("/auth/password/reset", params={"email": "reset-me@test.com", "token": "abc"})
        assert resp.status_code == 200
        assert "Reset Your Password" in resp.text
        # Hidden inputs with email + token are rendered
        assert 'name="email"' in resp.text
        assert 'value="reset-me@test.com"' in resp.text
        assert 'value="abc"' in resp.text

    def test_redirects_without_params(self, app_client, fresh_db):
        resp = app_client.get("/auth/password/reset")
        assert resp.status_code == 302
        assert resp.headers["location"].endswith("/login/password")


class TestSetupGet:
    def test_renders_form_with_params(self, app_client, fresh_db):
        _seed_user("new@test.com")
        resp = app_client.get("/auth/password/setup", params={"email": "new@test.com", "token": "xyz"})
        assert resp.status_code == 200
        assert "Set Up Your Account" in resp.text
        assert 'value="new@test.com"' in resp.text
        assert 'value="xyz"' in resp.text

    def test_redirects_without_params(self, app_client, fresh_db):
        resp = app_client.get("/auth/password/setup")
        assert resp.status_code == 302


# ---- POST /auth/password/reset (request) ----

class TestResetRequest:
    def test_issues_token_for_existing_user(self, app_client, fresh_db):
        _seed_user("forgot@test.com", password_hash="argon2_placeholder")
        resp = app_client.post("/auth/password/reset", data={"email": "forgot@test.com"})
        assert resp.status_code == 200
        assert "Check your email" in resp.text
        u = _get_user("forgot@test.com")
        assert u["reset_token"]  # token was stored

    def test_unknown_email_same_response(self, app_client, fresh_db):
        # Anti-enumeration: should not reveal whether email is registered.
        resp = app_client.post("/auth/password/reset", data={"email": "ghost@test.com"})
        assert resp.status_code == 200
        assert "Check your email" in resp.text

    def test_empty_email_same_response(self, app_client, fresh_db):
        resp = app_client.post("/auth/password/reset", data={"email": ""})
        assert resp.status_code == 200


# ---- POST /auth/password/reset/confirm ----

class TestResetConfirm:
    def test_valid_token_sets_password_and_redirects(self, app_client, fresh_db):
        from argon2 import PasswordHasher
        uid = _seed_user(
            "r1@test.com",
            password_hash=PasswordHasher().hash("oldpass123"),
            reset_token="tok-valid",
            reset_token_created=datetime.now(timezone.utc),
        )
        resp = app_client.post("/auth/password/reset/confirm", data={
            "email": "r1@test.com", "token": "tok-valid",
            "password": "brand-new-pwd", "confirm_password": "brand-new-pwd",
        })
        assert resp.status_code == 302
        assert "password_reset" in resp.headers["location"]
        u = _get_user("r1@test.com")
        assert u["reset_token"] is None
        # New password must verify
        PasswordHasher().verify(u["password_hash"], "brand-new-pwd")

    def test_wrong_token_renders_error(self, app_client, fresh_db):
        _seed_user("r2@test.com",
                   reset_token="tok-correct",
                   reset_token_created=datetime.now(timezone.utc))
        resp = app_client.post("/auth/password/reset/confirm", data={
            "email": "r2@test.com", "token": "tok-WRONG",
            "password": "abcdefgh", "confirm_password": "abcdefgh",
        })
        assert resp.status_code == 200
        assert "Invalid or expired" in resp.text

    def test_expired_token_rejected(self, app_client, fresh_db):
        _seed_user("r3@test.com",
                   reset_token="old",
                   reset_token_created=datetime.now(timezone.utc) - timedelta(days=2))
        resp = app_client.post("/auth/password/reset/confirm", data={
            "email": "r3@test.com", "token": "old",
            "password": "abcdefgh", "confirm_password": "abcdefgh",
        })
        assert resp.status_code == 200
        assert "expired" in resp.text.lower()

    def test_password_mismatch(self, app_client, fresh_db):
        _seed_user("r4@test.com",
                   reset_token="t",
                   reset_token_created=datetime.now(timezone.utc))
        resp = app_client.post("/auth/password/reset/confirm", data={
            "email": "r4@test.com", "token": "t",
            "password": "onething", "confirm_password": "another1",
        })
        assert resp.status_code == 200
        assert "do not match" in resp.text

    def test_password_too_short(self, app_client, fresh_db):
        _seed_user("r5@test.com",
                   reset_token="t",
                   reset_token_created=datetime.now(timezone.utc))
        resp = app_client.post("/auth/password/reset/confirm", data={
            "email": "r5@test.com", "token": "t",
            "password": "short", "confirm_password": "short",
        })
        assert resp.status_code == 200
        assert "at least 8" in resp.text


# ---- POST /auth/password/setup/request ----

class TestSetupRequest:
    def test_issues_token_for_pre_approved_user(self, app_client, fresh_db):
        _seed_user("invited@test.com")  # no password_hash
        resp = app_client.post("/auth/password/setup/request", data={"email": "invited@test.com"})
        assert resp.status_code == 200
        assert "Check your email" in resp.text
        u = _get_user("invited@test.com")
        assert u["setup_token"]

    def test_no_token_for_user_with_password(self, app_client, fresh_db):
        from argon2 import PasswordHasher
        _seed_user("already@test.com", password_hash=PasswordHasher().hash("x" * 10))
        resp = app_client.post("/auth/password/setup/request", data={"email": "already@test.com"})
        assert resp.status_code == 200  # anti-enumeration — same response
        u = _get_user("already@test.com")
        assert u["setup_token"] is None

    def test_unknown_email_same_response(self, app_client, fresh_db):
        resp = app_client.post("/auth/password/setup/request", data={"email": "who@test.com"})
        assert resp.status_code == 200
        assert "Check your email" in resp.text


# ---- POST /auth/password/setup/confirm ----

class TestSetupConfirm:
    def test_valid_token_sets_password_and_logs_in(self, app_client, fresh_db):
        _seed_user(
            "s1@test.com",
            setup_token="stok",
            setup_token_created=datetime.now(timezone.utc),
        )
        resp = app_client.post("/auth/password/setup/confirm", data={
            "email": "s1@test.com", "token": "stok",
            "password": "new-password-x", "confirm_password": "new-password-x",
            "name": "Seth One",
        })
        assert resp.status_code == 302
        assert resp.headers["location"] == "/dashboard"
        assert "access_token" in resp.cookies or "access_token" in resp.headers.get("set-cookie", "")
        u = _get_user("s1@test.com")
        assert u["setup_token"] is None
        assert u["name"] == "Seth One"
        from argon2 import PasswordHasher
        PasswordHasher().verify(u["password_hash"], "new-password-x")

    def test_expired_setup_token(self, app_client, fresh_db):
        _seed_user("s2@test.com",
                   setup_token="stok",
                   setup_token_created=datetime.now(timezone.utc) - timedelta(days=10))
        resp = app_client.post("/auth/password/setup/confirm", data={
            "email": "s2@test.com", "token": "stok",
            "password": "abcdefgh", "confirm_password": "abcdefgh",
        })
        assert resp.status_code == 200
        assert "expired" in resp.text.lower()

    def test_wrong_token(self, app_client, fresh_db):
        _seed_user("s3@test.com",
                   setup_token="right",
                   setup_token_created=datetime.now(timezone.utc))
        resp = app_client.post("/auth/password/setup/confirm", data={
            "email": "s3@test.com", "token": "wrong",
            "password": "abcdefgh", "confirm_password": "abcdefgh",
        })
        assert resp.status_code == 200
        assert "Invalid" in resp.text


# ---- Admin API: /api/users/{id}/reset-password, send_invite on create ----

class TestAdminInviteFlow:
    def test_reset_password_returns_reset_url(self, app_client, fresh_db):
        token = _seed_admin()
        _seed_user("target@test.com")
        target_id = _get_user("target@test.com")["id"]

        resp = app_client.post(
            f"/api/users/{target_id}/reset-password",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["reset_token"]
        assert "reset_url" in data
        assert "/auth/password/reset" in data["reset_url"]
        assert f"email=target%40test.com" in data["reset_url"]
        assert f"token={data['reset_token']}" in data["reset_url"]
        assert data["email_sent"] is False  # no SMTP configured in tests

    def test_create_user_with_send_invite_returns_invite_url(self, app_client, fresh_db):
        token = _seed_admin()
        resp = app_client.post(
            "/api/users",
            headers={"Authorization": f"Bearer {token}"},
            json={"email": "new@test.com", "name": "New", "role": "analyst", "send_invite": True},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["invite_url"]
        assert "/auth/password/setup" in data["invite_url"]
        assert data["invite_email_sent"] is False
        # And setup_token is actually stored on the user
        u = _get_user("new@test.com")
        assert u["setup_token"]

    def test_create_user_without_invite_has_no_invite_url(self, app_client, fresh_db):
        token = _seed_admin()
        resp = app_client.post(
            "/api/users",
            headers={"Authorization": f"Bearer {token}"},
            json={"email": "plain@test.com", "name": "Plain", "role": "analyst"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data.get("invite_url") is None
        assert data.get("invite_email_sent") is None
