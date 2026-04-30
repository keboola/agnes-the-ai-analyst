"""J4 — RBAC journey tests (v19+ resource_grants flow).

Full permission lifecycle: analyst denied by default → admin grants via
resource_grants → analyst can query → admin revokes → blocked again.
The legacy /api/admin/permissions and /api/access-requests/* endpoints
were removed in v19 along with `is_public`.
"""

import pytest
from tests.conftest import create_mock_extract


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _grant_table(conn, user_id: str, table_id: str) -> str:
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.resource_grants import ResourceGrantsRepository
    grp = UserGroupsRepository(conn).get_by_name(f"j-{user_id}")
    if not grp:
        grp = UserGroupsRepository(conn).create(
            name=f"j-{user_id}", description="journey", created_by="test",
        )
    members = UserGroupMembersRepository(conn)
    if not members.has_membership(user_id, grp["id"]):
        members.add_member(user_id, grp["id"], source="admin", added_by="test")
    grants = ResourceGrantsRepository(conn)
    if not grants.has_grant([grp["id"]], "table", table_id):
        return grants.create(
            group_id=grp["id"], resource_type="table", resource_id=table_id,
            assigned_by="test",
        )
    existing = next(
        g for g in grants.list_for_groups([grp["id"]], "table")
        if g["resource_id"] == table_id
    )
    return existing["id"]


@pytest.mark.journey
class TestRBACJourney:
    def _setup_table(self, seeded_app, mock_extract_factory):
        c = seeded_app["client"]
        env = seeded_app["env"]
        c.post(
            "/api/admin/register-table",
            json={
                "name": "private_data",
                "source_type": "keboola",
                "query_mode": "local",
                "description": "Private dataset",
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        mock_extract_factory(
            "keboola",
            [{"name": "private_data", "data": [{"id": "1", "secret": "top_secret"}]}],
        )
        from src.orchestrator import SyncOrchestrator
        SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

    def test_analyst_blocked_without_grant(self, seeded_app, mock_extract_factory):
        c = seeded_app["client"]
        self._setup_table(seeded_app, mock_extract_factory)
        resp = c.post(
            "/api/query",
            json={"sql": "SELECT * FROM private_data"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403

    def test_admin_grants_then_analyst_can_query(self, seeded_app, mock_extract_factory):
        c = seeded_app["client"]
        self._setup_table(seeded_app, mock_extract_factory)
        # Admin grants TABLE access via resource_grants
        from src.db import get_system_db
        conn = get_system_db()
        try:
            _grant_table(conn, "analyst1", "private_data")
        finally:
            conn.close()
        resp = c.post(
            "/api/query",
            json={"sql": "SELECT * FROM private_data"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 200

    def test_admin_revokes_grant_blocks_analyst(self, seeded_app, mock_extract_factory):
        c = seeded_app["client"]
        self._setup_table(seeded_app, mock_extract_factory)

        from src.db import get_system_db
        conn = get_system_db()
        try:
            grant_id = _grant_table(conn, "analyst1", "private_data")
        finally:
            conn.close()

        # Verify analyst can query
        resp = c.post(
            "/api/query",
            json={"sql": "SELECT * FROM private_data"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 200

        # Admin revokes the grant via REST API
        resp = c.delete(
            f"/api/admin/grants/{grant_id}",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 204

        # Analyst is blocked again
        resp = c.post(
            "/api/query",
            json={"sql": "SELECT * FROM private_data"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403

    def test_admin_can_query_any_table(self, seeded_app, mock_extract_factory):
        """Admin shortcut: members of the Admin group never need explicit grants."""
        c = seeded_app["client"]
        self._setup_table(seeded_app, mock_extract_factory)
        resp = c.post(
            "/api/query",
            json={"sql": "SELECT * FROM private_data"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
