"""Tests for the /admin/metrics list + /admin/metrics/{id} detail pages
(Phase 6).

Read-only surface — editing remains in ``da metrics import``. Pin: route
renders for admin, redirects/forbids analyst, 404s for missing IDs,
template references the SQL block + the canonical-name id.
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


def _seed_metric(id="finance/mrr", name="mrr", display_name="MRR", category="finance"):
    from src.db import get_system_db
    from src.repositories.metrics import MetricRepository

    conn = get_system_db()
    try:
        MetricRepository(conn).create(
            id=id, name=name, display_name=display_name,
            category=category, description="Monthly recurring revenue.",
            type="ratio", unit="USD", grain="monthly",
            sql="SELECT date_trunc('month', created_at) AS month, SUM(amount) AS mrr FROM subscriptions GROUP BY 1",
        )
    finally:
        conn.close()


def test_list_renders_for_admin_with_metrics(fresh_db):
    from app.main import app

    _, token = _seed_user("admin")
    _seed_metric()

    client = TestClient(app)
    r = client.get(
        "/admin/metrics",
        headers={"Accept": "text/html"},
        cookies={"access_token": token},
    )
    assert r.status_code == 200, r.text
    body = r.text
    assert "Metrics" in body
    # The metric we seeded is rendered with its display name and id
    assert "MRR" in body
    assert "finance/mrr" in body
    # The "managed via CLI" notice is present
    assert "da metrics import" in body
    # Uses the global .data-table from Phase 3
    assert "data-table" in body


def test_list_renders_empty_state_when_no_metrics(fresh_db):
    from app.main import app

    _, token = _seed_user("admin")
    client = TestClient(app)
    r = client.get(
        "/admin/metrics",
        headers={"Accept": "text/html"},
        cookies={"access_token": token},
    )
    assert r.status_code == 200, r.text
    assert "No metric definitions yet" in r.text


def test_list_forbidden_for_analyst(fresh_db):
    from app.main import app

    _, token = _seed_user("analyst")
    client = TestClient(app)
    r = client.get(
        "/admin/metrics",
        headers={"Accept": "text/html"},
        cookies={"access_token": token},
        follow_redirects=False,
    )
    assert r.status_code in (302, 403)


def test_detail_renders_for_admin(fresh_db):
    from app.main import app

    _, token = _seed_user("admin")
    _seed_metric()

    client = TestClient(app)
    r = client.get(
        "/admin/metrics/finance/mrr",
        headers={"Accept": "text/html"},
        cookies={"access_token": token},
    )
    assert r.status_code == 200, r.text
    body = r.text
    assert "MRR" in body
    assert "finance/mrr" in body
    # SQL renders inside the page (in a <pre>)
    assert "date_trunc" in body
    # Read-only notice
    assert "da metrics import" in body


def test_detail_404_for_missing_metric(fresh_db):
    from app.main import app

    _, token = _seed_user("admin")
    client = TestClient(app)
    r = client.get(
        "/admin/metrics/nope/missing",
        headers={"Accept": "text/html"},
        cookies={"access_token": token},
    )
    assert r.status_code == 404, r.text


def test_detail_forbidden_for_analyst(fresh_db):
    from app.main import app

    _, token = _seed_user("analyst")
    _seed_metric()
    client = TestClient(app)
    r = client.get(
        "/admin/metrics/finance/mrr",
        headers={"Accept": "text/html"},
        cookies={"access_token": token},
        follow_redirects=False,
    )
    assert r.status_code in (302, 403)
