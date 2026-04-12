"""Tests for access requests API — create, list, approve, deny."""

import pytest


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


class TestAccessRequestCreate:
    def test_create_request(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.post(
            "/api/access-requests",
            json={"table_id": "orders", "reason": "Need for analysis"},
            headers=_auth(token),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "id" in data
        assert data["status"] == "pending"
        assert data["table_id"] == "orders"

    def test_create_request_without_reason(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.post(
            "/api/access-requests",
            json={"table_id": "customers"},
            headers=_auth(token),
        )
        assert resp.status_code == 201

    def test_create_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            "/api/access-requests",
            json={"table_id": "orders", "reason": "Need access"},
        )
        assert resp.status_code == 401

    def test_create_missing_table_id_returns_422(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.post(
            "/api/access-requests",
            json={"reason": "No table_id"},
            headers=_auth(token),
        )
        assert resp.status_code == 422

    def test_duplicate_pending_request_returns_409(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]

        # First request
        resp1 = c.post(
            "/api/access-requests",
            json={"table_id": "invoices", "reason": "First request"},
            headers=_auth(token),
        )
        assert resp1.status_code == 201

        # Duplicate request for the same table
        resp2 = c.post(
            "/api/access-requests",
            json={"table_id": "invoices", "reason": "Duplicate"},
            headers=_auth(token),
        )
        assert resp2.status_code == 409


class TestAccessRequestMyRequests:
    def test_list_my_requests_empty(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/api/access-requests/my", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert "requests" in data
        assert isinstance(data["requests"], list)

    def test_list_my_requests_after_create(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]

        c.post(
            "/api/access-requests",
            json={"table_id": "my_table", "reason": "for analysis"},
            headers=_auth(token),
        )

        resp = c.get("/api/access-requests/my", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["requests"]) >= 1
        table_ids = [r["table_id"] for r in data["requests"]]
        assert "my_table" in table_ids

    def test_my_requests_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/api/access-requests/my")
        assert resp.status_code == 401


class TestAccessRequestPending:
    def test_list_pending_as_admin(self, seeded_app):
        c = seeded_app["client"]
        admin_token = seeded_app["admin_token"]
        analyst_token = seeded_app["analyst_token"]

        # Create a request
        c.post(
            "/api/access-requests",
            json={"table_id": "secret_table", "reason": "Need access"},
            headers=_auth(analyst_token),
        )

        resp = c.get("/api/access-requests/pending", headers=_auth(admin_token))
        assert resp.status_code == 200
        data = resp.json()
        assert "requests" in data
        assert "count" in data

    def test_list_pending_analyst_gets_403(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/api/access-requests/pending", headers=_auth(token))
        assert resp.status_code == 403

    def test_list_pending_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/api/access-requests/pending")
        assert resp.status_code == 401


class TestAccessRequestApproveAndDeny:
    def _create_request(self, c, analyst_token, table_id="test_table"):
        resp = c.post(
            "/api/access-requests",
            json={"table_id": table_id, "reason": "Test"},
            headers=_auth(analyst_token),
        )
        assert resp.status_code == 201
        return resp.json()["id"]

    def test_approve_request_as_admin(self, seeded_app):
        c = seeded_app["client"]
        admin_token = seeded_app["admin_token"]
        analyst_token = seeded_app["analyst_token"]

        req_id = self._create_request(c, analyst_token, "approve_table")

        resp = c.post(f"/api/access-requests/{req_id}/approve", headers=_auth(admin_token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "approved"
        assert data["id"] == req_id

    def test_approve_analyst_gets_403(self, seeded_app):
        c = seeded_app["client"]
        admin_token = seeded_app["admin_token"]
        analyst_token = seeded_app["analyst_token"]

        req_id = self._create_request(c, analyst_token, "approve_table2")

        resp = c.post(f"/api/access-requests/{req_id}/approve", headers=_auth(analyst_token))
        assert resp.status_code == 403

    def test_deny_request_as_admin(self, seeded_app):
        c = seeded_app["client"]
        admin_token = seeded_app["admin_token"]
        analyst_token = seeded_app["analyst_token"]

        req_id = self._create_request(c, analyst_token, "deny_table")

        resp = c.post(f"/api/access-requests/{req_id}/deny", headers=_auth(admin_token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "denied"
        assert data["id"] == req_id

    def test_deny_analyst_gets_403(self, seeded_app):
        c = seeded_app["client"]
        admin_token = seeded_app["admin_token"]
        analyst_token = seeded_app["analyst_token"]

        req_id = self._create_request(c, analyst_token, "deny_table2")

        resp = c.post(f"/api/access-requests/{req_id}/deny", headers=_auth(analyst_token))
        assert resp.status_code == 403

    def test_approve_nonexistent_request_returns_404(self, seeded_app):
        c = seeded_app["client"]
        admin_token = seeded_app["admin_token"]
        resp = c.post("/api/access-requests/nonexistent-id/approve", headers=_auth(admin_token))
        assert resp.status_code == 404

    def test_deny_nonexistent_request_returns_404(self, seeded_app):
        c = seeded_app["client"]
        admin_token = seeded_app["admin_token"]
        resp = c.post("/api/access-requests/nonexistent-id/deny", headers=_auth(admin_token))
        assert resp.status_code == 404
