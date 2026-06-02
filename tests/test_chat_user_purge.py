"""Tests for GDPR hard-delete of chat sessions + workdir.

The delete-user route resolves the target through the global ``users_repo()``
factory (which, in DuckDB mode, opens ``get_system_db()``), so the test must
seed into that same system DB rather than an isolated ``:memory:`` connection
— otherwise the route 404s on a user the test thinks it created. We point
``DATA_DIR`` at a tmp dir and use ``get_system_db()`` throughout.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import MagicMock

import duckdb
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.db import _ensure_schema
from app.api.users import router as users_router, _purge_user_chat_data
from app.auth.access import require_admin
from app.auth.dependencies import get_current_user
from app.chat.persistence import ChatRepository
from app.chat.types import Surface


ADMIN_USER = {"id": "admin1", "email": "admin@test.com", "is_admin": True}
TARGET_EMAIL = "target@test.com"


def _sysdb(tmp_path: Path, monkeypatch) -> duckdb.DuckDBPyConnection:
    """Point DATA_DIR at a tmp dir and return its system DuckDB connection.

    ``get_system_db()`` reopens when the resolved path changes (per-test
    isolation via the unique tmp_path), and runs ``_ensure_schema`` on open.
    The route's ``users_repo()`` factory uses the same ``get_system_db()``,
    so seeded rows are visible to the handler.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "sysdb"))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
    from src.db import get_system_db

    return get_system_db()


def _make_app(conn: duckdb.DuckDBPyConnection, tmp_path: Path) -> FastAPI:
    from app.api.users import _get_db
    from app.chat.workdir import WorkdirManager

    app = FastAPI()
    app.include_router(users_router)

    # Build a real chat repo so hard-delete can sweep rows
    chat_repo = ChatRepository(conn)
    app.state.chat_repo = chat_repo

    # Build a real workdir manager pointing at tmp_path
    bundled = tmp_path / "bundled"
    bundled.mkdir(parents=True, exist_ok=True)
    (bundled / "CLAUDE.md").write_text("d")
    workdir_mgr = WorkdirManager(
        data_dir=tmp_path / "data",
        repo=chat_repo,
        bundled_template_dir=bundled,
        server_url="https://example",
        agnes_version="0.55.0",
        get_marketplace_sha=lambda: "sha-1",
        get_template_status=lambda: None,
    )

    # Expose workdir_mgr via a fake chat_manager on app.state so
    # _purge_user_chat_data can find it
    fake_mgr = MagicMock()
    fake_mgr._workdir_mgr = workdir_mgr
    app.state.chat_manager = fake_mgr

    # Override auth so we don't need real session infrastructure
    app.dependency_overrides[get_current_user] = lambda: ADMIN_USER
    app.dependency_overrides[require_admin] = lambda: ADMIN_USER
    app.dependency_overrides[_get_db] = lambda: conn

    return app


def _seed_target_user(conn: duckdb.DuckDBPyConnection) -> str:
    """Insert a target user row and return the user_id."""
    user_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO users (id, email, name, created_at, active) VALUES (?, ?, ?, NOW(), TRUE)",
        [user_id, TARGET_EMAIL, "Target User"],
    )
    return user_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_hard_delete_removes_chat_sessions(tmp_path: Path, monkeypatch):
    """DELETE /api/users/{id}?hard=true removes all chat_sessions rows."""
    conn = _sysdb(tmp_path, monkeypatch)

    user_id = _seed_target_user(conn)
    repo = ChatRepository(conn)
    # Create two sessions for the target user
    repo.create_session(user_email=TARGET_EMAIL, surface=Surface.WEB)
    repo.create_session(user_email=TARGET_EMAIL, surface=Surface.WEB)

    assert len(repo.list_sessions(TARGET_EMAIL)) == 2

    app = _make_app(conn, tmp_path)
    client = TestClient(app)
    r = client.delete(f"/api/users/{user_id}?hard=true")
    assert r.status_code == 204

    # Sessions must be gone
    assert repo.list_sessions(TARGET_EMAIL) == []


def test_hard_delete_removes_workdir(tmp_path: Path, monkeypatch):
    """DELETE /api/users/{id}?hard=true wipes the per-user workdir."""
    conn = _sysdb(tmp_path, monkeypatch)

    user_id = _seed_target_user(conn)

    # Ensure a workdir exists on disk by calling workdir_mgr.ensure_user_workdir
    bundled = tmp_path / "bundled"
    bundled.mkdir(parents=True, exist_ok=True)
    (bundled / "CLAUDE.md").write_text("d")
    from app.chat.workdir import WorkdirManager
    from app.chat.persistence import ChatRepository as CR
    repo = CR(conn)
    wm = WorkdirManager(
        data_dir=tmp_path / "data",
        repo=repo,
        bundled_template_dir=bundled,
        server_url="https://example",
        agnes_version="0.55.0",
        get_marketplace_sha=lambda: "sha-1",
        get_template_status=lambda: None,
    )
    wm.ensure_user_workdir(TARGET_EMAIL)
    user_root = wm._user_root(TARGET_EMAIL)
    assert user_root.exists()

    app = _make_app(conn, tmp_path)
    client = TestClient(app)
    r = client.delete(f"/api/users/{user_id}?hard=true")
    assert r.status_code == 204

    # Workdir must be gone
    assert not user_root.exists()


def test_soft_delete_does_not_purge_chat(tmp_path: Path, monkeypatch):
    """DELETE /api/users/{id} without ?hard=true leaves chat sessions intact."""
    conn = _sysdb(tmp_path, monkeypatch)

    user_id = _seed_target_user(conn)
    repo = ChatRepository(conn)
    repo.create_session(user_email=TARGET_EMAIL, surface=Surface.WEB)

    app = _make_app(conn, tmp_path)
    client = TestClient(app)
    r = client.delete(f"/api/users/{user_id}")
    assert r.status_code == 204

    # Chat sessions must still exist (soft delete does not purge)
    rows = conn.execute(
        "SELECT COUNT(*) FROM chat_sessions WHERE user_email = ?", [TARGET_EMAIL]
    ).fetchone()[0]
    assert rows == 1


def test_purge_user_chat_data_no_sessions(tmp_path: Path):
    """_purge_user_chat_data is idempotent when user has no chat data."""
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)

    bundled = tmp_path / "bundled"
    bundled.mkdir(parents=True, exist_ok=True)
    (bundled / "CLAUDE.md").write_text("d")

    # Create a mock request with no chat_manager on state
    mock_request = MagicMock()
    mock_request.app.state.chat_manager = None

    # Should not raise
    _purge_user_chat_data(conn, mock_request, "nobody@example.com", actor_id="admin1")
