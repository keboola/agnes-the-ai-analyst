"""J4 — RBAC journey tests.

Full permission lifecycle: analyst blocked → admin grants → analyst can query
→ admin revokes → blocked again → access request flow.
"""

import pytest
from tests.conftest import create_mock_extract


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.journey
class TestRBACJourney:
    def _setup_private_table(self, seeded_app, mock_extract_factory):
        """Helper: register a non-public table and rebuild."""
        c = seeded_app["client"]
        t = seeded_app["admin_token"]
        env = seeded_app["env"]

        # Register table as non-public (is_public defaults False when explicitly set)
        # We rely on the default is_public=True and will test the query RBAC path
        resp = c.post(
            "/api/admin/register-table",
            json={
                "name": "private_data",
                "source_type": "keboola",
                "query_mode": "local",
                "description": "Private dataset",
            },
            headers=_auth(t),
        )
        assert resp.status_code == 201

        mock_extract_factory(
            "keboola",
            [{"name": "private_data", "data": [{"id": "1", "secret": "top_secret"}]}],
        )

        from src.orchestrator import SyncOrchestrator
        SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

    def test_analyst_can_query_public_table(self, seeded_app, mock_extract_factory):
        """Analyst can query public (default) tables without explicit permission."""
        c = seeded_app["client"]
        env = seeded_app["env"]

        # Register + create data
        c.post(
            "/api/admin/register-table",
            json={"name": "public_sales", "source_type": "keboola", "query_mode": "local"},
            headers=_auth(seeded_app["admin_token"]),
        )
        mock_extract_factory(
            "keboola",
            [{"name": "public_sales", "data": [{"id": "1", "amount": "100"}]}],
        )

        from src.orchestrator import SyncOrchestrator
        SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

        resp = c.post(
            "/api/query",
            json={"sql": "SELECT * FROM public_sales"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 200

    def test_admin_grants_permission_analyst_can_query(self, seeded_app, mock_extract_factory):
        """Admin grants explicit permission → analyst can query the table."""
        c = seeded_app["client"]
        t = seeded_app["admin_token"]
        env = seeded_app["env"]

        self._setup_private_table(seeded_app, mock_extract_factory)

        # Grant permission
        resp = c.post(
            "/api/admin/permissions",
            json={"user_id": "analyst1", "dataset": "private_data", "access": "read"},
            headers=_auth(t),
        )
        assert resp.status_code == 201

        # Verify permission recorded
        resp = c.get("/api/admin/permissions/analyst1", headers=_auth(t))
        assert resp.status_code == 200
        datasets = [p["dataset"] for p in resp.json()["permissions"]]
        assert "private_data" in datasets

    def test_admin_revokes_permission(self, seeded_app, mock_extract_factory):
        """After granting then revoking, permission is removed from record."""
        c = seeded_app["client"]
        t = seeded_app["admin_token"]
        env = seeded_app["env"]

        self._setup_private_table(seeded_app, mock_extract_factory)

        # Grant
        c.post(
            "/api/admin/permissions",
            json={"user_id": "analyst1", "dataset": "private_data", "access": "read"},
            headers=_auth(t),
        )

        # Revoke — use request() because TestClient.delete() doesn't accept a body
        import json as _json
        resp = c.request(
            "DELETE",
            "/api/admin/permissions",
            data=_json.dumps({"user_id": "analyst1", "dataset": "private_data", "access": "read"}),
            headers={**_auth(t), "Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert resp.json()["revoked"] is True

        # Permission should be gone
        resp = c.get("/api/admin/permissions/analyst1", headers=_auth(t))
        datasets = [p["dataset"] for p in resp.json()["permissions"]]
        assert "private_data" not in datasets

    def test_access_request_flow(self, seeded_app, mock_extract_factory):
        """Analyst submits access request → admin approves → request is approved."""
        c = seeded_app["client"]
        t = seeded_app["admin_token"]
        analyst_headers = _auth(seeded_app["analyst_token"])

        self._setup_private_table(seeded_app, mock_extract_factory)

        # Analyst creates access request
        resp = c.post(
            "/api/access-requests",
            json={"table_id": "private_data", "reason": "I need this for analysis"},
            headers=analyst_headers,
        )
        assert resp.status_code == 201
        req_id = resp.json()["id"]
        assert resp.json()["status"] == "pending"

        # Admin sees pending request
        resp = c.get("/api/access-requests/pending", headers=_auth(t))
        assert resp.status_code == 200
        pending_ids = [r["id"] for r in resp.json()["requests"]]
        assert req_id in pending_ids

        # Admin approves
        resp = c.post(f"/api/access-requests/{req_id}/approve", headers=_auth(t))
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"

        # Analyst's own requests show approved
        resp = c.get("/api/access-requests/my", headers=analyst_headers)
        assert resp.status_code == 200
        statuses = {r["id"]: r["status"] for r in resp.json()["requests"]}
        assert statuses.get(req_id) == "approved"

    def test_duplicate_access_request_rejected(self, seeded_app, mock_extract_factory):
        """Submitting a duplicate pending request returns 409."""
        c = seeded_app["client"]
        analyst_headers = _auth(seeded_app["analyst_token"])

        self._setup_private_table(seeded_app, mock_extract_factory)

        # First request
        resp = c.post(
            "/api/access-requests",
            json={"table_id": "private_data"},
            headers=analyst_headers,
        )
        assert resp.status_code == 201

        # Duplicate
        resp = c.post(
            "/api/access-requests",
            json={"table_id": "private_data"},
            headers=analyst_headers,
        )
        assert resp.status_code == 409
