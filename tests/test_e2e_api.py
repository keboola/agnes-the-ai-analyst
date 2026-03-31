"""E2E API tests — full server-side flow via FastAPI TestClient."""

import os
import tempfile
from pathlib import Path

import duckdb
import pytest

from tests.conftest import create_mock_extract


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


class TestFullSyncFlow:
    """Complete flow: register -> extract -> manifest -> download."""

    def test_register_tables_and_get_catalog(self, seeded_app):
        c = seeded_app["client"]
        t = seeded_app["admin_token"]
        env = seeded_app["env"]

        # Register tables
        resp = c.post("/api/admin/register-table", json={
            "name": "orders", "source_type": "keboola", "bucket": "in.c-crm",
            "source_table": "orders", "query_mode": "local",
        }, headers=_auth(t))
        assert resp.status_code == 201

        resp = c.post("/api/admin/register-table", json={
            "name": "customers", "source_type": "keboola", "bucket": "in.c-crm",
            "source_table": "customers", "query_mode": "local",
        }, headers=_auth(t))
        assert resp.status_code == 201

        # Verify catalog
        resp = c.get("/api/catalog/tables", headers=_auth(t))
        assert resp.status_code == 200
        tables = resp.json()["tables"]
        names = {tbl["name"] for tbl in tables}
        assert "orders" in names
        assert "customers" in names

    def test_manifest_after_extract(self, seeded_app):
        c = seeded_app["client"]
        t = seeded_app["admin_token"]
        env = seeded_app["env"]

        # Create mock extract with real data
        create_mock_extract(env["extracts_dir"], "keboola", [
            {"name": "orders", "data": [
                {"id": "1", "product": "Widget", "price": "99.99"},
                {"id": "2", "product": "Gadget", "price": "49.99"},
            ]},
            {"name": "customers", "data": [
                {"id": "1", "name": "Alice", "email": "alice@test.com"},
            ]},
        ])

        # Run orchestrator to populate sync_state
        from src.orchestrator import SyncOrchestrator
        SyncOrchestrator().rebuild()

        # Check manifest
        resp = c.get("/api/sync/manifest", headers=_auth(t))
        assert resp.status_code == 200
        manifest = resp.json()
        assert "orders" in manifest["tables"]
        assert "customers" in manifest["tables"]
        assert manifest["tables"]["orders"]["rows"] == 2
        assert manifest["tables"]["customers"]["rows"] == 1
        assert "server_time" in manifest

    def test_download_parquet_and_verify_content(self, seeded_app):
        c = seeded_app["client"]
        t = seeded_app["admin_token"]
        env = seeded_app["env"]

        # Create extract
        create_mock_extract(env["extracts_dir"], "keboola", [
            {"name": "orders", "data": [
                {"id": "1", "product": "Widget", "price": "99.99"},
                {"id": "2", "product": "Gadget", "price": "49.99"},
            ]},
        ])

        # Download parquet
        resp = c.get("/api/data/orders/download", headers=_auth(t))
        assert resp.status_code == 200
        assert "application/octet-stream" in resp.headers.get("content-type", "")

        # Verify content by writing to temp file and reading with DuckDB
        with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as f:
            f.write(resp.content)
            tmp_path = f.name

        try:
            conn = duckdb.connect()
            rows = conn.execute(f"SELECT * FROM read_parquet('{tmp_path}') ORDER BY id").fetchall()
            conn.close()
            assert len(rows) == 2
            assert rows[0][1] == "Widget"  # product column
            assert rows[1][1] == "Gadget"
        finally:
            os.unlink(tmp_path)

    def test_download_nonexistent_table_404(self, seeded_app):
        c = seeded_app["client"]
        t = seeded_app["admin_token"]
        resp = c.get("/api/data/nonexistent/download", headers=_auth(t))
        assert resp.status_code == 404


class TestRBACEnforcement:
    """Verify role-based access control across API endpoints."""

    def test_analyst_cannot_register_table(self, seeded_app):
        c = seeded_app["client"]
        t = seeded_app["analyst_token"]
        resp = c.post("/api/admin/register-table", json={
            "name": "test", "source_type": "keboola",
        }, headers=_auth(t))
        assert resp.status_code == 403

    def test_analyst_can_read_manifest(self, seeded_app):
        c = seeded_app["client"]
        t = seeded_app["analyst_token"]
        resp = c.get("/api/sync/manifest", headers=_auth(t))
        assert resp.status_code == 200

    def test_analyst_can_download_data(self, seeded_app):
        c = seeded_app["client"]
        env = seeded_app["env"]
        create_mock_extract(env["extracts_dir"], "keboola", [
            {"name": "orders", "data": [{"id": "1"}]},
        ])
        t = seeded_app["analyst_token"]
        resp = c.get("/api/data/orders/download", headers=_auth(t))
        assert resp.status_code == 200

    def test_admin_can_trigger_sync(self, seeded_app):
        c = seeded_app["client"]
        t = seeded_app["admin_token"]
        resp = c.post("/api/sync/trigger", headers=_auth(t))
        assert resp.status_code == 200

    def test_analyst_cannot_trigger_sync(self, seeded_app):
        c = seeded_app["client"]
        t = seeded_app["analyst_token"]
        resp = c.post("/api/sync/trigger", headers=_auth(t))
        assert resp.status_code == 403

    def test_unauthenticated_blocked(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/api/sync/manifest")
        assert resp.status_code == 401


class TestTableLifecycle:
    """Register -> update -> delete table via admin API."""

    def test_full_lifecycle(self, seeded_app):
        c = seeded_app["client"]
        t = seeded_app["admin_token"]

        # Create
        resp = c.post("/api/admin/register-table", json={
            "name": "lifecycle_test", "source_type": "keboola",
            "query_mode": "local", "description": "Test table",
        }, headers=_auth(t))
        assert resp.status_code == 201
        table_id = resp.json()["id"]

        # Read
        resp = c.get("/api/admin/registry", headers=_auth(t))
        assert resp.status_code == 200
        names = {tbl["name"] for tbl in resp.json()["tables"]}
        assert "lifecycle_test" in names

        # Update
        resp = c.put(f"/api/admin/registry/{table_id}", json={
            "query_mode": "remote",
        }, headers=_auth(t))
        assert resp.status_code == 200

        # Verify update
        resp = c.get("/api/admin/registry", headers=_auth(t))
        table = next(tbl for tbl in resp.json()["tables"] if tbl["id"] == table_id)
        assert table["query_mode"] == "remote"

        # Delete
        resp = c.delete(f"/api/admin/registry/{table_id}", headers=_auth(t))
        assert resp.status_code == 204

        # Verify gone
        resp = c.get("/api/admin/registry", headers=_auth(t))
        ids = {tbl["id"] for tbl in resp.json()["tables"]}
        assert table_id not in ids
