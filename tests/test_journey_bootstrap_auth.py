"""J1 — Bootstrap & Auth journey tests.

Verifies that authentication works end-to-end: JWT access, redirect for
unauthenticated requests, role enforcement, and the no-auth health endpoint.
"""

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.journey
class TestBootstrapAuth:
    def test_dashboard_accessible_with_admin_jwt(self, seeded_app):
        """Admin with valid JWT can reach the dashboard (200 or 302 redirect)."""
        c = seeded_app["client"]
        t = seeded_app["admin_token"]

        resp = c.get("/dashboard", headers=_auth(t), follow_redirects=False)
        assert resp.status_code in (200, 302)

    def test_dashboard_blocked_without_auth(self, seeded_app):
        """Unauthenticated request to dashboard is rejected (401 or 302 to login)."""
        c = seeded_app["client"]

        resp = c.get("/dashboard", follow_redirects=False)
        # Either 401 from API middleware or redirect to /login
        assert resp.status_code in (401, 302)

    def test_admin_can_list_registry(self, seeded_app, admin_user):
        """Admin JWT gives access to /api/admin/registry."""
        c = seeded_app["client"]

        # First register a table so there's something to list
        c.post(
            "/api/admin/register-table",
            json={"name": "journey_table", "source_type": "keboola", "query_mode": "local"},
            headers=admin_user,
        )

        resp = c.get("/api/admin/registry", headers=admin_user)
        assert resp.status_code == 200
        data = resp.json()
        assert "tables" in data
        names = {t["name"] for t in data["tables"]}
        assert "journey_table" in names

    def test_analyst_cannot_access_admin_endpoints(self, seeded_app):
        """Analyst JWT is forbidden from admin-only endpoints."""
        c = seeded_app["client"]
        analyst_headers = _auth(seeded_app["analyst_token"])

        resp = c.get("/api/admin/registry", headers=analyst_headers)
        assert resp.status_code == 403

        resp = c.post(
            "/api/admin/register-table",
            json={"name": "hack_table"},
            headers=analyst_headers,
        )
        assert resp.status_code == 403

    def test_health_endpoint_requires_no_auth(self, seeded_app):
        """Health check is always accessible without any token."""
        c = seeded_app["client"]

        resp = c.get("/api/health")
        assert resp.status_code == 200
        body = resp.json()
        assert "status" in body
        assert body["status"] in ("healthy", "degraded", "unhealthy")
