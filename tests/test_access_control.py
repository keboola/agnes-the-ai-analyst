"""E2E access control tests — verify data isolation between users via the
v19 resource_grants model.

Pre-v19 the data RBAC layer was effectively inactive — `is_public DEFAULT
true` plus no API/UI surface to flip the flag meant `can_access_table`
always bypassed. v19 dropped both `is_public` and `dataset_permissions`;
table access is now exclusively per-`(group, resource_type='table',
resource_id)` grants in `resource_grants`. Admin group members short-circuit
to True. Every other access requires an explicit grant.
"""

import os
import duckdb
import pytest

from tests.conftest import create_mock_extract


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _grant_table_to_analyst(conn, table_id: str, group_name: str = "analyst-grants") -> str:
    """Create (or reuse) a custom group, add analyst1 to it, mint a TABLE
    grant on `table_id`. Returns the group_id so callers can revoke later."""
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.resource_grants import ResourceGrantsRepository

    groups = UserGroupsRepository(conn)
    grp = groups.get_by_name(group_name)
    if not grp:
        grp = groups.create(name=group_name, description="test", created_by="test")
    members = UserGroupMembersRepository(conn)
    if not members.has_membership("analyst1", grp["id"]):
        members.add_member("analyst1", grp["id"], source="admin", added_by="test")
    grants = ResourceGrantsRepository(conn)
    if not grants.has_grant([grp["id"]], "table", table_id):
        grants.create(group_id=grp["id"], resource_type="table", resource_id=table_id,
                      assigned_by="test")
    return grp["id"]


def _revoke_all_table_grants(conn, table_id: str) -> None:
    from src.repositories.resource_grants import ResourceGrantsRepository
    ResourceGrantsRepository(conn).delete_by_resource("table", table_id)


class TestAdminBypass:
    """Admin group members see every registered table without explicit grants."""

    def test_admin_sees_all_tables_in_manifest(self, seeded_app):
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

        resp = c.get("/api/sync/manifest", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200

    def test_admin_can_download_any_table(self, seeded_app):
        env = seeded_app["env"]
        create_mock_extract(env["extracts_dir"], "keboola", [
            {"name": "salaries", "data": [{"id": "1"}]},
        ])
        from src.orchestrator import SyncOrchestrator
        SyncOrchestrator().rebuild()

        c = seeded_app["client"]
        c.post("/api/admin/register-table", json={
            "name": "salaries", "source_type": "keboola",
        }, headers=_auth(seeded_app["admin_token"]))

        resp = c.get("/api/data/salaries/download", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200


class TestNonAdminDeniedByDefault:
    """v19 fail-closed: non-admin users see nothing without explicit grants."""

    def test_analyst_cannot_see_ungranted_table_in_manifest(self, seeded_app):
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

        # No grant minted for analyst1 → table is invisible.
        resp = c.get("/api/sync/manifest", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        assert "orders" not in resp.json().get("tables", {})

    def test_analyst_blocked_from_downloading_ungranted_table(self, seeded_app):
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

        resp = c.get("/api/data/salaries/download",
                     headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 403

    def test_analyst_cannot_see_ungranted_table_in_catalog(self, seeded_app):
        c = seeded_app["client"]
        c.post("/api/admin/register-table", json={"name": "secret_data"},
               headers=_auth(seeded_app["admin_token"]))

        resp = c.get("/api/catalog/tables", headers=_auth(seeded_app["analyst_token"]))
        names = {t["name"] for t in resp.json()["tables"]}
        assert "secret_data" not in names


class TestExplicitGrants:
    """`POST /api/admin/grants` mints a `resource_grants(group, "table", id)`
    row; granting a group the user belongs to unlocks the table."""

    def test_grant_makes_table_visible_in_manifest(self, seeded_app):
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

        # Initially invisible.
        resp = c.get("/api/sync/manifest", headers=_auth(seeded_app["analyst_token"]))
        assert "salaries" not in resp.json().get("tables", {})

        from src.db import get_system_db
        conn = get_system_db()
        try:
            _grant_table_to_analyst(conn, "salaries")
        finally:
            conn.close()

        # Now visible.
        resp = c.get("/api/sync/manifest", headers=_auth(seeded_app["analyst_token"]))
        assert "salaries" in resp.json().get("tables", {})

    def test_grant_then_download(self, seeded_app):
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

        # Blocked before grant.
        resp = c.get("/api/data/salaries/download",
                     headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 403

        from src.db import get_system_db
        conn = get_system_db()
        try:
            _grant_table_to_analyst(conn, "salaries")
        finally:
            conn.close()

        # OK after grant.
        resp = c.get("/api/data/salaries/download",
                     headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200

    def test_revoke_blocks_access(self, seeded_app):
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
        try:
            _grant_table_to_analyst(conn, "salaries")
        finally:
            conn.close()

        resp = c.get("/api/data/salaries/download",
                     headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200

        # Revoke
        conn = get_system_db()
        try:
            _revoke_all_table_grants(conn, "salaries")
        finally:
            conn.close()

        resp = c.get("/api/data/salaries/download",
                     headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 403


class TestCatalogFiltering:
    """Catalog list reflects per-user grants. Admin sees everything; analyst
    sees only granted tables."""

    def test_admin_sees_all_in_catalog(self, seeded_app):
        c = seeded_app["client"]
        c.post("/api/admin/register-table", json={"name": "table_a"},
               headers=_auth(seeded_app["admin_token"]))
        c.post("/api/admin/register-table", json={"name": "table_b"},
               headers=_auth(seeded_app["admin_token"]))

        resp = c.get("/api/catalog/tables", headers=_auth(seeded_app["admin_token"]))
        names = {t["name"] for t in resp.json()["tables"]}
        assert {"table_a", "table_b"}.issubset(names)

    def test_analyst_sees_only_granted_tables_in_catalog(self, seeded_app):
        c = seeded_app["client"]
        c.post("/api/admin/register-table", json={"name": "granted_table"},
               headers=_auth(seeded_app["admin_token"]))
        c.post("/api/admin/register-table", json={"name": "ungranted_table"},
               headers=_auth(seeded_app["admin_token"]))

        from src.db import get_system_db
        conn = get_system_db()
        try:
            _grant_table_to_analyst(conn, "granted_table")
        finally:
            conn.close()

        resp = c.get("/api/catalog/tables", headers=_auth(seeded_app["analyst_token"]))
        names = {t["name"] for t in resp.json()["tables"]}
        assert "granted_table" in names
        assert "ungranted_table" not in names


class TestQueryFiltering:
    """`/api/query` blocks SQL referencing tables the user has no grant on."""

    def test_analyst_blocked_from_querying_ungranted_table(self, seeded_app):
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

        resp = c.post("/api/query", json={"sql": "SELECT * FROM salaries"},
                      headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 403

    def test_admin_can_query_any_table(self, seeded_app):
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

        resp = c.post("/api/query", json={"sql": "SELECT * FROM salaries"},
                      headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code != 403

    def test_granted_analyst_can_query(self, seeded_app):
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
        try:
            _grant_table_to_analyst(conn, "salaries")
        finally:
            conn.close()

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


class TestDownloadPathTraversal:
    """`/api/data/{table_id}/download` rejects unsafe table_id values."""

    def test_download_rejects_traversal_id(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/api/data/..%2Fetc/download",
                     headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 404

    def test_download_rejects_dotdot(self, seeded_app):
        c = seeded_app["client"]
        # FastAPI routing collapses backslash; use the URL-unsafe path arg
        resp = c.get("/api/data/foo%2F..%2Fbar/download",
                     headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 404

    def test_download_rejects_special_chars(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/api/data/foo%3Bbar/download",
                     headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 404

    def test_download_accepts_hyphenated_dotted_id(self, seeded_app):
        """Keboola-style ids (`in.c-crm.orders`) must pass the safety filter."""
        c = seeded_app["client"]
        # Just exercise the filter — table doesn't exist on disk so 404
        # is the expected outcome (NOT 422 / 400 from the safety check).
        resp = c.get("/api/data/in.c-crm.orders/download",
                     headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 404
