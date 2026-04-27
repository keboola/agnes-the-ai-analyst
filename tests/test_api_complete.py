"""Tests for all new API endpoints — catalog, telegram, admin, governance, web UI."""

import os
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")

    from app.main import create_app
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from src.repositories.sync_settings import DatasetPermissionRepository
    from src.repositories.knowledge import KnowledgeRepository
    from app.auth.jwt import create_access_token

    from tests.helpers.auth import grant_admin

    conn = get_system_db()
    ur = UserRepository(conn)
    ur.create(id="admin1", email="admin@test.com", name="Admin", role="admin")
    ur.create(id="analyst1", email="analyst@test.com", name="Analyst", role="analyst")
    ur.create(id="km1", email="km@test.com", name="KM Admin", role="km_admin")
    # v12: memory governance endpoints (/api/memory/admin/...) are gated by
    # require_admin — km_admin role is no longer a thing. Putting km1 in the
    # Admin group keeps the existing TestGovernance fixture pattern working
    # (the role/group distinction is irrelevant for these tests; they only
    # exercise the admin path of the governance flow).
    grant_admin(conn, "admin1")
    grant_admin(conn, "km1")

    DatasetPermissionRepository(conn).grant("analyst1", "sales", "read")

    # Seed knowledge for governance tests
    kr = KnowledgeRepository(conn)
    kr.create(id="k1", title="MRR", content="Monthly revenue", category="metrics", status="pending")
    kr.create(id="k2", title="Churn", content="Customer churn", category="metrics", status="approved")
    conn.close()

    app = create_app()
    c = TestClient(app)
    return {
        "client": c,
        "admin": create_access_token("admin1", "admin@test.com", "admin"),
        "analyst": create_access_token("analyst1", "analyst@test.com", "analyst"),
        "km": create_access_token("km1", "km@test.com", "km_admin"),
    }


def _h(token):
    return {"Authorization": f"Bearer {token}"}


# ---- Catalog ----

class TestCatalog:
    def test_catalog_tables(self, client):
        resp = client["client"].get("/api/catalog/tables", headers=_h(client["analyst"]))
        assert resp.status_code == 200

    def test_catalog_profile_not_found(self, client):
        # Admin can see 404 for truly missing tables (bypasses access control)
        resp = client["client"].get("/api/catalog/profile/nonexistent", headers=_h(client["admin"]))
        assert resp.status_code == 404

    def test_catalog_profile_access_denied_for_analyst(self, client):
        # Non-registered (non-public) table returns 403 for analyst
        resp = client["client"].get("/api/catalog/profile/private_table", headers=_h(client["analyst"]))
        assert resp.status_code == 403

    def test_catalog_profile_refresh_access_denied_for_analyst(self, client):
        # Refresh endpoint also enforces access control
        resp = client["client"].post("/api/catalog/profile/private_table/refresh", headers=_h(client["analyst"]))
        assert resp.status_code == 403

    def test_catalog_profile_public_table_accessible_to_analyst(self, client):
        # Register a public table — analyst can access its profile (404 since no profile data)
        client["client"].post("/api/admin/register-table",
                               json={"name": "public_table", "source_type": "keboola"},
                               headers=_h(client["admin"]))
        resp = client["client"].get("/api/catalog/profile/public_table", headers=_h(client["analyst"]))
        assert resp.status_code == 404  # access granted, but no profile data yet


# ---- Telegram ----

class TestTelegram:
    def test_telegram_status_not_linked(self, client):
        resp = client["client"].get("/api/telegram/status", headers=_h(client["analyst"]))
        assert resp.status_code == 200
        assert resp.json()["linked"] is False

    def test_telegram_verify_invalid_code(self, client):
        resp = client["client"].post("/api/telegram/verify",
                                      json={"code": "INVALID"},
                                      headers=_h(client["analyst"]))
        assert resp.status_code == 400

    def test_telegram_unlink(self, client):
        resp = client["client"].post("/api/telegram/unlink", headers=_h(client["analyst"]))
        assert resp.status_code == 200


# ---- Admin Tables ----

class TestAdminTables:
    def test_list_registry_empty(self, client):
        resp = client["client"].get("/api/admin/registry", headers=_h(client["admin"]))
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_register_and_list(self, client):
        resp = client["client"].post("/api/admin/register-table",
                                      json={"name": "Orders", "folder": "sales", "sync_strategy": "incremental"},
                                      headers=_h(client["admin"]))
        assert resp.status_code == 201

        resp = client["client"].get("/api/admin/registry", headers=_h(client["admin"]))
        assert resp.json()["count"] == 1

    def test_register_duplicate(self, client):
        client["client"].post("/api/admin/register-table",
                               json={"name": "Test", "folder": "f"},
                               headers=_h(client["admin"]))
        resp = client["client"].post("/api/admin/register-table",
                                      json={"name": "Test", "folder": "f"},
                                      headers=_h(client["admin"]))
        assert resp.status_code == 409

    def test_unregister(self, client):
        client["client"].post("/api/admin/register-table",
                               json={"name": "Temp"},
                               headers=_h(client["admin"]))
        resp = client["client"].delete("/api/admin/registry/temp", headers=_h(client["admin"]))
        assert resp.status_code == 204

    def test_analyst_blocked(self, client):
        resp = client["client"].get("/api/admin/registry", headers=_h(client["analyst"]))
        assert resp.status_code == 403


# ---- Corporate Memory Governance ----

class TestGovernance:
    def test_approve(self, client):
        resp = client["client"].post("/api/memory/admin/approve?item_id=k1",
                                      headers=_h(client["km"]))
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"

    def test_reject(self, client):
        resp = client["client"].post("/api/memory/admin/reject?item_id=k1",
                                      json={"reason": "not relevant"},
                                      headers=_h(client["km"]))
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"

    def test_mandate(self, client):
        resp = client["client"].post("/api/memory/admin/mandate?item_id=k1",
                                      json={"reason": "critical", "audience": "all"},
                                      headers=_h(client["km"]))
        assert resp.status_code == 200
        assert resp.json()["status"] == "mandatory"

    def test_batch_action(self, client):
        resp = client["client"].post("/api/memory/admin/batch",
                                      json={"item_ids": ["k1", "k2"], "action": "approve"},
                                      headers=_h(client["km"]))
        assert resp.status_code == 200
        assert len(resp.json()["success"]) == 2

    def test_pending_queue(self, client):
        resp = client["client"].get("/api/memory/admin/pending", headers=_h(client["km"]))
        assert resp.status_code == 200

    def test_audit_log(self, client):
        # Do an action first
        client["client"].post("/api/memory/admin/approve?item_id=k1", headers=_h(client["km"]))
        resp = client["client"].get("/api/memory/admin/audit", headers=_h(client["km"]))
        assert resp.status_code == 200

    def test_analyst_blocked_from_governance(self, client):
        resp = client["client"].post("/api/memory/admin/approve?item_id=k1",
                                      headers=_h(client["analyst"]))
        assert resp.status_code == 403

    def test_stats(self, client):
        resp = client["client"].get("/api/memory/stats", headers=_h(client["analyst"]))
        assert resp.status_code == 200
        assert resp.json()["total"] == 2

    def test_my_votes(self, client):
        # Vote first
        client["client"].post("/api/memory/k2/vote", json={"vote": 1}, headers=_h(client["analyst"]))
        resp = client["client"].get("/api/memory/my-votes", headers=_h(client["analyst"]))
        assert resp.status_code == 200


# ---- Sync Settings (new naming) ----

class TestSyncSettings:
    def test_get_sync_settings(self, client):
        resp = client["client"].get("/api/sync/settings", headers=_h(client["analyst"]))
        assert resp.status_code == 200

    def test_update_sync_settings(self, client):
        resp = client["client"].post("/api/sync/settings",
                                      json={"datasets": {"sales": True}},
                                      headers=_h(client["analyst"]))
        assert resp.status_code == 200
        assert "sales" in resp.json()["updated"]

    def test_table_subscriptions(self, client):
        resp = client["client"].get("/api/sync/table-subscriptions", headers=_h(client["analyst"]))
        assert resp.status_code == 200


# ---- Web UI ----

class TestWebUI:
    def test_login_page(self, client):
        resp = client["client"].get("/login")
        assert resp.status_code == 200

    def test_root_redirects(self, client):
        resp = client["client"].get("/", follow_redirects=False)
        assert resp.status_code == 302

    def test_health_no_auth(self, client):
        resp = client["client"].get("/api/health")
        assert resp.status_code == 200


# ---- Upload ----

class TestUpload:
    def test_upload_rejects_oversized_file(self, client):
        import io
        large_data = b"x" * (50 * 1024 * 1024 + 1)
        resp = client["client"].post(
            "/api/upload/artifacts",
            files={"file": ("big.csv", io.BytesIO(large_data), "text/csv")},
            headers=_h(client["admin"]),
        )
        assert resp.status_code == 413

    def test_upload_does_not_leak_absolute_path(self, client):
        """Upload response should not contain absolute filesystem paths."""
        import io
        resp = client["client"].post(
            "/api/upload/artifacts",
            files={"file": ("test.txt", io.BytesIO(b"hello"), "text/plain")},
            headers=_h(client["admin"]),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert not data.get("path", "").startswith("/"), "Response should not leak absolute path"
        assert "filename" in data, "Response should contain filename"
