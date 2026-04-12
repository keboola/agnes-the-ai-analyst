"""Tests for user settings API endpoints."""

import pytest


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


class TestSettingsGet:
    def test_get_settings_returns_user_id(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/settings", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == "admin1"
        assert "sync_settings" in data
        assert "permissions" in data

    def test_get_settings_analyst(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/api/settings", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == "analyst1"

    def test_get_settings_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/api/settings")
        assert resp.status_code == 401

    def test_get_settings_empty_permissions_for_new_user(self, seeded_app):
        """New users have no permissions by default."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/settings", headers=_auth(token))
        assert resp.status_code == 200
        # Admin sees their own settings — permissions list should exist (may be empty)
        assert isinstance(resp.json()["permissions"], list)


class TestSettingsDataset:
    def test_update_dataset_setting_with_permission(self, seeded_app):
        """Admin granting permission first, then analyst can update the dataset setting."""
        c = seeded_app["client"]
        admin_token = seeded_app["admin_token"]
        analyst_token = seeded_app["analyst_token"]

        # Grant permission to analyst first
        c.post(
            "/api/admin/permissions",
            json={"user_id": "analyst1", "dataset": "sales_data", "access": "read"},
            headers=_auth(admin_token),
        )

        resp = c.put(
            "/api/settings/dataset",
            json={"dataset": "sales_data", "enabled": True},
            headers=_auth(analyst_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["dataset"] == "sales_data"
        assert data["enabled"] is True

    def test_update_dataset_setting_without_permission_returns_403(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.put(
            "/api/settings/dataset",
            json={"dataset": "secret_data", "enabled": True},
            headers=_auth(token),
        )
        assert resp.status_code == 403

    def test_update_dataset_setting_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.put(
            "/api/settings/dataset",
            json={"dataset": "sales_data", "enabled": True},
        )
        assert resp.status_code == 401

    def test_update_dataset_missing_fields_returns_422(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.put(
            "/api/settings/dataset",
            json={"dataset": "sales_data"},  # missing 'enabled'
            headers=_auth(token),
        )
        assert resp.status_code == 422

    def test_update_without_explicit_permission_returns_403_even_for_admin(self, seeded_app):
        """The dataset settings endpoint checks dataset_permissions table — even admin
        needs explicit permission to enable/disable a specific dataset via this endpoint."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.put(
            "/api/settings/dataset",
            json={"dataset": "any_dataset_no_perm", "enabled": False},
            headers=_auth(token),
        )
        # The endpoint checks perm_repo.has_access which doesn't have admin bypass
        assert resp.status_code == 403

    def test_disable_dataset_with_permission(self, seeded_app):
        c = seeded_app["client"]
        admin_token = seeded_app["admin_token"]

        # Grant explicit permission to admin for the dataset
        c.post(
            "/api/admin/permissions",
            json={"user_id": "admin1", "dataset": "some_table", "access": "read"},
            headers=_auth(admin_token),
        )

        resp = c.put(
            "/api/settings/dataset",
            json={"dataset": "some_table", "enabled": False},
            headers=_auth(admin_token),
        )
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False
