"""Tests for admin permissions API — grant, revoke, list."""

import pytest


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


class TestGrantPermission:
    def test_grant_permission_as_admin(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/permissions",
            json={"user_id": "analyst1", "dataset": "sales", "access": "read"},
            headers=_auth(token),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["user_id"] == "analyst1"
        assert data["dataset"] == "sales"
        assert data["access"] == "read"

    def test_grant_analyst_gets_403(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.post(
            "/api/admin/permissions",
            json={"user_id": "analyst1", "dataset": "sales", "access": "read"},
            headers=_auth(token),
        )
        assert resp.status_code == 403

    def test_grant_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/permissions",
            json={"user_id": "analyst1", "dataset": "sales", "access": "read"},
        )
        assert resp.status_code == 401

    def test_grant_missing_fields_returns_422(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/permissions",
            json={"user_id": "analyst1"},  # missing 'dataset'
            headers=_auth(token),
        )
        assert resp.status_code == 422


class TestRevokePermission:
    def _delete_with_json(self, c, url, body, headers=None):
        """TestClient.delete() does not support a body — use request() method."""
        import json
        h = {"Content-Type": "application/json"}
        if headers:
            h.update(headers)
        return c.request("DELETE", url, content=json.dumps(body), headers=h)

    def test_revoke_permission_as_admin(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        # Grant first
        c.post(
            "/api/admin/permissions",
            json={"user_id": "analyst1", "dataset": "to_revoke", "access": "read"},
            headers=_auth(token),
        )

        # Then revoke
        resp = self._delete_with_json(
            c, "/api/admin/permissions",
            {"user_id": "analyst1", "dataset": "to_revoke"},
            headers=_auth(token),
        )
        assert resp.status_code == 200
        assert resp.json()["revoked"] is True

    def test_revoke_analyst_gets_403(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = self._delete_with_json(
            c, "/api/admin/permissions",
            {"user_id": "analyst1", "dataset": "sales"},
            headers=_auth(token),
        )
        assert resp.status_code == 403

    def test_revoke_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = self._delete_with_json(
            c, "/api/admin/permissions",
            {"user_id": "analyst1", "dataset": "sales"},
        )
        assert resp.status_code == 401


class TestListUserPermissions:
    def test_list_user_permissions_as_admin(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        # Grant a permission
        c.post(
            "/api/admin/permissions",
            json={"user_id": "analyst1", "dataset": "finance", "access": "read"},
            headers=_auth(token),
        )

        resp = c.get("/api/admin/permissions/analyst1", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == "analyst1"
        assert "permissions" in data
        datasets = [p["dataset"] for p in data["permissions"]]
        assert "finance" in datasets

    def test_list_user_permissions_analyst_gets_403(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/api/admin/permissions/analyst1", headers=_auth(token))
        assert resp.status_code == 403

    def test_list_user_permissions_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/api/admin/permissions/analyst1")
        assert resp.status_code == 401


class TestListAllPermissions:
    def test_list_all_permissions_empty(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/permissions", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert "permissions" in data
        assert isinstance(data["permissions"], list)

    def test_list_all_permissions_after_grant(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        c.post(
            "/api/admin/permissions",
            json={"user_id": "analyst1", "dataset": "all_perms_test", "access": "read"},
            headers=_auth(token),
        )

        resp = c.get("/api/admin/permissions", headers=_auth(token))
        assert resp.status_code == 200
        datasets = [p["dataset"] for p in resp.json()["permissions"]]
        assert "all_perms_test" in datasets

    def test_list_all_permissions_analyst_gets_403(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/api/admin/permissions", headers=_auth(token))
        assert resp.status_code == 403

    def test_list_all_permissions_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/api/admin/permissions")
        assert resp.status_code == 401
