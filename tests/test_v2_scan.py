# tests/test_v2_scan.py
import importlib
from unittest.mock import MagicMock
import pyarrow as pa
import pytest

from app.api.v2_arrow import parse_ipc_bytes


@pytest.fixture
def reload_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.db as db_module
    importlib.reload(db_module)
    yield db_module


def _seed(conn):
    from src.repositories.table_registry import TableRegistryRepository
    TableRegistryRepository(conn).register(
        id="bq_view", name="bq_view", source_type="bigquery",
        bucket="ds", source_table="bq_view", query_mode="remote",
        is_public=True,
    )


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
            lambda *a, **kw: fake_table,
        )
        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"role": "admin", "email": "a@x.com"}
            req = {
                "table_id": "bq_view",
                "select": ["event_date", "country_code"],
                "where": "event_date > DATE '2026-01-01'",
                "limit": 100,
            }
            tracker = v2_scan._build_quota_tracker()
            ipc_bytes = v2_scan.run_scan(conn, user, req, project_id="proj", quota=tracker)
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
        monkeypatch.setattr(v2_scan, "_run_bq_scan", lambda *a, **kw: fake_table)

        tracker = QuotaTracker(max_concurrent_per_user=1, max_daily_bytes_per_user=10**12)
        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"role": "admin", "email": "a@x.com"}
            req = {"table_id": "bq_view", "select": ["event_date"], "limit": 1}

            # Hold one concurrent slot
            with tracker.acquire(user="a@x.com"):
                with pytest.raises(QuotaExceededError) as e:
                    v2_scan.run_scan(conn, user, req, project_id="proj", quota=tracker)
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
            user = {"role": "admin", "email": "a@x.com"}
            req = {
                "table_id": "bq_view",
                "where": "event_date = NUKE_FN()",
            }
            with pytest.raises(WhereValidationError) as e:
                v2_scan.run_scan(conn, user, req, project_id="proj", quota=tracker)
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
            user = {"role": "admin", "email": "a@x.com"}
            req = {"table_id": "bq_view", "select": ["event_date"], "order_by": ["bogus_col"], "limit": 1}
            with pytest.raises(ValueError, match="unknown order_by"):
                v2_scan.run_scan(conn, user, req, project_id="proj", quota=tracker)
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
            user = {"role": "admin", "email": "a@x.com"}
            req = {
                "table_id": "bq_view",
                "select": ["event_date"],
                "order_by": ["(SELECT secret FROM read_csv('/etc/passwd') LIMIT 1)"],
                "limit": 1,
            }
            with pytest.raises(ValueError, match="invalid order_by"):
                v2_scan.run_scan(conn, user, req, project_id="proj", quota=tracker)
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
            user = {"role": "admin", "email": "a@x.com"}
            req = {
                "table_id": "bq_view",
                "select": ["event_date`+ INJECTED --"],
                "limit": 1,
            }
            with pytest.raises(ValueError, match="invalid column name"):
                v2_scan.run_scan(conn, user, req, project_id="proj", quota=tracker)
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
            from src.repositories.table_registry import TableRegistryRepository
            TableRegistryRepository(conn).register(
                id="local_t", name="local_t", source_type="keboola",
                bucket="b", source_table="local_t", query_mode="local",
                is_public=True,
            )
            user = {"role": "admin", "email": "a@x.com"}
            req = {
                "table_id": "local_t",
                "select": ['id"; DROP TABLE x; --'],
                "limit": 1,
            }
            with pytest.raises(ValueError, match="invalid column name"):
                v2_scan.run_scan(conn, user, req, project_id="", quota=tracker)
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
        monkeypatch.setattr(v2_scan, "_run_bq_scan", lambda *a, **kw: fake_table)
        tracker = v2_scan._build_quota_tracker()
        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"role": "admin", "email": "a@x.com"}
            req = {"table_id": "bq_view", "select": ["event_date"], "order_by": ["event_date DESC"], "limit": 1}
            # No exception
            v2_scan.run_scan(conn, user, req, project_id="proj", quota=tracker)
        finally:
            conn.close()
