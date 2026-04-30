"""Tests for /admin/audit page + GET /api/audit endpoint.

Replaces the previously-deleted /activity-center surface with a real
browser view over the ``audit_log`` table. The instrumentation in
``app/api/{scripts,metrics,sync,upload,admin,permissions,access_requests,
metadata,catalog,memory}.py`` writes the rows; this surface lets
operators slice + browse them.
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


def _seed_audit(action, user_id, resource="x", params=None):
    from src.db import get_system_db
    from src.repositories.audit import AuditRepository
    conn = get_system_db()
    try:
        AuditRepository(conn).log(
            user_id=user_id, action=action, resource=resource, params=params,
        )
    finally:
        conn.close()


# ── /api/audit endpoint ─────────────────────────────────────────────────


def test_audit_endpoint_admin_returns_list(fresh_db):
    from app.main import app
    uid, token = _seed_user("admin")
    _seed_audit("metric.upsert", uid, "metric:finance/mrr")
    _seed_audit("script.run", uid, "script:abc123")

    client = TestClient(app)
    r = client.get("/api/audit", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    rows = r.json()
    assert isinstance(rows, list)
    assert len(rows) == 2
    actions = {row["action"] for row in rows}
    assert actions == {"metric.upsert", "script.run"}
    for row in rows:
        assert isinstance(row["timestamp"], str)


def test_audit_endpoint_action_prefix_filter(fresh_db):
    from app.main import app
    uid, token = _seed_user("admin")
    _seed_audit("script.deploy", uid)
    _seed_audit("script.run", uid)
    _seed_audit("metric.upsert", uid)
    _seed_audit("sync.trigger", uid)

    client = TestClient(app)
    r = client.get(
        "/api/audit?action_prefix=script.",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    assert all(row["action"].startswith("script.") for row in rows)


def test_audit_endpoint_user_filter(fresh_db):
    from app.main import app
    uid, token = _seed_user("admin")
    other = str(uuid.uuid4())
    _seed_audit("metric.upsert", uid)
    _seed_audit("metric.delete", other)

    client = TestClient(app)
    r = client.get(
        f"/api/audit?user={uid}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["user_id"] == uid


def test_audit_endpoint_resource_filter(fresh_db):
    from app.main import app
    uid, token = _seed_user("admin")
    _seed_audit("metric.upsert", uid, "metric:finance/mrr")
    _seed_audit("metric.upsert", uid, "metric:sales/win-rate")

    client = TestClient(app)
    r = client.get(
        "/api/audit?resource=metric:finance/mrr",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["resource"] == "metric:finance/mrr"


def test_audit_endpoint_limit_clamped(fresh_db):
    from app.main import app
    uid, token = _seed_user("admin")
    for _ in range(5):
        _seed_audit("metric.upsert", uid)

    client = TestClient(app)
    r = client.get(
        "/api/audit?limit=2",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert len(r.json()) == 2


def test_audit_endpoint_admin_only(fresh_db):
    from app.main import app
    _, analyst_token = _seed_user("analyst")
    client = TestClient(app)
    r = client.get(
        "/api/audit",
        headers={"Authorization": f"Bearer {analyst_token}"},
    )
    assert r.status_code == 403


def test_audit_actions_endpoint_returns_distinct(fresh_db):
    from app.main import app
    uid, token = _seed_user("admin")
    _seed_audit("metric.upsert", uid)
    _seed_audit("metric.upsert", uid)
    _seed_audit("script.run", uid)

    client = TestClient(app)
    r = client.get(
        "/api/audit/actions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    actions = r.json()
    assert isinstance(actions, list)
    assert set(actions) == {"metric.upsert", "script.run"}


# ── /admin/audit page ───────────────────────────────────────────────────


def test_admin_audit_page_renders_for_admin(fresh_db):
    from app.main import app
    _, token = _seed_user("admin")
    client = TestClient(app)
    r = client.get(
        "/admin/audit",
        headers={"Accept": "text/html"},
        cookies={"access_token": token},
    )
    assert r.status_code == 200, r.text
    body = r.text
    assert "Audit log" in body
    assert "/api/audit" in body
    assert "data-table" in body
    assert "action_prefix" in body


def test_admin_audit_page_forbidden_for_analyst(fresh_db):
    from app.main import app
    _, token = _seed_user("analyst")
    client = TestClient(app)
    r = client.get(
        "/admin/audit",
        headers={"Accept": "text/html"},
        cookies={"access_token": token},
        follow_redirects=False,
    )
    assert r.status_code in (302, 403)


def test_admin_audit_page_unauthenticated_redirects(fresh_db):
    from app.main import app
    client = TestClient(app)
    r = client.get("/admin/audit", follow_redirects=False)
    assert r.status_code in (302, 401, 403)
