# tests/test_v2_schema.py
import asyncio
import importlib
from unittest.mock import patch, MagicMock
import pytest
from fastapi import HTTPException


@pytest.fixture
def reload_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.db as db_module
    importlib.reload(db_module)
    yield db_module


def _seed_bq_table(conn, *, is_public=True):
    from src.repositories.table_registry import TableRegistryRepository
    TableRegistryRepository(conn).register(
        id="bq_view", name="bq_view", source_type="bigquery",
        bucket="ds", source_table="bq_view", query_mode="remote",
        is_public=is_public,
    )


class TestSchemaEndpoint:
    def test_bq_table_returns_columns_and_dialect_hints(self, reload_db, monkeypatch):
        from app.api import v2_schema
        # Stub the BQ schema fetch to avoid hitting real BQ
        monkeypatch.setattr(
            v2_schema, "_fetch_bq_schema",
            lambda project, dataset, table: [
                {"name": "event_date", "type": "DATE", "nullable": False, "description": ""},
                {"name": "country_code", "type": "STRING", "nullable": True, "description": ""},
            ],
        )
        monkeypatch.setattr(v2_schema, "_fetch_bq_table_options", lambda *a: {"partition_by": "event_date", "clustered_by": []})

        conn = reload_db.get_system_db()
        try:
            _seed_bq_table(conn)
            user = {"role": "admin", "email": "a@x.com"}
            data = v2_schema.build_schema(conn, user, "bq_view", project_id="my-proj")
        finally:
            conn.close()
        assert data["table_id"] == "bq_view"
        assert data["sql_flavor"] == "bigquery"
        assert {c["name"] for c in data["columns"]} == {"event_date", "country_code"}
        assert "where_dialect_hints" in data
        assert data["partition_by"] == "event_date"

    def test_unknown_table_raises_404(self, reload_db):
        from app.api.v2_schema import build_schema, NotFound
        conn = reload_db.get_system_db()
        try:
            user = {"role": "admin", "email": "a@x.com"}
            with pytest.raises(NotFound):
                build_schema(conn, user, "missing", project_id="my-proj")
        finally:
            conn.close()

    def test_rbac_check_runs_before_cache(self, reload_db, monkeypatch):
        """Regression: cache lookup used to happen before the RBAC check, and the
        cache key had no user component — so an unauthorized user could read
        cached schema fetched by an authorized one. The fix moves RBAC ahead."""
        from app.api import v2_schema
        monkeypatch.setattr(
            v2_schema, "_fetch_bq_schema",
            lambda *a, **kw: [{"name": "x", "type": "STRING", "nullable": True, "description": ""}],
        )
        monkeypatch.setattr(v2_schema, "_fetch_bq_table_options", lambda *a: {})
        # Stub can_access_table to deny non-admins
        monkeypatch.setattr(
            "app.api.v2_schema.can_access_table",
            lambda user, tid, conn: False,
        )

        conn = reload_db.get_system_db()
        try:
            # Register the table NOT public so RBAC has to gate it.
            _seed_bq_table(conn, is_public=False)
            # Admin warms the cache.
            admin = {"role": "admin", "email": "admin@x.com"}
            v2_schema.build_schema(conn, admin, "bq_view", project_id="p")
            # Non-admin must hit RBAC denial — cache must NOT short-circuit.
            other = {"role": "viewer", "email": "viewer@x.com"}
            with pytest.raises(PermissionError):
                v2_schema.build_schema(conn, other, "bq_view", project_id="p")
        finally:
            conn.close()


class TestBqAccessErrors:
    """Issue #134: structured 502 translation on BQ errors in the strict /schema path.

    `/schema` differs from `/scan` and `/scan/estimate`: the SQL is
    server-constructed (queries `INFORMATION_SCHEMA.COLUMNS` with validated
    identifiers, no user-derived fragments), so `BadRequest` → 502
    (`bq_upstream_error`) rather than 400. Same as `/sample`.
    """

    def test_schema_returns_502_on_bq_forbidden_serviceusage(self, reload_db, monkeypatch):
        """When `_fetch_bq_schema` raises Forbidden mentioning serviceusage, the
        endpoint must translate to HTTP 502 with `cross_project_forbidden`
        and a hint that mentions billing_project."""
        from app.api import v2_schema
        from google.api_core.exceptions import Forbidden

        # Clear cache so a prior test's payload doesn't short-circuit the fetch.
        v2_schema._schema_cache.clear()

        def _raise_forbidden(project, dataset, table):
            raise Forbidden("Permission denied: serviceusage.services.use on project foo")

        monkeypatch.setattr(v2_schema, "_fetch_bq_schema", _raise_forbidden)
        # Empty project would fail identifier validation before reaching the BQ
        # call, so patch get_value to provide a non-empty project (mirrors
        # Task 1.2/1.3 in test_v2_scan_estimate.py / test_v2_scan.py).
        cfg = {
            ("data_source", "bigquery", "project"): "data-proj",
        }
        monkeypatch.setattr(
            "app.api.v2_schema.get_value",
            lambda *keys, **kw: cfg.get(keys, kw.get("default", "")),
        )

        conn = reload_db.get_system_db()
        try:
            _seed_bq_table(conn)
            user = {"role": "admin", "email": "a@x.com"}
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(v2_schema.schema(table_id="bq_view", user=user, conn=conn))
        finally:
            conn.close()

        assert exc_info.value.status_code == 502
        detail = exc_info.value.detail
        assert isinstance(detail, dict)
        assert detail["error"] == "cross_project_forbidden"
        assert "hint" in detail["details"]
        assert "billing_project" in detail["details"]["hint"].lower()

    def test_schema_returns_502_on_bq_forbidden_non_serviceusage(self, reload_db, monkeypatch):
        """A Forbidden that is NOT about serviceusage (e.g. dataset-level ACL)
        still becomes a 502, but with `bq_forbidden` and an empty hint."""
        from app.api import v2_schema
        from google.api_core.exceptions import Forbidden

        v2_schema._schema_cache.clear()

        def _raise_forbidden(project, dataset, table):
            raise Forbidden("Access Denied: Dataset foo.bar: User does not have permission")

        monkeypatch.setattr(v2_schema, "_fetch_bq_schema", _raise_forbidden)
        cfg = {
            ("data_source", "bigquery", "project"): "data-proj",
        }
        monkeypatch.setattr(
            "app.api.v2_schema.get_value",
            lambda *keys, **kw: cfg.get(keys, kw.get("default", "")),
        )

        conn = reload_db.get_system_db()
        try:
            _seed_bq_table(conn)
            user = {"role": "admin", "email": "a@x.com"}
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(v2_schema.schema(table_id="bq_view", user=user, conn=conn))
        finally:
            conn.close()

        assert exc_info.value.status_code == 502
        assert exc_info.value.detail["error"] == "bq_forbidden"

    def test_schema_returns_502_on_bq_bad_request(self, reload_db, monkeypatch):
        """`/schema` SQL is server-constructed (INFORMATION_SCHEMA.COLUMNS with
        validated identifiers); a BadRequest here means registry corruption →
        upstream error, not user fault. Translate to HTTP 502 (`bq_upstream_error`),
        same as `/sample`, opposite of `/scan*`."""
        from app.api import v2_schema
        from google.api_core.exceptions import BadRequest

        v2_schema._schema_cache.clear()

        def _raise_bad_request(project, dataset, table):
            raise BadRequest("Syntax error at line 1, column 5")

        monkeypatch.setattr(v2_schema, "_fetch_bq_schema", _raise_bad_request)
        cfg = {
            ("data_source", "bigquery", "project"): "data-proj",
        }
        monkeypatch.setattr(
            "app.api.v2_schema.get_value",
            lambda *keys, **kw: cfg.get(keys, kw.get("default", "")),
        )

        conn = reload_db.get_system_db()
        try:
            _seed_bq_table(conn)
            user = {"role": "admin", "email": "a@x.com"}
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(v2_schema.schema(table_id="bq_view", user=user, conn=conn))
        finally:
            conn.close()

        assert exc_info.value.status_code == 502
        detail = exc_info.value.detail
        assert isinstance(detail, dict)
        assert detail["error"] == "bq_upstream_error"
        assert "Syntax error" in detail["message"]

    def test_schema_returns_200_with_empty_partition_on_table_options_failure(self, reload_db, monkeypatch):
        """REGRESSION GUARD: `_fetch_bq_table_options` is best-effort (already
        wrapped in `try/except Exception → return {}` with a logger.warning).
        On internal failure the helper returns `{}`; the endpoint MUST still
        return 200 with the column list and `partition_by=None, clustered_by=[]`.

        Phase 2 will refactor the helper to use BqAccess; it must preserve the
        swallow-all contract. This test patches the helper to return the empty
        dict it would yield on failure, and asserts the endpoint surfaces a
        clean 200 — guards the swallow-all output contract end-to-end."""
        from app.api import v2_schema

        # Module-level schema cache survives across tests; clear it so an
        # earlier test's (partition_by, clustered_by) doesn't poison this one.
        v2_schema._schema_cache.clear()

        # Strict path returns real schema.
        monkeypatch.setattr(
            v2_schema, "_fetch_bq_schema",
            lambda project, dataset, table: [
                {"name": "event_date", "type": "DATE", "nullable": False, "description": ""},
                {"name": "country_code", "type": "STRING", "nullable": True, "description": ""},
            ],
        )
        # Best-effort path returns `{}` (the documented swallow-all output).
        # If a Phase 2 refactor accidentally lets HTTPException leak instead,
        # this test stays the same and other coverage flags the regression.
        monkeypatch.setattr(v2_schema, "_fetch_bq_table_options", lambda *a: {})

        cfg = {
            ("data_source", "bigquery", "project"): "data-proj",
        }
        monkeypatch.setattr(
            "app.api.v2_schema.get_value",
            lambda *keys, **kw: cfg.get(keys, kw.get("default", "")),
        )

        conn = reload_db.get_system_db()
        try:
            _seed_bq_table(conn)
            user = {"role": "admin", "email": "a@x.com"}
            data = asyncio.run(v2_schema.schema(table_id="bq_view", user=user, conn=conn))
        finally:
            conn.close()

        # Endpoint returns 200 (no HTTPException raised) with columns present.
        assert data["table_id"] == "bq_view"
        assert {c["name"] for c in data["columns"]} == {"event_date", "country_code"}
        # Partition info absent or None (the swallow-all returns {} on failure).
        assert data.get("partition_by") is None
        assert data.get("clustered_by") == []
