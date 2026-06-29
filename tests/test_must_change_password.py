"""TDD tests for the must-change-password feature (v77).

Covers:
1. Schema — must_change_password column exists after migration (v76→v77).
2. Repos — create/update parity on both backends (contract tests in
   tests/db_pg/test_users_contract.py; this file drives the DuckDB side
   and the app-level behaviour).
3. Bootstrap — seed admin sets must_change_password=True; restart on an
   already-rotated admin does NOT re-flag.
4. JSON login (POST /auth/password/login) — must_change_password=True
   returns 403 password_change_required; normal account still logs in.
5. Web login (POST /auth/password/login/web) — must_change_password=True
   mints a reset_token and redirects 303 to the reset page.
6. reset_confirm / setup_confirm — clear the flag; login works normally
   after that.
7. Admin set-password — sets must_change_password=True on the target user.
"""

from __future__ import annotations

import tempfile
import uuid
from datetime import datetime, timezone

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        from src.db import close_system_db

        close_system_db()
        yield tmp
        close_system_db()


@pytest.fixture()
def app_client(fresh_db, monkeypatch):
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-minimum-32-chars!!")
    from app.main import app

    return TestClient(app, follow_redirects=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ph():
    return PasswordHasher()


def _repo():
    """Return a fresh UserRepository connected to the current system DB."""
    from src.db import get_system_db
    from src.repositories.users import UserRepository

    return UserRepository(get_system_db())


def _seed_user(
    email: str,
    *,
    password_hash: str | None = None,
    must_change_password: bool = False,
    reset_token: str | None = None,
    reset_token_created: datetime | None = None,
    setup_token: str | None = None,
    setup_token_created: datetime | None = None,
) -> str:
    """Create a user (via UserRepository) and return its id."""
    repo = _repo()
    uid = str(uuid.uuid4())
    repo.create(
        id=uid,
        email=email,
        name=email.split("@")[0],
        password_hash=password_hash,
        must_change_password=must_change_password,
    )
    updates: dict = {}
    if reset_token is not None:
        updates["reset_token"] = reset_token
    if reset_token_created is not None:
        updates["reset_token_created"] = reset_token_created
    if setup_token is not None:
        updates["setup_token"] = setup_token
    if setup_token_created is not None:
        updates["setup_token_created"] = setup_token_created
    if updates:
        repo.update(id=uid, **updates)
    return uid


def _get_user(email: str) -> dict:
    return _repo().get_by_email(email)


def _seed_admin_token() -> str:
    """Create an admin user and return a JWT for that user."""
    from app.auth.jwt import create_access_token
    from tests.helpers.auth import grant_admin
    from src.db import get_system_db
    from src.repositories.users import UserRepository

    conn = get_system_db()
    uid = str(uuid.uuid4())
    UserRepository(conn).create(id=uid, email="admin@test", name="Admin")
    grant_admin(conn, uid)
    return create_access_token(user_id=uid, email="admin@test")


# ---------------------------------------------------------------------------
# 1. Schema — must_change_password column present on fresh install (v77)
# ---------------------------------------------------------------------------


class TestSchema:
    def test_column_exists_on_fresh_install(self, fresh_db):
        import duckdb
        from src.db import _ensure_schema
        import os

        db_path = os.path.join(fresh_db, "state", "system.duckdb")
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = duckdb.connect(db_path)
        _ensure_schema(conn)
        cols = {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'users'"
            ).fetchall()
        }
        assert "must_change_password" in cols
        conn.close()

    def test_column_defaults_false(self, fresh_db):
        import duckdb
        import os
        from src.db import _ensure_schema

        db_path = os.path.join(fresh_db, "state", "system.duckdb")
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = duckdb.connect(db_path)
        _ensure_schema(conn)
        conn.execute(
            "INSERT INTO users (id, email, name, created_at, updated_at) VALUES ('x', 'x@test', 'X', current_timestamp, current_timestamp)"
        )
        row = conn.execute("SELECT must_change_password FROM users WHERE id = 'x'").fetchone()
        assert row is not None
        assert row[0] is False
        conn.close()

    def test_migration_from_v76_adds_column(self, tmp_path):
        """A DB at v76 (no must_change_password) upgrades cleanly to v77."""
        import duckdb
        from src.db import _ensure_schema, get_schema_version, SCHEMA_VERSION

        db = duckdb.connect(str(tmp_path / "sys.duckdb"))
        # Stand up a minimal v76 DB without must_change_password.
        db.execute("CREATE TABLE schema_version (version INTEGER, applied_at TIMESTAMP DEFAULT current_timestamp)")
        db.execute("INSERT INTO schema_version VALUES (76, current_timestamp)")
        db.execute(
            """CREATE TABLE users (
                id VARCHAR PRIMARY KEY,
                email VARCHAR UNIQUE NOT NULL,
                name VARCHAR,
                password_hash VARCHAR,
                setup_token VARCHAR,
                setup_token_created TIMESTAMP,
                reset_token VARCHAR,
                reset_token_created TIMESTAMP,
                active BOOLEAN NOT NULL DEFAULT TRUE,
                deactivated_at TIMESTAMP,
                deactivated_by VARCHAR,
                created_at TIMESTAMP DEFAULT current_timestamp,
                updated_at TIMESTAMP,
                onboarded BOOLEAN NOT NULL DEFAULT FALSE,
                last_pull_at TIMESTAMP,
                slack_user_id VARCHAR
            )"""
        )
        db.execute("INSERT INTO users (id, email, name) VALUES ('u1', 'u@test', 'U')")

        _ensure_schema(db)

        assert get_schema_version(db) == SCHEMA_VERSION

        cols = {
            r[0]
            for r in db.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'users'"
            ).fetchall()
        }
        assert "must_change_password" in cols

        # Existing row backfilled to FALSE.
        row = db.execute("SELECT must_change_password FROM users WHERE id = 'u1'").fetchone()
        assert row is not None
        assert row[0] is False
        db.close()


# ---------------------------------------------------------------------------
# 2. Repository — create with must_change_password=True; update toggles it
# ---------------------------------------------------------------------------


class TestRepo:
    def test_create_with_flag_true_round_trips(self, fresh_db):
        repo = _repo()
        uid = str(uuid.uuid4())
        repo.create(id=uid, email="mcp@test", name="M", must_change_password=True)
        row = repo.get_by_id(uid)
        assert row is not None
        assert row["must_change_password"] is True

    def test_create_default_false(self, fresh_db):
        repo = _repo()
        uid = str(uuid.uuid4())
        repo.create(id=uid, email="normal@test", name="N")
        row = repo.get_by_id(uid)
        assert row is not None
        assert row["must_change_password"] is False

    def test_update_flag_to_false(self, fresh_db):
        repo = _repo()
        uid = str(uuid.uuid4())
        repo.create(id=uid, email="upd@test", name="U", must_change_password=True)
        repo.update(id=uid, must_change_password=False)
        row = repo.get_by_id(uid)
        assert row["must_change_password"] is False

    def test_update_flag_to_true(self, fresh_db):
        repo = _repo()
        uid = str(uuid.uuid4())
        repo.create(id=uid, email="upd2@test", name="U2")
        repo.update(id=uid, must_change_password=True)
        row = repo.get_by_id(uid)
        assert row["must_change_password"] is True


# ---------------------------------------------------------------------------
# 3. Bootstrap — seed admin sets must_change_password=True
# ---------------------------------------------------------------------------


class TestBootstrap:
    def test_seed_admin_with_password_gets_flagged(self, fresh_db, monkeypatch):
        """When SEED_ADMIN_EMAIL + SEED_ADMIN_PASSWORD are set and the admin is
        newly created, must_change_password must be True."""
        monkeypatch.setenv("SEED_ADMIN_EMAIL", "admin@example.com")
        monkeypatch.setenv("SEED_ADMIN_PASSWORD", "seeded-pass-123")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-minimum-32-chars!!")
        monkeypatch.setenv("TESTING", "1")

        # Trigger startup (which runs the seed logic).
        from app.main import app
        from fastapi.testclient import TestClient

        with TestClient(app):
            pass  # startup fires on enter

        user = _get_user("admin@example.com")
        assert user is not None
        assert user["must_change_password"] is True

    def test_seed_admin_without_password_not_flagged(self, fresh_db, monkeypatch):
        """SSO-only admin (no SEED_ADMIN_PASSWORD) must NOT be flagged."""
        monkeypatch.setenv("SEED_ADMIN_EMAIL", "sso@example.com")
        monkeypatch.delenv("SEED_ADMIN_PASSWORD", raising=False)
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-minimum-32-chars!!")
        monkeypatch.setenv("TESTING", "1")

        from app.main import app
        from fastapi.testclient import TestClient

        with TestClient(app):
            pass

        user = _get_user("sso@example.com")
        assert user is not None
        assert not user.get("must_change_password", False)

    def test_restart_does_not_reflag_rotated_admin(self, fresh_db, monkeypatch):
        """If the seed admin already has a password and must_change_password=False
        (meaning they rotated it), a restart must NOT set the flag again.

        The bootstrap update branch fires only when the existing user has
        NO password_hash. If they've rotated (has password_hash), it's a no-op.
        """
        monkeypatch.setenv("SEED_ADMIN_EMAIL", "admin2@example.com")
        monkeypatch.setenv("SEED_ADMIN_PASSWORD", "seed-pass-abc-123")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-minimum-32-chars!!")
        monkeypatch.setenv("TESTING", "1")

        # First boot: user created with must_change_password=True.
        from src.db import close_system_db
        from app.main import app
        from fastapi.testclient import TestClient

        with TestClient(app):
            pass

        # Simulate rotation: clear must_change_password.
        user = _get_user("admin2@example.com")
        repo = _repo()
        repo.update(id=user["id"], must_change_password=False)
        close_system_db()

        # Second boot: must NOT re-flag.
        with TestClient(app):
            pass

        user2 = _get_user("admin2@example.com")
        assert user2["must_change_password"] is False


# ---------------------------------------------------------------------------
# 4. JSON login — must_change_password → 403 password_change_required
# ---------------------------------------------------------------------------


class TestJsonLogin:
    def test_normal_login_works(self, app_client, fresh_db):
        pw = "correct-pass-123"
        _seed_user("ok@test.com", password_hash=_ph().hash(pw))
        resp = app_client.post(
            "/auth/password/login",
            json={"email": "ok@test.com", "password": pw},
        )
        assert resp.status_code == 200
        assert "access_token" in resp.json()

    def test_must_change_password_blocks_json_login(self, app_client, fresh_db):
        pw = "seeded-pass-xyz"
        _seed_user(
            "forced@test.com",
            password_hash=_ph().hash(pw),
            must_change_password=True,
        )
        resp = app_client.post(
            "/auth/password/login",
            json={"email": "forced@test.com", "password": pw},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "password_change_required"

    def test_wrong_password_still_401(self, app_client, fresh_db):
        _seed_user("wrong@test.com", password_hash=_ph().hash("correct-pass"))
        resp = app_client.post(
            "/auth/password/login",
            json={"email": "wrong@test.com", "password": "wrong-pass"},
        )
        assert resp.status_code == 401

    def test_must_change_password_checked_after_password_verify(self, app_client, fresh_db):
        """If must_change_password is True but password is wrong, 401 first,
        not 403 — verify happens before the flag check."""
        _seed_user(
            "both@test.com",
            password_hash=_ph().hash("correct-pass"),
            must_change_password=True,
        )
        resp = app_client.post(
            "/auth/password/login",
            json={"email": "both@test.com", "password": "wrong-pass"},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 5. Web login — must_change_password → 303 redirect to reset page with token
# ---------------------------------------------------------------------------


class TestWebLogin:
    def test_normal_web_login_works(self, app_client, fresh_db):
        pw = "web-pass-ok-123"
        _seed_user("web-ok@test.com", password_hash=_ph().hash(pw))
        resp = app_client.post(
            "/auth/password/login/web",
            data={"email": "web-ok@test.com", "password": pw},
        )
        assert resp.status_code == 302
        assert "access_token" in resp.cookies or "access_token" in resp.headers.get("set-cookie", "")

    def test_must_change_password_redirects_to_reset_page(self, app_client, fresh_db):
        pw = "seeded-web-pass"
        _seed_user(
            "web-forced@test.com",
            password_hash=_ph().hash(pw),
            must_change_password=True,
        )
        resp = app_client.post(
            "/auth/password/login/web",
            data={"email": "web-forced@test.com", "password": pw},
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert "/auth/password/reset" in loc
        assert "email=" in loc
        assert "token=" in loc
        # No login cookie must be set.
        assert "access_token" not in resp.cookies
        # A reset_token must have been minted in the DB.
        user = _get_user("web-forced@test.com")
        assert user["reset_token"] is not None

    def test_must_change_wrong_password_no_redirect(self, app_client, fresh_db):
        """Wrong password must still 302 to error page, not 303 to reset."""
        _seed_user(
            "web-bad@test.com",
            password_hash=_ph().hash("correct-pass"),
            must_change_password=True,
        )
        resp = app_client.post(
            "/auth/password/login/web",
            data={"email": "web-bad@test.com", "password": "wrong-pass"},
        )
        # Should be 302 to login page error, not 303 to reset.
        assert resp.status_code == 302
        assert "/auth/password/reset" not in resp.headers["location"]


# ---------------------------------------------------------------------------
# 6. reset_confirm / setup_confirm — clear the flag
# ---------------------------------------------------------------------------


class TestFlagClearedOnReset:
    def test_reset_confirm_clears_must_change_password(self, app_client, fresh_db):
        _seed_user(
            "rc@test.com",
            password_hash=_ph().hash("old-pass-123"),
            must_change_password=True,
            reset_token="tok-rc",
            reset_token_created=datetime.now(timezone.utc),
        )
        resp = app_client.post(
            "/auth/password/reset/confirm",
            data={
                "email": "rc@test.com",
                "token": "tok-rc",
                "password": "new-pass-after-reset",
                "confirm_password": "new-pass-after-reset",
            },
        )
        assert resp.status_code == 302
        user = _get_user("rc@test.com")
        assert user["must_change_password"] is False

    def test_setup_confirm_clears_must_change_password(self, app_client, fresh_db):
        """setup_confirm path: first-time setup also clears the flag."""
        _seed_user(
            "sc@test.com",
            must_change_password=True,
            setup_token="stok-sc",
            setup_token_created=datetime.now(timezone.utc),
        )
        resp = app_client.post(
            "/auth/password/setup/confirm",
            data={
                "email": "sc@test.com",
                "token": "stok-sc",
                "password": "new-setup-pass-123",
                "confirm_password": "new-setup-pass-123",
            },
        )
        assert resp.status_code == 302
        user = _get_user("sc@test.com")
        assert user["must_change_password"] is False

    def test_login_works_after_reset(self, app_client, fresh_db):
        """After clearing the flag via reset_confirm, JSON login must succeed."""
        _seed_user(
            "post-rc@test.com",
            password_hash=_ph().hash("old-pass-123"),
            must_change_password=True,
            reset_token="tok-post",
            reset_token_created=datetime.now(timezone.utc),
        )
        app_client.post(
            "/auth/password/reset/confirm",
            data={
                "email": "post-rc@test.com",
                "token": "tok-post",
                "password": "rotated-pass-456",
                "confirm_password": "rotated-pass-456",
            },
        )
        resp = app_client.post(
            "/auth/password/login",
            json={"email": "post-rc@test.com", "password": "rotated-pass-456"},
        )
        assert resp.status_code == 200
        assert "access_token" in resp.json()


# ---------------------------------------------------------------------------
# 7. Admin set-password sets must_change_password=True
# ---------------------------------------------------------------------------


class TestAdminSetPassword:
    def test_set_password_flags_must_change(self, app_client, fresh_db):
        admin_token = _seed_admin_token()
        uid = _seed_user("target@test.com", password_hash=_ph().hash("old-pass"))

        resp = app_client.post(
            f"/api/users/{uid}/set-password",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"password": "admin-set-pass-xyz"},
        )
        assert resp.status_code == 204

        user = _get_user("target@test.com")
        assert user["must_change_password"] is True

    def test_user_cannot_login_after_admin_sets_password(self, app_client, fresh_db):
        admin_token = _seed_admin_token()
        uid = _seed_user("blocked-target@test.com", password_hash=_ph().hash("old"))

        app_client.post(
            f"/api/users/{uid}/set-password",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"password": "admin-new-pass-xyz"},
        )

        resp = app_client.post(
            "/auth/password/login",
            json={"email": "blocked-target@test.com", "password": "admin-new-pass-xyz"},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "password_change_required"


class TestJsonSetupClearsFlag:
    def test_json_setup_clears_must_change_and_logs_in(self, app_client, fresh_db):
        """JSON POST /auth/password/setup is a self-chosen-password path, so it
        must clear must_change_password (parity with the web setup_confirm) —
        otherwise an admin-set user holding a still-valid setup token stays
        flagged after rotating, or bypasses the login gate without clearing it."""
        _seed_user(
            "json-setup@test.com",
            password_hash=_ph().hash("admin-set-old-pass"),
            must_change_password=True,
            setup_token="stok-json",
            setup_token_created=datetime.now(timezone.utc),
        )
        resp = app_client.post(
            "/auth/password/setup",
            json={
                "email": "json-setup@test.com",
                "token": "stok-json",
                "password": "self-chosen-pass-1",
            },
        )
        assert resp.status_code == 200, resp.text
        assert "access_token" in resp.json()
        user = _get_user("json-setup@test.com")
        assert user["must_change_password"] is False
        # And a subsequent JSON login is no longer blocked.
        login = app_client.post(
            "/auth/password/login",
            json={"email": "json-setup@test.com", "password": "self-chosen-pass-1"},
        )
        assert login.status_code == 200
