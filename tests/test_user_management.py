"""Tests for #11 — user management (active flag, safeguards, endpoints)."""

import os
import tempfile
import pytest

import duckdb

from src.db import _ensure_schema, get_schema_version


@pytest.fixture
def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        # Reset cached system DB so we open a brand-new instance in tmp
        from src.db import close_system_db
        close_system_db()
        yield tmp
        close_system_db()


def test_schema_v5_adds_active_column(fresh_db):
    from src.db import get_system_db, close_system_db
    conn = get_system_db()
    try:
        cols = conn.execute("PRAGMA table_info(users)").fetchall()
        col_names = [c[1] for c in cols]
        assert "active" in col_names
        assert "deactivated_at" in col_names
        assert "deactivated_by" in col_names
        assert get_schema_version(conn) >= 5
    finally:
        conn.close()
        close_system_db()


def test_schema_v5_backfill_keeps_existing_users_active(fresh_db):
    """Simulate upgrading from v4: insert a user pre-migration, verify active=TRUE afterwards."""
    import uuid
    import duckdb as _duckdb
    from pathlib import Path

    # 1. Create a v4-era DB by hand.
    db_dir = Path(fresh_db) / "state"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "system.duckdb"
    conn = _duckdb.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TIMESTAMP DEFAULT current_timestamp)")
        conn.execute("INSERT INTO schema_version (version) VALUES (4)")
        conn.execute("""CREATE TABLE users (
            id VARCHAR PRIMARY KEY, email VARCHAR UNIQUE NOT NULL,
            name VARCHAR, role VARCHAR DEFAULT 'analyst',
            password_hash VARCHAR, setup_token VARCHAR,
            setup_token_created TIMESTAMP, reset_token VARCHAR,
            reset_token_created TIMESTAMP,
            created_at TIMESTAMP DEFAULT current_timestamp, updated_at TIMESTAMP)""")
        uid = str(uuid.uuid4())
        conn.execute("INSERT INTO users (id, email, name, role) VALUES (?, 'pre@v4', 'Pre', 'admin')", [uid])
    finally:
        conn.close()

    # 2. Now let the app open it — schema should migrate to v5 and backfill active=TRUE.
    from src.db import get_system_db, close_system_db, get_schema_version
    close_system_db()
    conn = get_system_db()
    try:
        assert get_schema_version(conn) >= 5
        row = conn.execute("SELECT email, active FROM users WHERE email = 'pre@v4'").fetchone()
        assert row is not None
        assert row[1] is True
    finally:
        conn.close()
        close_system_db()


def test_repository_update_accepts_active(fresh_db):
    import uuid
    from src.db import get_system_db, close_system_db
    from src.repositories.users import UserRepository
    conn = get_system_db()
    try:
        repo = UserRepository(conn)
        uid = str(uuid.uuid4())
        repo.create(id=uid, email="a@b.c", name="A", role="analyst")
        repo.update(id=uid, active=False, deactivated_by="admin-uuid")
        row = repo.get_by_id(uid)
        assert row["active"] is False
        assert row["deactivated_by"] == "admin-uuid"
    finally:
        conn.close()
        close_system_db()


def test_repository_count_admins(fresh_db):
    import uuid
    from src.db import get_system_db, close_system_db
    from src.repositories.users import UserRepository
    conn = get_system_db()
    try:
        repo = UserRepository(conn)
        assert repo.count_admins() == 0
        repo.create(id=str(uuid.uuid4()), email="a@b.c", name="A", role="admin")
        repo.create(id=str(uuid.uuid4()), email="b@b.c", name="B", role="analyst")
        assert repo.count_admins() == 1
    finally:
        conn.close()
        close_system_db()


from fastapi.testclient import TestClient


@pytest.fixture
def app_client(fresh_db, monkeypatch):
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
    from app.main import app
    return TestClient(app)


def _seed_admin(fresh_db):
    """Create an admin user and return (id, bearer_token)."""
    import uuid
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token
    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="admin@test", name="Admin", role="admin")
        token = create_access_token(user_id=uid, email="admin@test", role="admin")
        return uid, token
    finally:
        conn.close()


def test_patch_user_updates_role(app_client, fresh_db):
    import uuid
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    admin_id, token = _seed_admin(fresh_db)
    target_id = str(uuid.uuid4())
    conn = get_system_db()
    try:
        UserRepository(conn).create(id=target_id, email="x@test", name="X", role="viewer")
    finally:
        conn.close()

    resp = app_client.patch(
        f"/api/users/{target_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"role": "analyst", "name": "X2"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["role"] == "analyst"
    assert data["name"] == "X2"
