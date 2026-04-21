"""Tests for #12 — personal access tokens (PAT)."""

import os
import tempfile
import pytest


@pytest.fixture
def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        yield tmp


def test_schema_v6_creates_pat_table(fresh_db):
    from src.db import get_system_db, get_schema_version, close_system_db
    conn = get_system_db()
    try:
        cols = conn.execute("PRAGMA table_info(personal_access_tokens)").fetchall()
        col_names = [c[1] for c in cols]
        for expected in ("id", "user_id", "name", "token_hash", "prefix",
                         "scopes", "created_at", "expires_at", "last_used_at", "revoked_at"):
            assert expected in col_names
        assert get_schema_version(conn) >= 6
    finally:
        conn.close()
        close_system_db()


def test_access_token_repo_create_and_lookup(fresh_db):
    import hashlib, uuid
    from datetime import datetime, timezone, timedelta
    from src.db import get_system_db, close_system_db
    from src.repositories.access_tokens import AccessTokenRepository

    conn = get_system_db()
    try:
        repo = AccessTokenRepository(conn)
        token_id = str(uuid.uuid4())
        raw = "abcdefgh" + "x" * 32
        repo.create(
            id=token_id,
            user_id="u1",
            name="laptop",
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            prefix=raw[:8],
            expires_at=datetime.now(timezone.utc) + timedelta(days=90),
        )
        row = repo.get_by_id(token_id)
        assert row is not None
        assert row["name"] == "laptop"
        assert row["prefix"] == "abcdefgh"
        assert row["revoked_at"] is None

        rows = repo.list_for_user("u1")
        assert len(rows) == 1

        repo.revoke(token_id)
        assert repo.get_by_id(token_id)["revoked_at"] is not None
    finally:
        conn.close()
        close_system_db()


def test_access_token_repo_mark_used(fresh_db):
    import hashlib, uuid
    from datetime import datetime, timezone
    from src.db import get_system_db, close_system_db
    from src.repositories.access_tokens import AccessTokenRepository

    conn = get_system_db()
    try:
        repo = AccessTokenRepository(conn)
        tid = str(uuid.uuid4())
        repo.create(id=tid, user_id="u1", name="x",
                    token_hash=hashlib.sha256(b"r").hexdigest(), prefix="rrrrrrrr")
        assert repo.get_by_id(tid)["last_used_at"] is None
        repo.mark_used(tid)
        assert repo.get_by_id(tid)["last_used_at"] is not None
    finally:
        conn.close()
        close_system_db()


def test_pat_token_carries_typ_claim(fresh_db):
    from app.auth.jwt import create_access_token, verify_token
    token = create_access_token(
        user_id="u1", email="u@test", role="analyst",
        token_id="deadbeef-1234", typ="pat",
    )
    payload = verify_token(token)
    assert payload["typ"] == "pat"
    assert payload["jti"] == "deadbeef-1234"


def test_session_token_defaults_typ(fresh_db):
    from app.auth.jwt import create_access_token, verify_token
    token = create_access_token(user_id="u1", email="u@test", role="analyst")
    payload = verify_token(token)
    # Default typ is "session".
    assert payload.get("typ") == "session"


def test_revoked_pat_is_rejected(fresh_db, monkeypatch):
    from fastapi.testclient import TestClient
    import hashlib, uuid
    from datetime import datetime, timezone, timedelta
    from src.db import get_system_db, close_system_db
    from src.repositories.users import UserRepository
    from src.repositories.access_tokens import AccessTokenRepository
    from app.auth.jwt import create_access_token
    from app.main import app

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="u@t", name="U", role="admin")
        token_id = str(uuid.uuid4())
        raw = "secretXX" + "a" * 32
        AccessTokenRepository(conn).create(
            id=token_id, user_id=uid, name="ci",
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            prefix=raw[:8],
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
        jwt_token = create_access_token(
            user_id=uid, email="u@t", role="admin", token_id=token_id, typ="pat",
        )
        # Revoke
        AccessTokenRepository(conn).revoke(token_id)
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.get(
        "/api/users",
        headers={"Authorization": f"Bearer {jwt_token}", "Accept": "application/json"},
    )
    assert resp.status_code == 401


def test_expired_pat_is_rejected_from_db(fresh_db):
    """A PAT with a past expires_at in DB is rejected even if JWT exp is in future."""
    from fastapi.testclient import TestClient
    import hashlib, uuid
    from datetime import datetime, timezone, timedelta
    from src.db import get_system_db, close_system_db
    from src.repositories.users import UserRepository
    from src.repositories.access_tokens import AccessTokenRepository
    from app.auth.jwt import create_access_token
    from app.main import app

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="u@t", name="U", role="admin")
        tid = str(uuid.uuid4())
        # Past-dated expiry in DB
        AccessTokenRepository(conn).create(
            id=tid, user_id=uid, name="stale",
            token_hash=hashlib.sha256(b"whatever").hexdigest(), prefix=tid.replace("-","")[:8],
            expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        # JWT with much longer TTL so signature-level `exp` would pass
        pat = create_access_token(
            user_id=uid, email="u@t", role="admin",
            token_id=tid, typ="pat",
            expires_delta=timedelta(days=365),
        )
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.get(
        "/api/users",
        headers={"Authorization": f"Bearer {pat}", "Accept": "application/json"},
    )
    assert resp.status_code == 401
