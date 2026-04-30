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
        repo.create(id=uid, email=email, name=email.split("@")[0],
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
    from tests.helpers.auth import grant_admin
    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="admin@test", name="Admin")
        grant_admin(conn, uid)
        return create_access_token(user_id=uid, email="admin@test")
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

    def test_does_not_leak_user_existence_via_name_prefill(self, app_client, fresh_db):
        """GET /setup must render the same form whether the email exists or not,
        so an attacker can't enumerate users by watching the name field."""
        _seed_user("alice@test.com")  # seeded with name="alice" (derived from email)
        r_known = app_client.get("/auth/password/setup",
                                 params={"email": "alice@test.com", "token": "anything"})
        r_unknown = app_client.get("/auth/password/setup",
                                   params={"email": "ghost@test.com", "token": "anything"})
        assert r_known.status_code == 200 and r_unknown.status_code == 200
        # Seeded user's display name must NOT be pre-filled in the name input.
        assert 'value="alice"' not in r_known.text
        # The two responses should differ only by URL-reflected values (email).
        for body in (r_known.text, r_unknown.text):
            assert 'name="name"' in body  # the blank name input is always there

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

    def test_concurrent_reset_only_one_wins(self, app_client, fresh_db):
        """Two concurrent reset/confirm POSTs — exactly one must succeed.

        Mirrors `test_concurrent_verify_only_one_wins` for the magic-link
        flow. Without the CAS pattern at `_atomic_consume_reset_token`,
        two concurrent POSTs with the same valid token could both write
        different new passwords for the same user (last-write-wins
        semantics). With the CAS, the loser gets the "Invalid or expired"
        form back instead of silently overwriting.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import threading

        _seed_user(
            "race@test.com",
            reset_token="race-tok",
            reset_token_created=datetime.now(timezone.utc),
        )

        results: list[tuple[int, str]] = []
        barrier = threading.Barrier(2, timeout=5)

        def confirm(payload_password: str):
            barrier.wait()  # both threads hit the endpoint at the same instant
            resp = app_client.post(
                "/auth/password/reset/confirm",
                data={
                    "email": "race@test.com",
                    "token": "race-tok",
                    "password": payload_password,
                    "confirm_password": payload_password,
                },
            )
            results.append((resp.status_code, resp.text))

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(confirm, "winner-password"),
                pool.submit(confirm, "loser-password"),
            ]
            for f in as_completed(futures):
                f.result()

        # Exactly one 302 (winner — redirected to login) and one 200
        # (loser — got the reset form back with the standard error).
        redirects = [r for r in results if r[0] == 302]
        rejects = [r for r in results if r[0] == 200]
        assert len(redirects) == 1, (
            f"Expected exactly 1 winner, got {len(redirects)} (results: {results})"
        )
        assert len(rejects) == 1, (
            f"Expected exactly 1 loser, got {len(rejects)} (results: {results})"
        )
        assert "Invalid or expired" in rejects[0][1]


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
        assert "reset_url" in data
        assert "/auth/password/reset" in data["reset_url"]
        assert "token=" in data["reset_url"]  # URL still contains the token
        assert "email_sent" in data
        assert data["email_sent"] is False  # no SMTP configured in tests
        assert "reset_token" not in data  # raw token must NOT be in response

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


class TestJsonSetupHardening:
    """The JSON POST /auth/password/setup endpoint must enforce the same token
    TTL and active-account gate as the web flow."""

    def test_expired_token_rejected(self, app_client, fresh_db):
        _seed_user(
            "j1@test.com",
            setup_token="tok",
            setup_token_created=datetime.now(timezone.utc) - timedelta(days=10),
        )
        resp = app_client.post("/auth/password/setup",
                               json={"email": "j1@test.com", "token": "tok",
                                     "password": "long-enough-1"})
        assert resp.status_code == 400
        assert "expired" in resp.json()["detail"].lower()

    def test_missing_created_timestamp_rejected(self, app_client, fresh_db):
        """A token row without setup_token_created is treated as invalid — we
        cannot verify its age, so it must fail closed."""
        _seed_user("j2@test.com", setup_token="tok")
        resp = app_client.post("/auth/password/setup",
                               json={"email": "j2@test.com", "token": "tok",
                                     "password": "long-enough-1"})
        assert resp.status_code == 400

    def test_deactivated_user_rejected(self, app_client, fresh_db):
        uid = _seed_user(
            "j3@test.com",
            setup_token="tok",
            setup_token_created=datetime.now(timezone.utc),
        )
        # Flip user to inactive
        from src.db import get_system_db
        from src.repositories.users import UserRepository
        conn = get_system_db()
        try:
            UserRepository(conn).update(id=uid, active=False)
        finally:
            conn.close()

        resp = app_client.post("/auth/password/setup",
                               json={"email": "j3@test.com", "token": "tok",
                                     "password": "long-enough-1"})
        assert resp.status_code == 403


class TestCaseSensitiveEmailLookup:
    """Reset/setup requests must match the codebase's case-sensitive email
    lookup — lowercasing here would silently fail for mixed-case accounts."""

    def test_reset_request_preserves_email_case(self, app_client, fresh_db):
        # User stored as-is with mixed-case local-part
        _seed_user("User.Mixed@Example.com", password_hash="x")
        # Caller submits the same exact case → token must be issued
        resp = app_client.post("/auth/password/reset",
                               data={"email": "User.Mixed@Example.com"})
        assert resp.status_code == 200
        u = _get_user("User.Mixed@Example.com")
        assert u["reset_token"]

    def test_reset_request_case_mismatch_still_anti_enumerates(self, app_client, fresh_db):
        _seed_user("User.Mixed@Example.com", password_hash="x")
        # Wrong case: response is the same (anti-enumeration) and no token is issued
        resp = app_client.post("/auth/password/reset",
                               data={"email": "user.mixed@example.com"})
        assert resp.status_code == 200
        assert "Check your email" in resp.text
        u = _get_user("User.Mixed@Example.com")
        assert u["reset_token"] is None
