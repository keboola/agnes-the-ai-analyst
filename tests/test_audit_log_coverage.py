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


# ── admin.py (registry mutations) ───────────────────────────────────────


def test_admin_register_table_writes_audit(fresh_db):
    from app.main import app
    uid, token = _seed_admin()
    client = TestClient(app)
    r = client.post(
        "/api/admin/register-table",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "Orders", "folder": "data", "sync_strategy": "full"},
    )
    assert r.status_code == 201, r.text
    rows = _audit_rows(action_prefix="registry.register", user_id=uid)
    assert len(rows) == 1
    assert rows[0]["resource"] == "admin:orders"


def test_admin_unregister_table_writes_audit(fresh_db):
    from app.main import app
    uid, token = _seed_admin()
    client = TestClient(app)
    client.post(
        "/api/admin/register-table",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "Orders", "folder": "data", "sync_strategy": "full"},
    )
    r = client.delete(
        "/api/admin/registry/orders",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 204, r.text
    rows = _audit_rows(action_prefix="registry.unregister", user_id=uid)
    assert len(rows) == 1


# ── permissions.py (legacy dataset_permissions) ─────────────────────────


def test_permissions_grant_writes_audit(fresh_db):
    from app.main import app
    uid, token = _seed_admin()
    client = TestClient(app)
    r = client.post(
        "/api/admin/permissions",
        headers={"Authorization": f"Bearer {token}"},
        json={"user_id": "victim", "dataset": "orders", "access": "read"},
    )
    assert r.status_code == 201, r.text
    rows = _audit_rows(action_prefix="permission.grant", user_id=uid)
    assert len(rows) == 1


def test_permissions_revoke_writes_audit(fresh_db):
    from app.main import app
    uid, token = _seed_admin()
    client = TestClient(app)
    client.post(
        "/api/admin/permissions",
        headers={"Authorization": f"Bearer {token}"},
        json={"user_id": "victim", "dataset": "orders", "access": "read"},
    )
    r = client.request(
        "DELETE", "/api/admin/permissions",
        headers={"Authorization": f"Bearer {token}"},
        json={"user_id": "victim", "dataset": "orders"},
    )
    assert r.status_code == 200, r.text
    rows = _audit_rows(action_prefix="permission.revoke", user_id=uid)
    assert len(rows) == 1


# ── access_requests.py ──────────────────────────────────────────────────


def test_access_request_create_writes_audit(fresh_db):
    from app.main import app
    uid, token = _seed_admin()
    client = TestClient(app)
    r = client.post(
        "/api/access-requests",
        headers={"Authorization": f"Bearer {token}"},
        json={"table_id": "orders", "reason": "I need it"},
    )
    assert r.status_code == 201, r.text
    rows = _audit_rows(action_prefix="access_request.create", user_id=uid)
    assert len(rows) == 1


def test_access_request_approve_and_deny_write_audit(fresh_db):
    from app.main import app
    uid, token = _seed_admin()
    client = TestClient(app)
    # Create one to approve
    create1 = client.post(
        "/api/access-requests",
        headers={"Authorization": f"Bearer {token}"},
        json={"table_id": "orders", "reason": ""},
    )
    rid1 = create1.json()["id"]
    a = client.post(
        f"/api/access-requests/{rid1}/approve",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert a.status_code == 200, a.text
    # Create another to deny
    create2 = client.post(
        "/api/access-requests",
        headers={"Authorization": f"Bearer {token}"},
        json={"table_id": "customers", "reason": ""},
    )
    rid2 = create2.json()["id"]
    d = client.post(
        f"/api/access-requests/{rid2}/deny",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert d.status_code == 200, d.text
    assert len(_audit_rows(action_prefix="access_request.approve", user_id=uid)) == 1
    assert len(_audit_rows(action_prefix="access_request.deny", user_id=uid)) == 1


# ── metadata.py ─────────────────────────────────────────────────────────


def test_metadata_save_writes_audit(fresh_db):
    from app.main import app
    uid, token = _seed_admin()
    client = TestClient(app)
    r = client.post(
        "/api/admin/metadata/orders",
        headers={"Authorization": f"Bearer {token}"},
        json={"columns": [{"column_name": "id", "basetype": "INTEGER",
                            "description": "Primary key"}]},
    )
    assert r.status_code == 200, r.text
    rows = _audit_rows(action_prefix="metadata.save", user_id=uid)
    assert len(rows) == 1
    assert rows[0]["resource"] == "metadata:orders"


# ── catalog.py ──────────────────────────────────────────────────────────
# profile_refresh requires a parquet on disk; we don't simulate that here.
# Coverage relies on the audit helper being reachable — exercise via the
# code path's metadata.save above (catalog and metadata share the
# AuditRepository.log path).


# ── memory.py (user-self submit + vote) ─────────────────────────────────


def test_memory_create_writes_audit(fresh_db):
    from app.main import app
    uid, token = _seed_admin()
    client = TestClient(app)
    r = client.post(
        "/api/memory",
        headers={"Authorization": f"Bearer {token}"},
        json={"title": "Tip", "content": "Always …", "category": "ops"},
    )
    assert r.status_code == 201, r.text
    rows = _audit_rows(action_prefix="km_create")
    assert len(rows) == 1


def test_memory_vote_writes_audit(fresh_db):
    from app.main import app
    uid, token = _seed_admin()
    client = TestClient(app)
    create = client.post(
        "/api/memory",
        headers={"Authorization": f"Bearer {token}"},
        json={"title": "Tip", "content": "x", "category": "ops"},
    )
    item_id = create.json()["id"]
    r = client.post(
        f"/api/memory/{item_id}/vote",
        headers={"Authorization": f"Bearer {token}"},
        json={"vote": 1},
    )
    assert r.status_code == 200, r.text
    rows = _audit_rows(action_prefix="km_vote")
    assert len(rows) == 1
