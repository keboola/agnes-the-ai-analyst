# tests/test_v2_scan.py
import asyncio
import importlib
from unittest.mock import MagicMock, patch
import pyarrow as pa
import pytest
from fastapi import HTTPException

from app.api.v2_arrow import parse_ipc_bytes


@pytest.fixture
def reload_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.db as db_module
    importlib.reload(db_module)
    yield db_module


def _seed(conn):
    _ensure_admin1(conn)
    from src.repositories.table_registry import TableRegistryRepository
    TableRegistryRepository(conn).register(
        id="bq_view", name="bq_view", source_type="bigquery",
        bucket="ds", source_table="bq_view", query_mode="remote",
    )


def _ensure_admin1(conn):
    """Seed an admin user with id='admin1' + Admin group membership so
    {"id": "admin1", ...} dicts pass the can_access admin shortcut."""
    from src.db import SYSTEM_ADMIN_GROUP
    from src.repositories.users import UserRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    if UserRepository(conn).get_by_id('admin1') is None:
        UserRepository(conn).create(id='admin1', email='admin1@test.com', name='Admin')
    admin_gid = conn.execute(
        'SELECT id FROM user_groups WHERE name = ?', [SYSTEM_ADMIN_GROUP]
    ).fetchone()
    if admin_gid:
        UserGroupMembersRepository(conn).add_member(
            'admin1', admin_gid[0], source='system_seed',
        )


def _bq(billing="billing-proj", data="data-proj"):
    """Build a BqAccess wired to default factories. For tests that monkeypatch
    `_run_bq_scan` whole, the inner factories are never called."""
    from connectors.bigquery.access import BqAccess, BqProjects
    return BqAccess(BqProjects(billing=billing, data=data))


class TestScan:
    def test_returns_arrow_ipc_for_simple_request(self, reload_db, monkeypatch):
        from app.api import v2_scan
        monkeypatch.setattr(
            v2_scan, "_resolve_schema",
            lambda *a, **kw: {"event_date": "DATE", "country_code": "STRING"},
        )
        fake_table = pa.table(
            {"event_date": ["2026-04-27"], "country_code": ["CZ"]}
        )
        monkeypatch.setattr(
            v2_scan, "_run_bq_scan",
            lambda bq, sql: fake_table,
        )
        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            req = {
                "table_id": "bq_view",
                "select": ["event_date", "country_code"],
                "where": "event_date > DATE '2026-01-01'",
                "limit": 100,
            }
            tracker = v2_scan._build_quota_tracker()
            ipc_bytes = v2_scan.run_scan(conn, user, req, bq=_bq(data="proj"), quota=tracker)
        finally:
            conn.close()
        got = parse_ipc_bytes(ipc_bytes)
        assert got.num_rows == 1
        assert got.column_names == ["event_date", "country_code"]

    def test_quota_concurrent_exceeded_raises_429(self, reload_db, monkeypatch):
        from app.api import v2_scan
        from app.api.v2_quota import QuotaTracker, QuotaExceededError, KIND_CONCURRENT
        monkeypatch.setattr(
            v2_scan, "_resolve_schema",
            lambda *a, **kw: {"event_date": "DATE"},
        )
        fake_table = pa.table({"event_date": ["2026-04-27"]})
        monkeypatch.setattr(v2_scan, "_run_bq_scan", lambda bq, sql: fake_table)

        tracker = QuotaTracker(max_concurrent_per_user=1, max_daily_bytes_per_user=10**12)
        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            req = {"table_id": "bq_view", "select": ["event_date"], "limit": 1}

            # Hold one concurrent slot
            with tracker.acquire(user="a@x.com"):
                with pytest.raises(QuotaExceededError) as e:
                    v2_scan.run_scan(conn, user, req, bq=_bq(data="proj"), quota=tracker)
                assert e.value.kind == KIND_CONCURRENT
        finally:
            conn.close()

    def test_validator_rejection_propagates(self, reload_db, monkeypatch):
        from app.api import v2_scan
        from app.api.where_validator import WhereValidationError, REJECT_UNKNOWN_FUNCTION
        monkeypatch.setattr(
            v2_scan, "_resolve_schema",
            lambda *a, **kw: {"event_date": "DATE"},
        )

        tracker = v2_scan._build_quota_tracker()
        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            req = {
                "table_id": "bq_view",
                "where": "event_date = NUKE_FN()",
            }
            with pytest.raises(WhereValidationError) as e:
                v2_scan.run_scan(conn, user, req, bq=_bq(data="proj"), quota=tracker)
            assert e.value.kind == REJECT_UNKNOWN_FUNCTION
        finally:
            conn.close()


class TestOrderByValidation:
    """Regression: order_by was concatenated raw into FROM clause SQL — exploitable."""

    def test_unknown_column_rejected(self, reload_db, monkeypatch):
        from app.api import v2_scan
        monkeypatch.setattr(
            v2_scan, "_resolve_schema",
            lambda *a, **kw: {"event_date": "DATE"},
        )
        tracker = v2_scan._build_quota_tracker()
        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            req = {"table_id": "bq_view", "select": ["event_date"], "order_by": ["bogus_col"], "limit": 1}
            with pytest.raises(ValueError, match="unknown order_by"):
                v2_scan.run_scan(conn, user, req, bq=_bq(data="proj"), quota=tracker)
        finally:
            conn.close()

    def test_subquery_injection_rejected(self, reload_db, monkeypatch):
        from app.api import v2_scan
        monkeypatch.setattr(
            v2_scan, "_resolve_schema",
            lambda *a, **kw: {"event_date": "DATE"},
        )
        tracker = v2_scan._build_quota_tracker()
        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            req = {
                "table_id": "bq_view",
                "select": ["event_date"],
                "order_by": ["(SELECT secret FROM read_csv('/etc/passwd') LIMIT 1)"],
                "limit": 1,
            }
            with pytest.raises(ValueError, match="invalid order_by"):
                v2_scan.run_scan(conn, user, req, bq=_bq(data="proj"), quota=tracker)
        finally:
            conn.close()

    def test_backtick_in_column_name_rejected(self, reload_db, monkeypatch):
        """Defense in depth: even though BQ INFORMATION_SCHEMA never returns
        backticks in column names, an analyst-supplied select entry containing
        one must be rejected at the validator. Otherwise it would break out
        of the `…` quoted identifier in _build_bq_sql."""
        from app.api import v2_scan
        monkeypatch.setattr(
            v2_scan, "_resolve_schema",
            lambda *a, **kw: {"event_date": "DATE"},
        )
        tracker = v2_scan._build_quota_tracker()
        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            req = {
                "table_id": "bq_view",
                "select": ["event_date`+ INJECTED --"],
                "limit": 1,
            }
            with pytest.raises(ValueError, match="invalid column name"):
                v2_scan.run_scan(conn, user, req, bq=_bq(data="proj"), quota=tracker)
        finally:
            conn.close()

    def test_double_quote_in_column_name_rejected(self, reload_db, monkeypatch):
        """Same defense for the local DuckDB path which uses `\"…\"` quoting."""
        from app.api import v2_scan
        monkeypatch.setattr(
            v2_scan, "_resolve_schema",
            lambda *a, **kw: {"id": "INTEGER"},
        )
        tracker = v2_scan._build_quota_tracker()
        conn = reload_db.get_system_db()
        try:
            _ensure_admin1(conn)
            from src.repositories.table_registry import TableRegistryRepository
            TableRegistryRepository(conn).register(
                id="local_t", name="local_t", source_type="keboola",
                bucket="b", source_table="local_t", query_mode="local",
            )
            user = {"id": "admin1", "email": "a@x.com"}
            req = {
                "table_id": "local_t",
                "select": ['id"; DROP TABLE x; --'],
                "limit": 1,
            }
            with pytest.raises(ValueError, match="invalid column name"):
                v2_scan.run_scan(conn, user, req, bq=_bq(data=""), quota=tracker)
        finally:
            conn.close()

    def test_reserved_word_columns_get_quoted_in_bq_sql(self):
        """Regression: a column literally named `order` (a SQL reserved word)
        must be backtick-quoted in BQ SQL, otherwise the generated query
        would be `SELECT order FROM ...` which doesn't parse."""
        from app.api.v2_scan import _build_bq_sql, ScanRequest
        sql = _build_bq_sql(
            {"bucket": "ds", "source_table": "t"},
            "p",
            ScanRequest(table_id="t", select=["order", "group"], order_by=["order DESC"], limit=10),
        )
        assert "`order`" in sql
        assert "`group`" in sql
        assert "SELECT order " not in sql.lower().replace("`", "")  # not unquoted

    def test_known_column_with_direction_accepted(self, reload_db, monkeypatch):
        from app.api import v2_scan
        monkeypatch.setattr(
            v2_scan, "_resolve_schema",
            lambda *a, **kw: {"event_date": "DATE"},
        )
        fake_table = pa.table({"event_date": ["2026-04-27"]})
        monkeypatch.setattr(v2_scan, "_run_bq_scan", lambda bq, sql: fake_table)
        tracker = v2_scan._build_quota_tracker()
        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            req = {"table_id": "bq_view", "select": ["event_date"], "order_by": ["event_date DESC"], "limit": 1}
            # No exception
            v2_scan.run_scan(conn, user, req, bq=_bq(data="proj"), quota=tracker)
        finally:
            conn.close()


class TestBqAccessErrors:
    """Issue #134: structured 502/400 translation on BQ errors in scan path.

    These tests exercise the REAL translation path through `BqAccess` +
    `translate_bq_error` by injecting a duckdb_session whose execute() raises
    the Google API exception. That's the production path — Phase 1
    monkeypatches of `_run_bq_scan` whole would skip the translation logic
    and only test the outer wrap (which has been removed in Phase 2)."""

    def test_scan_returns_502_on_bq_forbidden_serviceusage(self, reload_db, bq_access):
        """When _run_bq_scan raises Forbidden mentioning serviceusage, the
        endpoint must translate to HTTP 502 with `cross_project_forbidden`
        and a hint that mentions billing_project."""
        from app.api import v2_scan
        from google.api_core.exceptions import Forbidden

        # Mock duckdb conn whose execute() raises Forbidden — exercises the
        # translation path in _run_bq_scan.
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = Forbidden(
            "Permission denied: serviceusage.services.use on project foo"
        )
        bq = bq_access(duckdb_conn=mock_conn, billing="billing-proj", data="data-proj")

        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            req = {
                "table_id": "bq_view",
                "select": ["event_date", "country_code"],
                "where": "event_date > DATE '2026-01-01'",
                "limit": 1000,
            }
            with patch.object(
                v2_scan, "_resolve_schema",
                lambda *a, **kw: {"event_date": "DATE", "country_code": "STRING"},
            ):
                with pytest.raises(HTTPException) as exc_info:
                    asyncio.run(
                        v2_scan.scan_endpoint(raw=req, user=user, conn=conn, bq=bq)
                    )
        finally:
            conn.close()

        assert exc_info.value.status_code == 502
        detail = exc_info.value.detail
        assert isinstance(detail, dict)
        assert detail["error"] == "cross_project_forbidden"
        assert "hint" in detail["details"]
        assert "billing_project" in detail["details"]["hint"].lower()

    def test_scan_returns_502_on_bq_forbidden_non_serviceusage(self, reload_db, bq_access):
        """A Forbidden that is NOT about serviceusage (e.g. dataset-level ACL)
        still becomes a 502, but with `bq_forbidden`."""
        from app.api import v2_scan
        from google.api_core.exceptions import Forbidden

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = Forbidden(
            "Access Denied: Table foo.bar.baz: User does not have permission"
        )
        bq = bq_access(duckdb_conn=mock_conn, billing="billing-proj", data="data-proj")

        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            req = {
                "table_id": "bq_view",
                "select": ["event_date"],
                "limit": 1,
            }
            with patch.object(
                v2_scan, "_resolve_schema",
                lambda *a, **kw: {"event_date": "DATE", "country_code": "STRING"},
            ):
                with pytest.raises(HTTPException) as exc_info:
                    asyncio.run(
                        v2_scan.scan_endpoint(raw=req, user=user, conn=conn, bq=bq)
                    )
        finally:
            conn.close()

        assert exc_info.value.status_code == 502
        assert exc_info.value.detail["error"] == "bq_forbidden"

    def test_scan_returns_400_on_bq_bad_request(self, reload_db, bq_access):
        """`/scan` SQL is user-derived (built from req.select/where/order_by),
        so a BQ BadRequest must surface as HTTP 400 with `bq_bad_request`."""
        from app.api import v2_scan
        from google.api_core.exceptions import BadRequest

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = BadRequest(
            "Syntax error: unexpected token at line 1, column 5"
        )
        bq = bq_access(duckdb_conn=mock_conn, billing="billing-proj", data="data-proj")

        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            req = {
                "table_id": "bq_view",
                "select": ["event_date"],
                "limit": 1,
            }
            with patch.object(
                v2_scan, "_resolve_schema",
                lambda *a, **kw: {"event_date": "DATE", "country_code": "STRING"},
            ):
                with pytest.raises(HTTPException) as exc_info:
                    asyncio.run(
                        v2_scan.scan_endpoint(raw=req, user=user, conn=conn, bq=bq)
                    )
        finally:
            conn.close()

        assert exc_info.value.status_code == 400
        detail = exc_info.value.detail
        assert isinstance(detail, dict)
        assert detail["error"] == "bq_bad_request"
        assert "Syntax error" in detail["message"]


def test_resolve_schema_passes_bq_kwarg_to_build_schema(monkeypatch, bq_access):
    """Regression guard: _resolve_schema must call build_schema with the new bq= kwarg.

    Previously called with project_id= kwarg from the pre-Phase-2 signature, which
    throws TypeError after build_schema was migrated to take bq= in #134 Task 2.9.
    Caught in final code review. Every other test in this file monkeypatches
    _resolve_schema wholesale, so the bug slipped past the suite.

    This test does NOT monkeypatch _resolve_schema — it stubs build_schema one
    layer up to capture the kwargs _resolve_schema actually passes.
    """
    from app.api import v2_scan

    captured_kwargs: dict = {}

    def fake_build_schema(conn, user, table_id, **kwargs):
        captured_kwargs.update(kwargs)
        return {"columns": []}

    monkeypatch.setattr("app.api.v2_scan.build_schema", fake_build_schema)
    bq = bq_access()  # default test billing/data

    v2_scan._resolve_schema(conn=None, user=None, table_id="t", bq=bq)

    assert "bq" in captured_kwargs, (
        "build_schema must be called with bq= kwarg, not project_id="
    )
    assert "project_id" not in captured_kwargs, (
        "build_schema no longer accepts project_id= — that's the bug this guards"
    )
    assert captured_kwargs["bq"] is bq
