"""Tests for RemoteQueryEngine — two-phase BQ registration + DuckDB execution."""

import sys
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import duckdb
import pyarrow as pa
import pytest

from src.remote_query import RemoteQueryEngine, RemoteQueryError, _validate_sql


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def analytics_conn():
    conn = duckdb.connect()
    conn.execute("CREATE TABLE orders (id INT, date DATE, amount DECIMAL(10,2))")
    conn.execute(
        "INSERT INTO orders VALUES (1, '2026-01-01', 100.0), (2, '2026-01-15', 200.0)"
    )
    yield conn
    conn.close()


def _make_bq_mock(arrow_table, count_value=None):
    """Build a minimal BQ client mock.

    First call to client.query() returns a count job, second returns a data job.
    If count_value is None, infer it from arrow_table.num_rows.
    """
    if count_value is None:
        count_value = arrow_table.num_rows

    count_arrow = pa.table({"count": pa.array([count_value], type=pa.int64())})

    count_job = MagicMock()
    count_job.to_arrow.return_value = count_arrow

    data_job = MagicMock()
    data_job.to_arrow.return_value = arrow_table

    mock_client = MagicMock()
    mock_client.query.side_effect = [count_job, data_job]

    return mock_client


# ---------------------------------------------------------------------------
# TestRemoteQueryEngineRegister
# ---------------------------------------------------------------------------


class TestRemoteQueryEngineRegister:
    def test_register_bq_success(self, analytics_conn):
        """Mock BQ client returning an Arrow table; verify view is queryable."""
        arrow_table = pa.table(
            {
                "order_id": pa.array([10, 20, 30], type=pa.int64()),
                "revenue": pa.array([1.0, 2.0, 3.0], type=pa.float64()),
            }
        )
        mock_client = _make_bq_mock(arrow_table)

        engine = RemoteQueryEngine(
            analytics_conn,
            _bq_client_factory=lambda project: mock_client,
            max_bq_registration_rows=500_000,
        )

        result = engine.register_bq("bq_orders", "SELECT order_id, revenue FROM bq.orders")

        assert result["alias"] == "bq_orders"
        assert result["rows"] == 3
        assert result["columns"] == ["order_id", "revenue"]
        assert result["memory_mb"] > 0

        # The alias must be queryable from DuckDB
        rows = analytics_conn.execute("SELECT COUNT(*) FROM bq_orders").fetchone()
        assert rows[0] == 3

    def test_register_bq_row_limit_exceeded(self, analytics_conn):
        """COUNT pre-check returns a value exceeding the row limit → RemoteQueryError."""
        arrow_table = pa.table({"x": pa.array([1], type=pa.int64())})
        # count exceeds limit
        mock_client = _make_bq_mock(arrow_table, count_value=1_000_000)

        engine = RemoteQueryEngine(
            analytics_conn,
            _bq_client_factory=lambda project: mock_client,
            max_bq_registration_rows=500_000,
        )

        with pytest.raises(RemoteQueryError) as exc_info:
            engine.register_bq("bq_big", "SELECT * FROM bq.huge_table")

        assert exc_info.value.error_type == "row_limit"
        assert exc_info.value.details["count"] == 1_000_000

    def test_register_bq_missing_package(self, analytics_conn):
        """When google-cloud-bigquery is not installed, engine must raise ImportError."""
        engine = RemoteQueryEngine(
            analytics_conn,
            # No factory — will try to import google.cloud.bigquery
            _bq_client_factory=None,
            max_bq_registration_rows=500_000,
        )

        with patch.dict(sys.modules, {"google": None, "google.cloud": None, "google.cloud.bigquery": None}):
            with pytest.raises((ImportError, ModuleNotFoundError)):
                engine.register_bq("bq_alias", "SELECT 1")


# ---------------------------------------------------------------------------
# TestRemoteQueryEngineExecute
# ---------------------------------------------------------------------------


class TestRemoteQueryEngineExecute:
    def test_execute_local_only(self, analytics_conn):
        """Query local table; result dict has correct structure."""
        engine = RemoteQueryEngine(analytics_conn)
        result = engine.execute("SELECT id, amount FROM orders ORDER BY id")

        assert result["columns"] == ["id", "amount"]
        assert result["row_count"] == 2
        assert result["truncated"] is False
        assert len(result["rows"]) == 2
        # Non-standard types (Decimal) must be serialized to str
        for row in result["rows"]:
            for val in row:
                assert isinstance(val, (int, float, bool, str, type(None)))

    def test_execute_with_registered_bq(self, analytics_conn):
        """Manually register an Arrow table, then JOIN it with local orders."""
        bq_arrow = pa.table(
            {
                "id": pa.array([1, 2], type=pa.int64()),
                "label": pa.array(["first", "second"], type=pa.utf8()),
            }
        )
        mock_client = _make_bq_mock(bq_arrow)

        engine = RemoteQueryEngine(
            analytics_conn,
            _bq_client_factory=lambda project: mock_client,
            max_bq_registration_rows=500_000,
        )
        engine.register_bq("bq_labels", "SELECT id, label FROM bq.labels")

        result = engine.execute(
            "SELECT o.id, o.amount, b.label "
            "FROM orders o JOIN bq_labels b ON o.id = b.id "
            "ORDER BY o.id"
        )

        assert result["row_count"] == 2
        assert "label" in result["columns"]

    def test_execute_respects_max_result_rows(self, analytics_conn):
        """When max_result_rows=1, result is truncated after 1 row."""
        engine = RemoteQueryEngine(analytics_conn, max_result_rows=1)
        result = engine.execute("SELECT id FROM orders ORDER BY id")

        assert result["row_count"] == 1
        assert result["truncated"] is True

    def test_execute_invalid_sql(self, analytics_conn):
        """DROP TABLE must be rejected with RemoteQueryError(error_type='query_error')."""
        engine = RemoteQueryEngine(analytics_conn)

        with pytest.raises(RemoteQueryError) as exc_info:
            engine.execute("DROP TABLE orders")

        assert exc_info.value.error_type == "query_error"


# ---------------------------------------------------------------------------
# _validate_sql unit tests
# ---------------------------------------------------------------------------


class TestValidateSql:
    @pytest.mark.parametrize(
        "sql",
        [
            "DROP TABLE foo",
            "DELETE FROM foo",
            "INSERT INTO foo VALUES (1)",
            "UPDATE foo SET x=1",
            "ALTER TABLE foo ADD COLUMN y INT",
            "CREATE TABLE foo (x INT)",
            "COPY foo TO '/tmp/out.csv'",
            "ATTACH '/db.duckdb'",
            "DETACH db",
            "LOAD 'extension'",
            "INSTALL httpfs",
            "SELECT read_parquet('/data/file.parquet')",
            "SELECT * FROM '../secret/file'",
            "SELECT 1; DROP TABLE foo",
        ],
    )
    def test_blocked_sql(self, sql):
        with pytest.raises(RemoteQueryError) as exc_info:
            _validate_sql(sql)
        assert exc_info.value.error_type == "query_error"

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT id FROM orders",
            "WITH cte AS (SELECT 1 AS x) SELECT x FROM cte",
            "select count(*) from orders",
            "with t as (select 1) select * from t",
        ],
    )
    def test_allowed_sql(self, sql):
        # Should not raise
        _validate_sql(sql)
