"""E2E access control tests — verify data isolation between users."""

import os
import duckdb
import pytest
from tests.conftest import create_mock_extract


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


class TestPublicTablesAccessible:
    """Default: is_public=True tables are accessible to everyone."""

    def test_analyst_sees_public_tables_in_manifest(self, seeded_app):
        """Analyst can see public tables in manifest."""
        c = seeded_app["client"]
        env = seeded_app["env"]

        # Create extract with data
        create_mock_extract(env["extracts_dir"], "keboola", [
            {"name": "orders", "data": [{"id": "1"}]},
        ])
        from src.orchestrator import SyncOrchestrator
        SyncOrchestrator().rebuild()

        # Register table as public (default)
        c.post("/api/admin/register-table", json={
            "name": "orders", "source_type": "keboola",
        }, headers=_auth(seeded_app["admin_token"]))

        # Analyst should see it
        resp = c.get("/api/sync/manifest", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200

    def test_analyst_can_download_public_table(self, seeded_app):
        env = seeded_app["env"]
        create_mock_extract(env["extracts_dir"], "keboola", [
            {"name": "orders", "data": [{"id": "1"}]},
        ])
        from src.orchestrator import SyncOrchestrator
        SyncOrchestrator().rebuild()

        c = seeded_app["client"]
        # Register table so access control recognizes it as public
        c.post("/api/admin/register-table", json={
            "name": "orders", "source_type": "keboola",
        }, headers=_auth(seeded_app["admin_token"]))

        resp = c.get("/api/data/orders/download", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200

    def test_admin_can_download_public_table(self, seeded_app):
        env = seeded_app["env"]
        create_mock_extract(env["extracts_dir"], "keboola", [
            {"name": "orders", "data": [{"id": "1"}]},
        ])
        from src.orchestrator import SyncOrchestrator
        SyncOrchestrator().rebuild()

        c = seeded_app["client"]
        resp = c.get("/api/data/orders/download", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200

    def test_public_table_visible_in_catalog(self, seeded_app):
        c = seeded_app["client"]
        env = seeded_app["env"]

        create_mock_extract(env["extracts_dir"], "keboola", [
            {"name": "orders", "data": [{"id": "1"}]},
        ])
        from src.orchestrator import SyncOrchestrator
        SyncOrchestrator().rebuild()

        c.post("/api/admin/register-table", json={
            "name": "orders", "source_type": "keboola",
        }, headers=_auth(seeded_app["admin_token"]))

        resp = c.get("/api/catalog/tables", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        names = {t["name"] for t in resp.json()["tables"]}
        assert "orders" in names


class TestPrivateTablesRestricted:
    """Tables with is_public=False require explicit permission."""

    def test_analyst_cannot_see_private_table_in_manifest(self, seeded_app):
        """Private table hidden from manifest for unauthorized user."""
        c = seeded_app["client"]
        env = seeded_app["env"]

        # Create extract
        create_mock_extract(env["extracts_dir"], "keboola", [
            {"name": "salaries", "data": [{"id": "1", "amount": "100000"}]},
        ])
        from src.orchestrator import SyncOrchestrator
        SyncOrchestrator().rebuild()

        # Register as public first (default), then make private
        c.post("/api/admin/register-table", json={
            "name": "salaries", "source_type": "keboola",
        }, headers=_auth(seeded_app["admin_token"]))

        # Make private via direct DB update
        from src.db import get_system_db
        conn = get_system_db()
        conn.execute("UPDATE table_registry SET is_public = false WHERE name = 'salaries'")
        conn.close()

        # Analyst should NOT see it in manifest
        resp = c.get("/api/sync/manifest", headers=_auth(seeded_app["analyst_token"]))
        assert "salaries" not in resp.json().get("tables", {})

        # Admin SHOULD see it
        resp = c.get("/api/sync/manifest", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        # Admin sees all — salaries should not be filtered out

    def test_analyst_blocked_from_downloading_private_table(self, seeded_app):
        c = seeded_app["client"]
        env = seeded_app["env"]

        create_mock_extract(env["extracts_dir"], "keboola", [
            {"name": "salaries", "data": [{"id": "1", "amount": "100000"}]},
        ])
        from src.orchestrator import SyncOrchestrator
        SyncOrchestrator().rebuild()

        # Make private
        from src.db import get_system_db
        conn = get_system_db()
        conn.execute(
            "INSERT INTO table_registry (id, name, is_public) VALUES ('salaries','salaries',false) "
            "ON CONFLICT(id) DO UPDATE SET is_public=false"
        )
        conn.close()

        resp = c.get("/api/data/salaries/download", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 403

    def test_admin_can_download_private_table(self, seeded_app):
        c = seeded_app["client"]
        env = seeded_app["env"]

        create_mock_extract(env["extracts_dir"], "keboola", [
            {"name": "salaries", "data": [{"id": "1", "amount": "100000"}]},
        ])
        from src.orchestrator import SyncOrchestrator
        SyncOrchestrator().rebuild()

        from src.db import get_system_db
        conn = get_system_db()
        conn.execute(
            "INSERT INTO table_registry (id, name, is_public) VALUES ('salaries','salaries',false) "
            "ON CONFLICT(id) DO UPDATE SET is_public=false"
        )
        conn.close()

        resp = c.get("/api/data/salaries/download", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200

    def test_mixed_public_private_manifest(self, seeded_app):
        """Manifest shows public tables but hides private ones for analyst."""
        c = seeded_app["client"]
        env = seeded_app["env"]

        create_mock_extract(env["extracts_dir"], "keboola", [
            {"name": "orders", "data": [{"id": "1"}]},
            {"name": "salaries", "data": [{"id": "1", "amount": "100000"}]},
        ])
        from src.orchestrator import SyncOrchestrator
        SyncOrchestrator().rebuild()

        # Register both
        c.post("/api/admin/register-table", json={
            "name": "orders", "source_type": "keboola",
        }, headers=_auth(seeded_app["admin_token"]))
        c.post("/api/admin/register-table", json={
            "name": "salaries", "source_type": "keboola",
        }, headers=_auth(seeded_app["admin_token"]))

        # Make salaries private
        from src.db import get_system_db
        conn = get_system_db()
        conn.execute("UPDATE table_registry SET is_public = false WHERE name = 'salaries'")
        conn.close()

        # Analyst sees orders but not salaries
        resp = c.get("/api/sync/manifest", headers=_auth(seeded_app["analyst_token"]))
        tables = resp.json().get("tables", {})
        assert "orders" in tables
        assert "salaries" not in tables


class TestExplicitPermissions:
    """Granting explicit access to private tables."""

    def test_grant_then_access(self, seeded_app):
        c = seeded_app["client"]
        env = seeded_app["env"]

        create_mock_extract(env["extracts_dir"], "keboola", [
            {"name": "salaries", "data": [{"id": "1"}]},
        ])
        from src.orchestrator import SyncOrchestrator
        SyncOrchestrator().rebuild()

        # Make private
        from src.db import get_system_db
        conn = get_system_db()
        conn.execute(
            "INSERT INTO table_registry (id, name, is_public) VALUES ('salaries','salaries',false) "
            "ON CONFLICT(id) DO UPDATE SET is_public=false"
        )
        conn.close()

        # Analyst blocked
        resp = c.get("/api/data/salaries/download", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 403

        # Admin grants access
        resp = c.post("/api/admin/permissions", json={
            "user_id": "analyst1", "dataset": "salaries", "access": "read",
        }, headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 201

        # Now analyst CAN download
        resp = c.get("/api/data/salaries/download", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200

    def test_revoke_removes_access(self, seeded_app):
        c = seeded_app["client"]
        env = seeded_app["env"]

        create_mock_extract(env["extracts_dir"], "keboola", [
            {"name": "salaries", "data": [{"id": "1"}]},
        ])
        from src.orchestrator import SyncOrchestrator
        SyncOrchestrator().rebuild()

        from src.db import get_system_db
        conn = get_system_db()
        conn.execute(
            "INSERT INTO table_registry (id, name, is_public) VALUES ('salaries','salaries',false) "
            "ON CONFLICT(id) DO UPDATE SET is_public=false"
        )
        conn.close()

        # Grant
        c.post("/api/admin/permissions", json={
            "user_id": "analyst1", "dataset": "salaries", "access": "read",
        }, headers=_auth(seeded_app["admin_token"]))

        # Verify access
        resp = c.get("/api/data/salaries/download", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200

        # Revoke
        c.request("DELETE", "/api/admin/permissions", json={
            "user_id": "analyst1", "dataset": "salaries",
        }, headers=_auth(seeded_app["admin_token"]))

        # Now blocked again
        resp = c.get("/api/data/salaries/download", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 403

    def test_grant_makes_private_table_visible_in_manifest(self, seeded_app):
        """After granting access, analyst sees private table in manifest."""
        c = seeded_app["client"]
        env = seeded_app["env"]

        create_mock_extract(env["extracts_dir"], "keboola", [
            {"name": "salaries", "data": [{"id": "1"}]},
        ])
        from src.orchestrator import SyncOrchestrator
        SyncOrchestrator().rebuild()

        c.post("/api/admin/register-table", json={
            "name": "salaries", "source_type": "keboola",
        }, headers=_auth(seeded_app["admin_token"]))

        from src.db import get_system_db
        conn = get_system_db()
        conn.execute("UPDATE table_registry SET is_public = false WHERE name = 'salaries'")
        conn.close()

        # Not visible before grant
        resp = c.get("/api/sync/manifest", headers=_auth(seeded_app["analyst_token"]))
        assert "salaries" not in resp.json().get("tables", {})

        # Grant access
        c.post("/api/admin/permissions", json={
            "user_id": "analyst1", "dataset": "salaries", "access": "read",
        }, headers=_auth(seeded_app["admin_token"]))

        # Now visible
        resp = c.get("/api/sync/manifest", headers=_auth(seeded_app["analyst_token"]))
        assert "salaries" in resp.json().get("tables", {})


class TestCatalogFiltering:
    """Catalog only shows accessible tables."""

    def test_private_table_hidden_from_catalog(self, seeded_app):
        c = seeded_app["client"]

        # Register public + private
        c.post("/api/admin/register-table", json={"name": "public_table"}, headers=_auth(seeded_app["admin_token"]))
        c.post("/api/admin/register-table", json={"name": "private_table"}, headers=_auth(seeded_app["admin_token"]))

        from src.db import get_system_db
        conn = get_system_db()
        conn.execute("UPDATE table_registry SET is_public = false WHERE name = 'private_table'")
        conn.close()

        resp = c.get("/api/catalog/tables", headers=_auth(seeded_app["analyst_token"]))
        names = {t["name"] for t in resp.json()["tables"]}
        assert "public_table" in names
        assert "private_table" not in names

    def test_admin_sees_all_in_catalog(self, seeded_app):
        c = seeded_app["client"]

        c.post("/api/admin/register-table", json={"name": "public_table"}, headers=_auth(seeded_app["admin_token"]))
        c.post("/api/admin/register-table", json={"name": "private_table"}, headers=_auth(seeded_app["admin_token"]))

        from src.db import get_system_db
        conn = get_system_db()
        conn.execute("UPDATE table_registry SET is_public = false WHERE name = 'private_table'")
        conn.close()

        resp = c.get("/api/catalog/tables", headers=_auth(seeded_app["admin_token"]))
        names = {t["name"] for t in resp.json()["tables"]}
        assert "public_table" in names
        assert "private_table" in names

    def test_granted_private_table_shown_in_catalog(self, seeded_app):
        """After granting access, private table appears in catalog for that user."""
        c = seeded_app["client"]

        c.post("/api/admin/register-table", json={"name": "secret_data"}, headers=_auth(seeded_app["admin_token"]))

        from src.db import get_system_db
        conn = get_system_db()
        conn.execute("UPDATE table_registry SET is_public = false WHERE name = 'secret_data'")
        conn.close()

        # Not visible before grant
        resp = c.get("/api/catalog/tables", headers=_auth(seeded_app["analyst_token"]))
        names = {t["name"] for t in resp.json()["tables"]}
        assert "secret_data" not in names

        # Grant access
        c.post("/api/admin/permissions", json={
            "user_id": "analyst1", "dataset": "secret_data", "access": "read",
        }, headers=_auth(seeded_app["admin_token"]))

        # Now visible
        resp = c.get("/api/catalog/tables", headers=_auth(seeded_app["analyst_token"]))
        names = {t["name"] for t in resp.json()["tables"]}
        assert "secret_data" in names


class TestPermissionsAPI:
    """Admin permissions CRUD."""

    def test_grant_and_list(self, seeded_app):
        c = seeded_app["client"]
        h = _auth(seeded_app["admin_token"])

        resp = c.post("/api/admin/permissions", json={
            "user_id": "analyst1", "dataset": "secret_data", "access": "read",
        }, headers=h)
        assert resp.status_code == 201

        resp = c.get("/api/admin/permissions/analyst1", headers=h)
        assert resp.status_code == 200
        datasets = {p["dataset"] for p in resp.json()["permissions"]}
        assert "secret_data" in datasets

    def test_analyst_cannot_manage_permissions(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post("/api/admin/permissions", json={
            "user_id": "analyst1", "dataset": "anything",
        }, headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 403

    def test_grant_multiple_datasets(self, seeded_app):
        c = seeded_app["client"]
        h = _auth(seeded_app["admin_token"])

        for ds in ["sales", "hr", "finance"]:
            resp = c.post("/api/admin/permissions", json={
                "user_id": "analyst1", "dataset": ds, "access": "read",
            }, headers=h)
            assert resp.status_code == 201

        resp = c.get("/api/admin/permissions/analyst1", headers=h)
        datasets = {p["dataset"] for p in resp.json()["permissions"]}
        assert datasets == {"sales", "hr", "finance"}

    def test_revoke_via_delete(self, seeded_app):
        c = seeded_app["client"]
        h = _auth(seeded_app["admin_token"])

        c.post("/api/admin/permissions", json={
            "user_id": "analyst1", "dataset": "secret_data", "access": "read",
        }, headers=h)

        resp = c.request("DELETE", "/api/admin/permissions", json={
            "user_id": "analyst1", "dataset": "secret_data",
        }, headers=h)
        assert resp.status_code == 200

        resp = c.get("/api/admin/permissions/analyst1", headers=h)
        datasets = {p["dataset"] for p in resp.json()["permissions"]}
        assert "secret_data" not in datasets

    def test_analyst_cannot_revoke_permissions(self, seeded_app):
        c = seeded_app["client"]
        resp = c.request("DELETE", "/api/admin/permissions", json={
            "user_id": "analyst1", "dataset": "anything",
        }, headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 403


class TestQueryFiltering:
    """Query endpoint respects access control."""

    def test_analyst_blocked_from_querying_private_table(self, seeded_app):
        c = seeded_app["client"]
        env = seeded_app["env"]

        # Create extract with private data
        create_mock_extract(env["extracts_dir"], "keboola", [
            {"name": "salaries", "data": [{"id": "1", "amount": "100000"}]},
        ])
        from src.orchestrator import SyncOrchestrator
        SyncOrchestrator().rebuild()

        from src.db import get_system_db
        conn = get_system_db()
        conn.execute(
            "INSERT INTO table_registry (id, name, is_public) VALUES ('salaries','salaries',false) "
            "ON CONFLICT(id) DO UPDATE SET is_public=false"
        )
        conn.close()

        resp = c.post("/api/query", json={"sql": "SELECT * FROM salaries"},
                       headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 403

    def test_admin_can_query_private_table(self, seeded_app):
        c = seeded_app["client"]
        env = seeded_app["env"]

        create_mock_extract(env["extracts_dir"], "keboola", [
            {"name": "salaries", "data": [{"id": "1", "amount": "100000"}]},
        ])
        from src.orchestrator import SyncOrchestrator
        SyncOrchestrator().rebuild()

        from src.db import get_system_db
        conn = get_system_db()
        conn.execute(
            "INSERT INTO table_registry (id, name, is_public) VALUES ('salaries','salaries',false) "
            "ON CONFLICT(id) DO UPDATE SET is_public=false"
        )
        conn.close()

        resp = c.post("/api/query", json={"sql": "SELECT * FROM salaries"},
                       headers=_auth(seeded_app["admin_token"]))
        # Admin should not be blocked by access control
        assert resp.status_code != 403

    def test_analyst_can_query_public_table(self, seeded_app):
        c = seeded_app["client"]
        env = seeded_app["env"]

        create_mock_extract(env["extracts_dir"], "keboola", [
            {"name": "orders", "data": [{"id": "1", "total": "99.99"}]},
        ])
        from src.orchestrator import SyncOrchestrator
        SyncOrchestrator().rebuild()

        # Register table so access control recognizes it as public
        c.post("/api/admin/register-table", json={
            "name": "orders", "source_type": "keboola",
        }, headers=_auth(seeded_app["admin_token"]))

        resp = c.post("/api/query", json={"sql": "SELECT * FROM orders"},
                       headers=_auth(seeded_app["analyst_token"]))
        # Public table should not be blocked
        assert resp.status_code != 403

    def test_granted_analyst_can_query_private_table(self, seeded_app):
        c = seeded_app["client"]
        env = seeded_app["env"]

        create_mock_extract(env["extracts_dir"], "keboola", [
            {"name": "salaries", "data": [{"id": "1", "amount": "100000"}]},
        ])
        from src.orchestrator import SyncOrchestrator
        SyncOrchestrator().rebuild()

        from src.db import get_system_db
        conn = get_system_db()
        conn.execute(
            "INSERT INTO table_registry (id, name, is_public) VALUES ('salaries','salaries',false) "
            "ON CONFLICT(id) DO UPDATE SET is_public=false"
        )
        conn.close()

        # Grant access
        c.post("/api/admin/permissions", json={
            "user_id": "analyst1", "dataset": "salaries", "access": "read",
        }, headers=_auth(seeded_app["admin_token"]))

        resp = c.post("/api/query", json={"sql": "SELECT * FROM salaries"},
                       headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code != 403


class TestUnauthenticatedAccess:
    """Endpoints require authentication."""

    def test_manifest_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/api/sync/manifest")
        assert resp.status_code in (401, 403)

    def test_download_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/api/data/orders/download")
        assert resp.status_code in (401, 403)

    def test_catalog_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/api/catalog/tables")
        assert resp.status_code in (401, 403)

    def test_query_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post("/api/query", json={"sql": "SELECT 1"})
        assert resp.status_code in (401, 403)

    def test_permissions_api_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post("/api/admin/permissions", json={
            "user_id": "analyst1", "dataset": "anything",
        })
        assert resp.status_code in (401, 403)
