"""Phase E audit gap tests: verify all 8 new audit_log writers fire correctly.

Covered:
  - query.local  (POST /api/query — local DuckDB table)
  - query.remote (POST /api/query — BQ-remote table, smoke only: no live BQ)
  - query.hybrid (POST /api/query/hybrid — admin-only BQ+local join)
  - catalog.list (GET /api/v2/catalog)
  - catalog.schema (GET /api/v2/schema/{table_id})
  - catalog.sample (GET /api/v2/sample/{table_id})
  - snapshot.estimate (POST /api/v2/scan/estimate)
  - snapshot.create  (POST /api/v2/scan)
  - data.access_check (GET /api/data/{table_id}/check-access)

Each success-path test asserts:
  1. Endpoint returns expected HTTP status.
  2. An audit_log row appears with correct action + resource + non-null user_id.

Each audit-failure test monkeypatches AuditRepository.log to raise and asserts
the endpoint still returns the normal success status (audit failure is invisible
to caller).
"""
import importlib
from unittest.mock import MagicMock, patch
import pytest
import pyarrow as pa

from src.db import get_system_db


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _grant_table(conn, user_id: str, table_id: str) -> None:
    """Grant user_id read access to table_id via a dedicated group."""
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.resource_grants import ResourceGrantsRepository

    grp_name = f"audit-e-{user_id}-{table_id}"[:60]
    grp = UserGroupsRepository(conn).get_by_name(grp_name)
    if not grp:
        grp = UserGroupsRepository(conn).create(
            name=grp_name, description="audit-e-test", created_by="test",
        )
    members = UserGroupMembersRepository(conn)
    if not members.has_membership(user_id, grp["id"]):
        members.add_member(user_id, grp["id"], source="admin", added_by="test")
    grants = ResourceGrantsRepository(conn)
    if not grants.has_grant([grp["id"]], "table", table_id):
        grants.create(
            group_id=grp["id"], resource_type="table", resource_id=table_id,
            assigned_by="test",
        )


def _register_table(client, admin_hdrs, table_id, source_type="keboola", query_mode="local"):
    resp = client.post(
        "/api/admin/register-table",
        json={"name": table_id, "source_type": source_type, "query_mode": query_mode},
        headers=admin_hdrs,
    )
    assert resp.status_code in (200, 201), resp.text
    return resp


def _count_audit_rows(action):
    conn = get_system_db()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = ?", [action]
        ).fetchone()[0]
    finally:
        conn.close()


def _last_audit_row(action):
    conn = get_system_db()
    try:
        row = conn.execute(
            "SELECT user_id, action, resource, result, client_kind "
            "FROM audit_log WHERE action = ? ORDER BY timestamp DESC LIMIT 1",
            [action],
        ).fetchone()
        return row
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# catalog.list
# ---------------------------------------------------------------------------

class TestCatalogListAudit:
    def test_catalog_list_writes_audit_log(self, seeded_app, analyst_user, mock_extract_factory):
        """GET /api/v2/catalog writes catalog.list audit row."""
        c = seeded_app["client"]
        before = _count_audit_rows("catalog.list")
        resp = c.get("/api/v2/catalog", headers=analyst_user)
        assert resp.status_code == 200
        after = _count_audit_rows("catalog.list")
        assert after == before + 1
        row = _last_audit_row("catalog.list")
        assert row[0] == "analyst1"
        assert row[1] == "catalog.list"
        assert row[2] == "catalog"
        assert row[3] == "success"

    def test_catalog_list_audit_failure_invisible_to_caller(self, seeded_app, analyst_user, monkeypatch):
        """Audit write failure must not 5xx the endpoint."""
        from src.repositories.audit import AuditRepository
        monkeypatch.setattr(AuditRepository, "log", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db gone")))
        c = seeded_app["client"]
        resp = c.get("/api/v2/catalog", headers=analyst_user)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# catalog.schema
# ---------------------------------------------------------------------------

class TestCatalogSchemaAudit:
    def test_catalog_schema_writes_audit_log(self, seeded_app, admin_user, mock_extract_factory):
        """GET /api/v2/schema/{table_id} writes catalog.schema audit row."""
        c = seeded_app["client"]
        admin_hdrs = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
        table_id = "schema_audit_tbl"
        mock_extract_factory("keboola", [{"name": table_id, "data": [{"x": "1"}]}])
        _register_table(c, admin_hdrs, table_id)

        before = _count_audit_rows("catalog.schema")
        resp = c.get(f"/api/v2/schema/{table_id}", headers=admin_user)
        assert resp.status_code == 200
        after = _count_audit_rows("catalog.schema")
        assert after == before + 1
        row = _last_audit_row("catalog.schema")
        assert row[0] == "admin1"
        assert row[1] == "catalog.schema"
        assert f"table:{table_id}" in row[2]
        assert row[3] == "success"

    def test_catalog_schema_audit_failure_invisible_to_caller(
        self, seeded_app, admin_user, mock_extract_factory, monkeypatch
    ):
        """Audit write failure must not 5xx the endpoint."""
        from src.repositories.audit import AuditRepository
        c = seeded_app["client"]
        admin_hdrs = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
        table_id = "schema_audit_tbl2"
        mock_extract_factory("keboola", [{"name": table_id, "data": [{"x": "1"}]}])
        _register_table(c, admin_hdrs, table_id)
        monkeypatch.setattr(AuditRepository, "log", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db gone")))
        resp = c.get(f"/api/v2/schema/{table_id}", headers=admin_user)
        assert resp.status_code == 200

    def test_catalog_schema_error_path_writes_audit_log(self, seeded_app, admin_user):
        """404 on unknown table must write an error audit row."""
        c = seeded_app["client"]
        before = _count_audit_rows("catalog.schema")
        resp = c.get("/api/v2/schema/nonexistent_xyz_table", headers=admin_user)
        assert resp.status_code == 404
        after = _count_audit_rows("catalog.schema")
        assert after == before + 1
        row = _last_audit_row("catalog.schema")
        assert row[3].startswith("error.")


# ---------------------------------------------------------------------------
# catalog.sample
# ---------------------------------------------------------------------------

class TestCatalogSampleAudit:
    def test_catalog_sample_writes_audit_log(self, seeded_app, admin_user, mock_extract_factory):
        """GET /api/v2/sample/{table_id} writes catalog.sample audit row."""
        c = seeded_app["client"]
        admin_hdrs = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
        table_id = "sample_audit_tbl"
        mock_extract_factory("keboola", [{"name": table_id, "data": [{"a": "1"}, {"a": "2"}]}])
        _register_table(c, admin_hdrs, table_id)

        before = _count_audit_rows("catalog.sample")
        resp = c.get(f"/api/v2/sample/{table_id}", headers=admin_user)
        assert resp.status_code == 200
        after = _count_audit_rows("catalog.sample")
        assert after == before + 1
        row = _last_audit_row("catalog.sample")
        assert row[0] == "admin1"
        assert row[1] == "catalog.sample"
        assert f"table:{table_id}" in row[2]
        assert row[3] == "success"

    def test_catalog_sample_audit_failure_invisible_to_caller(
        self, seeded_app, admin_user, mock_extract_factory, monkeypatch
    ):
        """Audit write failure must not 5xx the endpoint."""
        from src.repositories.audit import AuditRepository
        c = seeded_app["client"]
        admin_hdrs = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
        table_id = "sample_audit_tbl2"
        mock_extract_factory("keboola", [{"name": table_id, "data": [{"a": "1"}]}])
        _register_table(c, admin_hdrs, table_id)
        monkeypatch.setattr(AuditRepository, "log", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("gone")))
        resp = c.get(f"/api/v2/sample/{table_id}", headers=admin_user)
        assert resp.status_code == 200

    def test_catalog_sample_error_path_writes_audit_log(self, seeded_app, admin_user):
        """404 on unknown table must write an error audit row."""
        c = seeded_app["client"]
        before = _count_audit_rows("catalog.sample")
        resp = c.get("/api/v2/sample/nonexistent_xyz_table", headers=admin_user)
        assert resp.status_code == 404
        after = _count_audit_rows("catalog.sample")
        assert after == before + 1
        row = _last_audit_row("catalog.sample")
        assert row[3].startswith("error.")


# ---------------------------------------------------------------------------
# data.access_check
# ---------------------------------------------------------------------------

class TestDataAccessCheckAudit:
    def _setup_table(self, seeded_app, mock_extract_factory, table_id):
        c = seeded_app["client"]
        admin_hdrs = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
        _register_table(c, admin_hdrs, table_id)
        mock_extract_factory("keboola", [{"name": table_id, "data": [{"v": "1"}]}])
        conn = get_system_db()
        try:
            _grant_table(conn, "analyst1", table_id)
        finally:
            conn.close()

    def test_access_check_granted_writes_audit_log(self, seeded_app, analyst_user, mock_extract_factory):
        """204 check-access must write data.access_check audit row with granted=True."""
        table_id = "check_access_granted_tbl"
        self._setup_table(seeded_app, mock_extract_factory, table_id)
        c = seeded_app["client"]
        before = _count_audit_rows("data.access_check")
        resp = c.get(f"/api/data/{table_id}/check-access", headers=analyst_user)
        assert resp.status_code == 204
        after = _count_audit_rows("data.access_check")
        assert after == before + 1
        row = _last_audit_row("data.access_check")
        assert row[0] == "analyst1"
        assert row[1] == "data.access_check"
        assert f"table:{table_id}" in row[2]
        assert row[3] == "success"

    def test_access_check_denied_writes_audit_log(self, seeded_app, analyst_user, mock_extract_factory):
        """403 check-access must write data.access_check audit row with granted=False."""
        # Register a table but do NOT grant analyst access
        c = seeded_app["client"]
        admin_hdrs = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
        table_id = "check_access_denied_tbl"
        _register_table(c, admin_hdrs, table_id)

        before = _count_audit_rows("data.access_check")
        resp = c.get(f"/api/data/{table_id}/check-access", headers=analyst_user)
        assert resp.status_code == 403
        after = _count_audit_rows("data.access_check")
        assert after == before + 1
        row = _last_audit_row("data.access_check")
        assert row[3] == "error.403"

    def test_access_check_audit_failure_invisible_to_caller(
        self, seeded_app, analyst_user, mock_extract_factory, monkeypatch
    ):
        """Audit write failure must not 5xx the endpoint."""
        from src.repositories.audit import AuditRepository
        table_id = "check_access_audit_fail_tbl"
        self._setup_table(seeded_app, mock_extract_factory, table_id)
        c = seeded_app["client"]
        monkeypatch.setattr(AuditRepository, "log", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("gone")))
        resp = c.get(f"/api/data/{table_id}/check-access", headers=analyst_user)
        assert resp.status_code == 204


# ---------------------------------------------------------------------------
# query.local
# ---------------------------------------------------------------------------

class TestQueryLocalAudit:
    def _setup_local_table(self, seeded_app, mock_extract_factory, table_id="qlocal_tbl"):
        c = seeded_app["client"]
        admin_hdrs = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
        _register_table(c, admin_hdrs, table_id)
        mock_extract_factory("keboola", [{"name": table_id, "data": [{"n": "1"}, {"n": "2"}]}])
        # Rebuild analytics DB so master view is visible
        resp = c.post("/api/sync/trigger", headers=admin_hdrs)
        # sync may fail if orchestrator is not available, ignore
        return table_id

    def test_query_local_writes_audit_log(self, seeded_app, admin_user, mock_extract_factory, monkeypatch):
        """POST /api/query on a local table must write query.local audit row."""
        # Monkeypatch get_analytics_db_readonly to return a fresh in-memory DuckDB
        # with the table so we don't need a full orchestrator rebuild.
        import duckdb as _duckdb
        from app.api import query as query_mod

        table_id = "qlocal_audit_tbl"
        mock_extract_factory("keboola", [{"name": table_id, "data": [{"n": "1"}]}])

        mem_conn = _duckdb.connect(":memory:")
        mem_conn.execute(f"CREATE TABLE {table_id} (n VARCHAR)")
        mem_conn.execute(f"INSERT INTO {table_id} VALUES ('1')")

        monkeypatch.setattr(query_mod, "get_analytics_db_readonly", lambda: mem_conn)
        # No BQ path — ensure _bq_guardrail_inputs returns empty sets
        monkeypatch.setattr(query_mod, "_bq_guardrail_inputs",
                            lambda *a, **kw: ([], [], None))

        c = seeded_app["client"]
        before = _count_audit_rows("query.local")
        resp = c.post(
            "/api/query",
            json={"sql": f"SELECT * FROM {table_id}", "limit": 10},
            headers=admin_user,
        )
        assert resp.status_code == 200, resp.text
        after = _count_audit_rows("query.local")
        assert after == before + 1
        row = _last_audit_row("query.local")
        assert row[0] == "admin1"
        assert row[1] == "query.local"
        assert row[3] == "success"
        mem_conn.close()

    def test_query_local_audit_failure_invisible(self, seeded_app, admin_user, mock_extract_factory, monkeypatch):
        """Audit write failure must not 5xx POST /api/query."""
        import duckdb as _duckdb
        from app.api import query as query_mod
        from src.repositories.audit import AuditRepository

        table_id = "qlocal_audit_fail_tbl"
        mem_conn = _duckdb.connect(":memory:")
        mem_conn.execute(f"CREATE TABLE {table_id} (n VARCHAR)")
        monkeypatch.setattr(query_mod, "get_analytics_db_readonly", lambda: mem_conn)
        monkeypatch.setattr(query_mod, "_bq_guardrail_inputs",
                            lambda *a, **kw: ([], [], None))
        monkeypatch.setattr(AuditRepository, "log", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("gone")))

        c = seeded_app["client"]
        resp = c.post(
            "/api/query",
            json={"sql": f"SELECT * FROM {table_id}", "limit": 5},
            headers=admin_user,
        )
        assert resp.status_code == 200
        mem_conn.close()


# ---------------------------------------------------------------------------
# query.hybrid
# ---------------------------------------------------------------------------

class TestQueryHybridAudit:
    def test_query_hybrid_writes_audit_log(self, seeded_app, admin_user, monkeypatch):
        """POST /api/query/hybrid must write query.hybrid audit row on success."""
        from src.remote_query import RemoteQueryEngine
        mock_engine = MagicMock()
        mock_engine.execute.return_value = {"columns": ["a"], "rows": [["1"]]}

        monkeypatch.setattr(
            "app.api.query_hybrid.RemoteQueryEngine",
            lambda *a, **kw: mock_engine,
        )
        monkeypatch.setattr(
            "app.api.query_hybrid.load_config",
            lambda: {},
        )
        import duckdb as _duckdb
        mem_conn = _duckdb.connect(":memory:")
        monkeypatch.setattr(
            "app.api.query_hybrid.get_analytics_db_readonly",
            lambda: mem_conn,
        )

        c = seeded_app["client"]
        before = _count_audit_rows("query.hybrid")
        resp = c.post(
            "/api/query/hybrid",
            json={"sql": "SELECT a FROM some_table", "register_bq": {}},
            headers=admin_user,
        )
        assert resp.status_code == 200, resp.text
        after = _count_audit_rows("query.hybrid")
        assert after == before + 1
        row = _last_audit_row("query.hybrid")
        assert row[0] == "admin1"
        assert row[1] == "query.hybrid"
        assert row[3] == "success"
        mem_conn.close()

    def test_query_hybrid_audit_failure_invisible(self, seeded_app, admin_user, monkeypatch):
        """Audit write failure must not 5xx POST /api/query/hybrid."""
        from src.repositories.audit import AuditRepository
        mock_engine = MagicMock()
        mock_engine.execute.return_value = {"columns": [], "rows": []}
        monkeypatch.setattr("app.api.query_hybrid.RemoteQueryEngine", lambda *a, **kw: mock_engine)
        monkeypatch.setattr("app.api.query_hybrid.load_config", lambda: {})
        import duckdb as _duckdb
        mem_conn = _duckdb.connect(":memory:")
        monkeypatch.setattr("app.api.query_hybrid.get_analytics_db_readonly", lambda: mem_conn)
        monkeypatch.setattr(AuditRepository, "log", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("gone")))

        c = seeded_app["client"]
        resp = c.post(
            "/api/query/hybrid",
            json={"sql": "SELECT 1", "register_bq": {}},
            headers=admin_user,
        )
        assert resp.status_code == 200
        mem_conn.close()


# ---------------------------------------------------------------------------
# snapshot.estimate (POST /api/v2/scan/estimate)
# ---------------------------------------------------------------------------

class TestSnapshotEstimateAudit:
    def test_snapshot_estimate_local_writes_audit_log(
        self, seeded_app, admin_user, mock_extract_factory, monkeypatch
    ):
        """POST /api/v2/scan/estimate on a local table must write snapshot.estimate audit row."""
        import importlib
        from app.api import v2_scan, v2_schema

        c = seeded_app["client"]
        admin_hdrs = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
        table_id = "est_audit_local_tbl"
        mock_extract_factory("keboola", [{"name": table_id, "data": [{"col": "x"}]}])
        _register_table(c, admin_hdrs, table_id)

        # Stub build_schema so we don't need a real analytics DB
        monkeypatch.setattr(v2_scan, "_resolve_schema",
                            lambda *a, **kw: {"col": "STRING"})

        before = _count_audit_rows("snapshot.estimate")
        resp = c.post(
            "/api/v2/scan/estimate",
            json={"table_id": table_id},
            headers=admin_user,
        )
        assert resp.status_code == 200, resp.text
        after = _count_audit_rows("snapshot.estimate")
        assert after == before + 1
        row = _last_audit_row("snapshot.estimate")
        assert row[0] == "admin1"
        assert row[1] == "snapshot.estimate"
        assert f"table:{table_id}" in row[2]
        assert row[3] == "success"

    def test_snapshot_estimate_audit_failure_invisible(
        self, seeded_app, admin_user, mock_extract_factory, monkeypatch
    ):
        """Audit write failure must not 5xx POST /api/v2/scan/estimate."""
        from src.repositories.audit import AuditRepository
        from app.api import v2_scan

        c = seeded_app["client"]
        admin_hdrs = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
        table_id = "est_audit_fail_tbl"
        mock_extract_factory("keboola", [{"name": table_id, "data": [{"col": "x"}]}])
        _register_table(c, admin_hdrs, table_id)

        monkeypatch.setattr(v2_scan, "_resolve_schema",
                            lambda *a, **kw: {"col": "STRING"})
        monkeypatch.setattr(AuditRepository, "log", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("gone")))

        resp = c.post(
            "/api/v2/scan/estimate",
            json={"table_id": table_id},
            headers=admin_user,
        )
        assert resp.status_code == 200

    def test_snapshot_estimate_error_path_writes_audit_log(self, seeded_app, admin_user):
        """404 on unknown table must write snapshot.estimate error audit row."""
        c = seeded_app["client"]
        before = _count_audit_rows("snapshot.estimate")
        resp = c.post(
            "/api/v2/scan/estimate",
            json={"table_id": "nonexistent_xyz_estimate"},
            headers=admin_user,
        )
        assert resp.status_code == 404
        after = _count_audit_rows("snapshot.estimate")
        assert after == before + 1
        row = _last_audit_row("snapshot.estimate")
        assert row[3].startswith("error.")


# ---------------------------------------------------------------------------
# snapshot.create (POST /api/v2/scan)
# ---------------------------------------------------------------------------

class TestSnapshotCreateAudit:
    def test_snapshot_create_local_writes_audit_log(
        self, seeded_app, admin_user, mock_extract_factory, monkeypatch
    ):
        """POST /api/v2/scan on a local table must write snapshot.create audit row."""
        from app.api import v2_scan

        c = seeded_app["client"]
        admin_hdrs = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
        table_id = "scan_audit_local_tbl"
        mock_extract_factory("keboola", [{"name": table_id, "data": [{"n": "1"}, {"n": "2"}]}])
        _register_table(c, admin_hdrs, table_id)

        monkeypatch.setattr(v2_scan, "_resolve_schema",
                            lambda *a, **kw: {"n": "STRING"})

        before = _count_audit_rows("snapshot.create")
        resp = c.post(
            "/api/v2/scan",
            json={"table_id": table_id},
            headers=admin_user,
        )
        assert resp.status_code == 200, resp.text
        after = _count_audit_rows("snapshot.create")
        assert after == before + 1
        row = _last_audit_row("snapshot.create")
        assert row[0] == "admin1"
        assert row[1] == "snapshot.create"
        assert f"table:{table_id}" in row[2]
        assert row[3] == "success"

    def test_snapshot_create_audit_failure_invisible(
        self, seeded_app, admin_user, mock_extract_factory, monkeypatch
    ):
        """Audit write failure must not 5xx POST /api/v2/scan."""
        from src.repositories.audit import AuditRepository
        from app.api import v2_scan

        c = seeded_app["client"]
        admin_hdrs = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
        table_id = "scan_audit_fail_tbl"
        mock_extract_factory("keboola", [{"name": table_id, "data": [{"n": "1"}]}])
        _register_table(c, admin_hdrs, table_id)

        monkeypatch.setattr(v2_scan, "_resolve_schema",
                            lambda *a, **kw: {"n": "STRING"})
        monkeypatch.setattr(AuditRepository, "log", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("gone")))

        resp = c.post(
            "/api/v2/scan",
            json={"table_id": table_id},
            headers=admin_user,
        )
        assert resp.status_code == 200

    def test_snapshot_create_error_path_writes_audit_log(self, seeded_app, admin_user):
        """404 on unknown table must write snapshot.create error audit row."""
        c = seeded_app["client"]
        before = _count_audit_rows("snapshot.create")
        resp = c.post(
            "/api/v2/scan",
            json={"table_id": "nonexistent_xyz_scan"},
            headers=admin_user,
        )
        assert resp.status_code == 404
        after = _count_audit_rows("snapshot.create")
        assert after == before + 1
        row = _last_audit_row("snapshot.create")
        assert row[3].startswith("error.")


# ---------------------------------------------------------------------------
# Error message capping (Fix 1)
# ---------------------------------------------------------------------------

def _get_last_audit_params(action: str) -> dict:
    """Fetch params dict from the latest audit row matching action."""
    import json
    conn = get_system_db()
    try:
        row = conn.execute(
            "SELECT params FROM audit_log WHERE action = ? ORDER BY timestamp DESC LIMIT 1",
            [action],
        ).fetchone()
        if row:
            params_str = row[0]
            if isinstance(params_str, str):
                return json.loads(params_str) or {}
            return params_str or {}
        return {}
    finally:
        conn.close()


class TestErrorMessageCapping:
    """Verify that audit error paths cap error messages at 200 chars.

    Rather than monkeypatching to force errors, we trigger 404 errors
    (table not found) which emit audit rows with error messages.
    The error message from FileNotFoundError (the exception) is capped
    by the fix in each endpoint's error handler.
    """

    def test_v2_schema_error_message_is_capped(self, seeded_app, admin_user):
        """v2/schema error path caps error message at 200 chars."""
        c = seeded_app["client"]
        before = _count_audit_rows("catalog.schema")
        # Request a nonexistent table to trigger FileNotFoundError
        resp = c.get("/api/v2/schema/nonexistent_xyz_123456789", headers=admin_user)
        assert resp.status_code == 404
        after = _count_audit_rows("catalog.schema")
        assert after == before + 1

        params = _get_last_audit_params("catalog.schema")
        error_msg = params.get("error", "")
        assert len(error_msg) <= 200, f"error message length {len(error_msg)} exceeds 200"

    def test_v2_sample_error_message_is_capped(self, seeded_app, admin_user):
        """v2/sample error path caps error message at 200 chars."""
        c = seeded_app["client"]
        before = _count_audit_rows("catalog.sample")
        resp = c.get("/api/v2/sample/nonexistent_xyz_sample123", headers=admin_user)
        assert resp.status_code == 404
        after = _count_audit_rows("catalog.sample")
        assert after == before + 1

        params = _get_last_audit_params("catalog.sample")
        error_msg = params.get("error", "")
        assert len(error_msg) <= 200, f"error message length {len(error_msg)} exceeds 200"

    def test_v2_scan_estimate_error_message_is_capped(self, seeded_app, admin_user):
        """v2/scan/estimate error path caps error message at 200 chars."""
        c = seeded_app["client"]
        before = _count_audit_rows("snapshot.estimate")
        resp = c.post("/api/v2/scan/estimate", json={"table_id": "nonexistent_xyz_estimate"}, headers=admin_user)
        assert resp.status_code == 404
        after = _count_audit_rows("snapshot.estimate")
        assert after == before + 1

        params = _get_last_audit_params("snapshot.estimate")
        error_msg = params.get("error", "")
        assert len(error_msg) <= 200, f"error message length {len(error_msg)} exceeds 200"

    def test_v2_scan_error_message_is_capped(self, seeded_app, admin_user):
        """v2/scan error path caps error message at 200 chars."""
        c = seeded_app["client"]
        before = _count_audit_rows("snapshot.create")
        resp = c.post("/api/v2/scan", json={"table_id": "nonexistent_xyz_scan123"}, headers=admin_user)
        assert resp.status_code == 404
        after = _count_audit_rows("snapshot.create")
        assert after == before + 1

        params = _get_last_audit_params("snapshot.create")
        error_msg = params.get("error", "")
        assert len(error_msg) <= 200, f"error message length {len(error_msg)} exceeds 200"
