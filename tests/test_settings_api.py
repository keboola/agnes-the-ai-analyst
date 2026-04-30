"""Tests for user settings API endpoints (v19+).

The legacy ``permissions`` field on GET /api/settings was removed —
table access lives in resource_grants now, queryable via
/api/me/effective-access. PUT /api/settings/dataset still gates on
access, but the gate goes through ``app.auth.access.can_access`` against
``ResourceType.TABLE`` instead of dataset_permissions.
"""

import pytest


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _grant_table(conn, user_id: str, table_id: str) -> None:
    """Mint a one-shot resource_grants(group, "table", id) row that
    covers ``user_id``. Creates a per-test group + membership + grant."""
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.resource_grants import ResourceGrantsRepository
    grp = UserGroupsRepository(conn).create(
        name=f"test-{user_id}-{table_id}", description="test", created_by="test",
    )
    UserGroupMembersRepository(conn).add_member(
        user_id, grp["id"], source="admin", added_by="test",
    )
    ResourceGrantsRepository(conn).create(
        group_id=grp["id"], resource_type="table", resource_id=table_id,
        assigned_by="test",
    )


class TestSettingsGet:
    def test_get_settings_returns_user_id(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/settings", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == "admin1"
        assert "sync_settings" in data
        # v19: legacy ``permissions`` field dropped — use /api/me/effective-access.
        assert "permissions" not in data

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


class TestSettingsDataset:
    def test_update_dataset_setting_with_grant(self, seeded_app):
        """After admin grants TABLE access, analyst can enable the dataset."""
        c = seeded_app["client"]
        analyst_token = seeded_app["analyst_token"]

        from src.db import get_system_db
        conn = get_system_db()
        try:
            _grant_table(conn, "analyst1", "sales_data")
        finally:
            conn.close()

        resp = c.put(
            "/api/settings/dataset",
            json={"dataset": "sales_data", "enabled": True},
            headers=_auth(analyst_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["dataset"] == "sales_data"
        assert data["enabled"] is True

    def test_update_dataset_setting_without_grant_returns_403(self, seeded_app):
        """No resource_grants(table) row → 403, regardless of who's asking
        (admin still passes via the can_access admin shortcut)."""
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

    def test_admin_passes_gate_via_admin_shortcut(self, seeded_app):
        """Admin is in the Admin system group → can_access shortcut → no
        explicit grant needed even though no resource_grants row exists."""
        c = seeded_app["client"]
        admin_token = seeded_app["admin_token"]
        resp = c.put(
            "/api/settings/dataset",
            json={"dataset": "any_dataset_no_grant", "enabled": False},
            headers=_auth(admin_token),
        )
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False
