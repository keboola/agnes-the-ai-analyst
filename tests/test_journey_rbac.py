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
    """Stack-gated RBAC: wrap the table in an auto data_package and
    grant the package to a custom group the user is in. Returns the
    grant id so callers that revoke by grant id continue to work.
    """
    from tests.conftest import grant_table_via_package
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_groups import UserGroupsRepository
    pkg_id = grant_table_via_package(
        conn, table_id, user_id, group_name=f"j-{user_id}",
    )
    grp = UserGroupsRepository(conn).get_by_name(f"j-{user_id}")
    existing = next(
        g for g in ResourceGrantsRepository(conn)
            .list_for_groups([grp["id"]], "data_package")
        if g["resource_id"] == pkg_id
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
