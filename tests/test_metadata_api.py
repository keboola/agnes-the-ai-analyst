"""Tests for admin metadata API — column metadata CRUD and push."""

import pytest


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _register_table(c, token, table_name="test_table"):
    """Helper to register a table in the registry."""
    resp = c.post(
        "/api/admin/register-table",
        json={"name": table_name, "source_type": "keboola", "bucket": "in.c-test",
              "source_table": table_name, "query_mode": "local"},
        headers=_auth(token),
    )
    return resp.json().get("id", table_name.lower())


class TestGetMetadata:
    def test_get_metadata_empty(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/metadata/some_table", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["table_id"] == "some_table"
        assert "columns" in data
        assert isinstance(data["columns"], list)

    def test_get_metadata_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/api/admin/metadata/some_table")
        assert resp.status_code == 401

    def test_get_metadata_analyst_allowed(self, seeded_app):
        """GET metadata is allowed for authenticated users (not admin-only)."""
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/api/admin/metadata/some_table", headers=_auth(token))
        assert resp.status_code == 200


class TestSaveMetadata:
    def test_save_column_metadata_as_admin(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        table_id = _register_table(c, token, "orders_meta")

        resp = c.post(
            f"/api/admin/metadata/{table_id}",
            json={
                "columns": [
                    {"column_name": "id", "basetype": "INTEGER", "description": "Primary key", "confidence": "manual"},
                    {"column_name": "name", "basetype": "VARCHAR", "description": "Customer name"},
                ]
            },
            headers=_auth(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["count"] == 2

    def test_save_metadata_analyst_gets_403(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.post(
            "/api/admin/metadata/some_table",
            json={"columns": [{"column_name": "id", "basetype": "INTEGER"}]},
            headers=_auth(token),
        )
        assert resp.status_code == 403

    def test_save_metadata_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/metadata/some_table",
            json={"columns": [{"column_name": "id"}]},
        )
        assert resp.status_code == 401

    def test_save_metadata_missing_columns_returns_422(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/metadata/some_table",
            json={},  # missing 'columns'
            headers=_auth(token),
        )
        assert resp.status_code == 422

    def test_save_then_get_metadata(self, seeded_app):
        """Save metadata then verify it can be retrieved."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        table_id = _register_table(c, token, "round_trip_table")

        c.post(
            f"/api/admin/metadata/{table_id}",
            json={
                "columns": [
                    {"column_name": "amount", "basetype": "DECIMAL", "description": "Order amount"},
                ]
            },
            headers=_auth(token),
        )

        resp = c.get(f"/api/admin/metadata/{table_id}", headers=_auth(token))
        assert resp.status_code == 200
        columns = resp.json()["columns"]
        assert len(columns) >= 1
        col_names = [c_["column_name"] for c_ in columns]
        assert "amount" in col_names


class TestPushMetadata:
    def test_push_metadata_table_not_found_returns_404(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post("/api/admin/metadata/nonexistent_table/push", headers=_auth(token))
        assert resp.status_code == 404

    def test_push_metadata_non_keboola_returns_400(self, seeded_app):
        """Push is only supported for keboola tables."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        # Register a bigquery table (non-keboola)
        c.post(
            "/api/admin/register-table",
            json={"name": "bq_table", "source_type": "bigquery", "query_mode": "remote"},
            headers=_auth(token),
        )

        resp = c.post("/api/admin/metadata/bq_table/push", headers=_auth(token))
        assert resp.status_code == 400
        assert "keboola" in resp.json()["detail"].lower()

    def test_push_metadata_analyst_gets_403(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.post("/api/admin/metadata/some_table/push", headers=_auth(token))
        assert resp.status_code == 403

    def test_push_metadata_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post("/api/admin/metadata/some_table/push")
        assert resp.status_code == 401

    def test_push_keboola_table_without_env_vars_returns_500(self, seeded_app):
        """Keboola table without env vars configured should return 500."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        table_id = _register_table(c, token, "kbc_push_table")

        # Should fail with 500 because KBC_STACK_URL and KBC_STORAGE_TOKEN are not set
        resp = c.post(f"/api/admin/metadata/{table_id}/push", headers=_auth(token))
        assert resp.status_code == 500
