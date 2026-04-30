"""Tests for the /admin/settings page (Phase 5).

The page is a thin shell over the existing per-user settings APIs
(``/api/settings``, ``/api/settings/dataset``, ``/api/sync/table-subscriptions``).
Pin: route renders for admin, redirects/forbids analyst, references the
expected endpoint paths so a renderer regression that drops the JS
fetches surfaces in CI.
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


def test_admin_settings_renders_for_admin(fresh_db):
    from app.main import app

    _, token = _seed_user("admin")
    client = TestClient(app)
    r = client.get(
        "/admin/settings",
        headers={"Accept": "text/html"},
        cookies={"access_token": token},
    )
    assert r.status_code == 200, r.text
    body = r.text
    # Page chrome
    assert "Settings" in body
    # JS wires the three existing endpoints — pin so a refactor that
    # drops a section's fetch shows up in CI.
    assert "/api/settings" in body
    assert "/api/settings/dataset" in body
    assert "/api/sync/table-subscriptions" in body


def test_admin_settings_forbidden_for_analyst(fresh_db):
    from app.main import app

    _, token = _seed_user("analyst")
    client = TestClient(app)
    r = client.get(
        "/admin/settings",
        headers={"Accept": "text/html"},
        cookies={"access_token": token},
        follow_redirects=False,
    )
    assert r.status_code in (302, 403), r.text


def test_admin_settings_unauthenticated_redirects(fresh_db):
    from app.main import app

    client = TestClient(app)
    r = client.get("/admin/settings", follow_redirects=False)
    assert r.status_code in (302, 401, 403)
