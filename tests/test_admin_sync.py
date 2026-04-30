"""Tests for the /admin/sync admin page and the /api/sync/history endpoint
that backs it.

Coverage:
  - /admin/sync renders for admin, 302/403 for analyst and unauthenticated
  - GET /api/sync/history returns a list, admin-only, ``limit`` and
    ``table_id`` filters are respected
  - The page template references the live history endpoint and the
    trigger endpoint (pin so a renderer regression that drops the JS
    fetch fails CI)
"""

from __future__ import annotations

import tempfile
import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        from src.db import close_system_db
        close_system_db()
        yield tmp
        close_system_db()


def _seed_user(role: str = "admin"):
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email=f"{role}@test", name=role.title(), role=role)
        if role == "admin":
            admin_gid = conn.execute(
                "SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]
            ).fetchone()[0]
            UserGroupMembersRepository(conn).add_member(uid, admin_gid, source="system_seed")
        return uid, create_access_token(user_id=uid, email=f"{role}@test", role=role)
    finally:
        conn.close()


def _seed_history_row(table_id: str, status: str = "ok", rows: int = 100):
    """Inserts a single sync_history row so the endpoint has data to return."""
    from datetime import datetime, timezone
    from src.db import get_system_db

    conn = get_system_db()
    try:
        conn.execute(
            "INSERT INTO sync_history (id, table_id, synced_at, rows, duration_ms, status, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [str(uuid.uuid4()), table_id, datetime.now(timezone.utc), rows, 1234, status, None],
        )
    finally:
        conn.close()


# ── /api/sync/history ────────────────────────────────────────────────────


def test_history_endpoint_admin_returns_list(fresh_db):
    from app.main import app

    _, token = _seed_user("admin")
    _seed_history_row("orders")
    _seed_history_row("customers")

    client = TestClient(app)
    r = client.get(
        "/api/sync/history",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert isinstance(data, list)
    assert len(data) == 2
    table_ids = {row["table_id"] for row in data}
    assert table_ids == {"orders", "customers"}
    # synced_at is serialized as a string for JSON; the page formats
    # client-side via fmtAbs.
    assert all(isinstance(row["synced_at"], str) for row in data)


def test_history_endpoint_filter_by_table_id(fresh_db):
    from app.main import app

    _, token = _seed_user("admin")
    _seed_history_row("orders")
    _seed_history_row("orders")
    _seed_history_row("customers")

    client = TestClient(app)
    r = client.get(
        "/api/sync/history?table_id=orders",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 2
    assert all(row["table_id"] == "orders" for row in data)


def test_history_endpoint_limit_clamped(fresh_db):
    from app.main import app

    _, token = _seed_user("admin")
    for _ in range(5):
        _seed_history_row("orders")

    client = TestClient(app)
    r = client.get(
        "/api/sync/history?limit=2",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert len(r.json()) == 2


def test_history_endpoint_admin_only(fresh_db):
    from app.main import app

    _, analyst_token = _seed_user("analyst")
    client = TestClient(app)
    r = client.get(
        "/api/sync/history",
        headers={"Authorization": f"Bearer {analyst_token}"},
    )
    assert r.status_code == 403, r.text


# ── /admin/sync page ─────────────────────────────────────────────────────


def test_admin_sync_page_renders_for_admin(fresh_db):
    from app.main import app

    _, token = _seed_user("admin")
    client = TestClient(app)
    r = client.get(
        "/admin/sync",
        headers={"Accept": "text/html"},
        cookies={"access_token": token},
    )
    assert r.status_code == 200, r.text
    body = r.text
    # Page chrome
    assert "Sync" in body
    # Live history fetch path is wired
    assert "/api/sync/history" in body
    # Trigger button targets the existing endpoint
    assert "/api/sync/trigger" in body
    # Uses the global .data-table class introduced in Phase 3
    assert "data-table" in body
    # Guard against re-introducing raw confirm() — must use the modal
    assert "confirmDestructive" in body


def test_admin_sync_page_forbidden_for_analyst(fresh_db):
    from app.main import app

    _, token = _seed_user("analyst")
    client = TestClient(app)
    r = client.get(
        "/admin/sync",
        headers={"Accept": "text/html"},
        cookies={"access_token": token},
        follow_redirects=False,
    )
    # Analyst gets 302 (redirect to login or dashboard) or 403 — both are
    # accepted by the existing TestAdminRoleGuards pattern in test_web_ui.
    assert r.status_code in (302, 403), r.text


def test_admin_sync_page_unauthenticated_redirects(fresh_db):
    from app.main import app

    client = TestClient(app)
    r = client.get("/admin/sync", follow_redirects=False)
    assert r.status_code in (302, 401, 403)
