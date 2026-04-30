"""Pin audit_log writes for the high-blast-radius admin/user-mutating
endpoints that previously left no trace.

Modules covered:
  - app/api/scripts.py   : deploy / run_by_id / run_adhoc / delete
  - app/api/metrics.py   : create_or_update / delete / import
  - app/api/sync.py      : trigger / settings / table-subscriptions
  - app/api/upload.py    : sessions / artifacts / local-md

For each: trigger the endpoint as the appropriate role, then assert a
matching ``audit_log`` row exists. The row check is deliberately loose
(action prefix + user_id) so the tests survive minor parameter wording
changes without being brittle.
"""

from __future__ import annotations

import io
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


def _seed_admin():
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="admin@test", name="Admin", role="admin")
        admin_gid = conn.execute(
            "SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]
        ).fetchone()[0]
        UserGroupMembersRepository(conn).add_member(uid, admin_gid, source="system_seed")
        return uid, create_access_token(user_id=uid, email="admin@test", role="admin")
    finally:
        conn.close()


def _audit_rows(action_prefix: str = "", user_id: str = "") -> list[dict]:
    """Read audit_log rows directly. Returns rows matching action prefix
    + user_id (both optional). Used to assert a write happened without
    coupling tests to exact param values."""
    from src.db import get_system_db

    conn = get_system_db()
    try:
        sql = "SELECT * FROM audit_log WHERE 1=1"
        params: list = []
        if action_prefix:
            sql += " AND action LIKE ?"
            params.append(action_prefix + "%")
        if user_id:
            sql += " AND user_id = ?"
            params.append(user_id)
        rows = conn.execute(sql, params).fetchall()
        if not rows:
            return []
        cols = [d[0] for d in conn.description]
        return [dict(zip(cols, r)) for r in rows]
    finally:
        conn.close()


# ── scripts.py ──────────────────────────────────────────────────────────


def test_scripts_deploy_writes_audit(fresh_db):
    from app.main import app
    uid, token = _seed_admin()
    client = TestClient(app)
    # ``import sys`` is on the blocklist — use a benign script.
    r = client.post(
        "/api/scripts/deploy",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "ping", "source": "print('hello')"},
    )
    assert r.status_code == 201, r.text
    rows = _audit_rows(action_prefix="script.deploy", user_id=uid)
    assert len(rows) == 1
    assert rows[0]["resource"].startswith("script:")


def test_scripts_run_adhoc_writes_audit(fresh_db):
    from app.main import app
    uid, token = _seed_admin()
    client = TestClient(app)
    r = client.post(
        "/api/scripts/run",
        headers={"Authorization": f"Bearer {token}"},
        json={"source": "print(1)"},
    )
    assert r.status_code == 200, r.text
    rows = _audit_rows(action_prefix="script.run_adhoc", user_id=uid)
    assert len(rows) == 1


def test_scripts_delete_writes_audit(fresh_db):
    from app.main import app
    uid, token = _seed_admin()
    client = TestClient(app)
    deploy = client.post(
        "/api/scripts/deploy",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "ephemeral", "source": "print(2)"},
    )
    assert deploy.status_code == 201, deploy.text
    sid = deploy.json()["id"]
    r = client.delete(f"/api/scripts/{sid}", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 204, r.text
    rows = _audit_rows(action_prefix="script.delete", user_id=uid)
    assert len(rows) == 1
    assert rows[0]["resource"] == f"script:{sid}"


# ── metrics.py ──────────────────────────────────────────────────────────


def test_metrics_upsert_writes_audit(fresh_db):
    from app.main import app
    uid, token = _seed_admin()
    client = TestClient(app)
    r = client.post(
        "/api/admin/metrics",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "id": "finance/mrr", "name": "mrr", "display_name": "MRR",
            "category": "finance", "sql": "SELECT 1",
        },
    )
    assert r.status_code == 201, r.text
    rows = _audit_rows(action_prefix="metric.upsert", user_id=uid)
    assert len(rows) == 1
    assert rows[0]["resource"] == "metric:finance/mrr"


def test_metrics_delete_writes_audit(fresh_db):
    from app.main import app
    uid, token = _seed_admin()
    client = TestClient(app)
    client.post(
        "/api/admin/metrics",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "id": "finance/mrr", "name": "mrr", "display_name": "MRR",
            "category": "finance", "sql": "SELECT 1",
        },
    )
    r = client.delete(
        "/api/admin/metrics/finance/mrr",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    rows = _audit_rows(action_prefix="metric.delete", user_id=uid)
    assert len(rows) == 1


def test_metrics_import_writes_audit(fresh_db):
    import yaml
    from app.main import app
    uid, token = _seed_admin()
    payload = yaml.dump([{
        "name": "arr", "category": "finance", "display_name": "ARR",
        "sql": "SELECT 1", "grain": "yearly",
    }])
    client = TestClient(app)
    r = client.post(
        "/api/admin/metrics/import",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("metrics.yml", io.BytesIO(payload.encode()), "text/yaml")},
    )
    assert r.status_code == 200, r.text
    rows = _audit_rows(action_prefix="metric.import", user_id=uid)
    assert len(rows) == 1


# ── sync.py ─────────────────────────────────────────────────────────────


def test_sync_trigger_writes_audit(fresh_db):
    from app.main import app
    uid, token = _seed_admin()
    client = TestClient(app)
    r = client.post(
        "/api/sync/trigger",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    rows = _audit_rows(action_prefix="sync.trigger", user_id=uid)
    assert len(rows) == 1


def test_sync_settings_update_writes_audit(fresh_db):
    from app.main import app
    uid, token = _seed_admin()
    client = TestClient(app)
    r = client.post(
        "/api/sync/settings",
        headers={"Authorization": f"Bearer {token}"},
        json={"datasets": {"jira": True}},
    )
    assert r.status_code == 200, r.text
    rows = _audit_rows(action_prefix="sync.settings.update", user_id=uid)
    assert len(rows) == 1


def test_sync_subscriptions_update_writes_audit(fresh_db):
    from app.main import app
    uid, token = _seed_admin()
    client = TestClient(app)
    r = client.post(
        "/api/sync/table-subscriptions",
        headers={"Authorization": f"Bearer {token}"},
        json={"table_mode": "explicit", "tables": {"orders": True}},
    )
    assert r.status_code == 200, r.text
    rows = _audit_rows(action_prefix="sync.subscriptions.update", user_id=uid)
    assert len(rows) == 1


# ── upload.py ───────────────────────────────────────────────────────────


def test_upload_session_writes_audit(fresh_db):
    from app.main import app
    uid, token = _seed_admin()
    client = TestClient(app)
    r = client.post(
        "/api/upload/sessions",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("session.jsonl", io.BytesIO(b'{"ok":1}\n'), "application/jsonl")},
    )
    assert r.status_code == 200, r.text
    rows = _audit_rows(action_prefix="upload.session", user_id=uid)
    assert len(rows) == 1


def test_upload_artifact_writes_audit(fresh_db):
    from app.main import app
    uid, token = _seed_admin()
    client = TestClient(app)
    r = client.post(
        "/api/upload/artifacts",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("report.html", io.BytesIO(b"<html></html>"), "text/html")},
    )
    assert r.status_code == 200, r.text
    rows = _audit_rows(action_prefix="upload.artifact", user_id=uid)
    assert len(rows) == 1


def test_upload_local_md_writes_audit(fresh_db):
    from app.main import app
    uid, token = _seed_admin()
    client = TestClient(app)
    r = client.post(
        "/api/upload/local-md",
        headers={"Authorization": f"Bearer {token}"},
        json={"content": "# notes\n"},
    )
    assert r.status_code == 200, r.text
    rows = _audit_rows(action_prefix="upload.local_md", user_id=uid)
    assert len(rows) == 1
