"""Tests for remote_query module - hybrid local Parquet + remote BigQuery queries.

Tests cover:
- CLI argument parsing (_parse_register_bq, build_parser)
- Local view setup (_setup_local_views via create_local_views)
- BQ registration with safety checks (_validate_bq_result_size, _estimate_memory_mb, _register_bq_views)
- Output formatting (_print_table, _format_output)
- End-to-end local-only queries (no BQ mocking needed)
"""

import argparse
import csv
import json
import os
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.remote_query import (
    RemoteQueryError,
    _estimate_memory_mb,
    _format_output,
    _parse_register_bq,
    _print_table,
    _register_bq_views,
    _validate_bq_result_size,
    build_parser,
    execute_remote_query,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_local_project(tmp_path):
    """Create a minimal project with docs/data_description.md and parquet files.

    Layout:
        tmp_path/
            docs/data_description.md   (YAML with local + remote + hybrid tables)
            server/parquet/crm_data/orders.parquet
            server/parquet/crm_data/products.parquet

    Returns (project_root, data_dir) where data_dir = tmp_path / "server".
    """
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()

    data_description = """\
# Data Description

```yaml
folder_mapping:
  in.c-crm: crm_data

tables:
  - id: "in.c-crm.orders"
    name: "orders"
    description: "Order data"
    primary_key: "order_id"
    sync_strategy: "full_refresh"

  - id: "in.c-crm.products"
    name: "products"
    description: "Product catalog"
    primary_key: "product_id"
    sync_strategy: "full_refresh"

  - id: "prj-grp-dataview-prod-1ff9.supply.traffic"
    name: "traffic"
    description: "Remote BQ traffic table"
    primary_key: "id"
    query_mode: "remote"

  - id: "in.c-crm.inventory"
    name: "inventory"
    description: "Hybrid inventory"
    primary_key: "id"
    sync_strategy: "full_refresh"
    query_mode: "hybrid"
```
"""
    (docs_dir / "data_description.md").write_text(data_description)

    # Create parquet files for local tables
    crm_dir = tmp_path / "server" / "parquet" / "crm_data"
    crm_dir.mkdir(parents=True)

    orders_table = pa.table({
        "order_id": [1, 2, 3, 4, 5],
        "amount": [10.0, 20.0, 30.0, 40.0, 50.0],
        "product_id": [101, 102, 101, 103, 102],
    })
    pq.write_table(orders_table, crm_dir / "orders.parquet")

    products_table = pa.table({
        "product_id": [101, 102, 103],
        "name": ["Widget", "Gadget", "Doohickey"],
    })
    pq.write_table(products_table, crm_dir / "products.parquet")

    # Create parquet for hybrid table
    inventory_table = pa.table({
        "id": [1, 2],
        "stock": [100, 200],
    })
    pq.write_table(inventory_table, crm_dir / "inventory.parquet")

    data_dir = str(tmp_path / "server")
    return tmp_path, data_dir


@pytest.fixture
def duckdb_conn():
    """Create an in-memory DuckDB connection, closed after test."""
    conn = duckdb.connect(":memory:")
    yield conn
    conn.close()


class _DuckDBConnectionProxy:
    """Proxy around DuckDBPyConnection that silently ignores unsupported SET commands.

    DuckDB versions may not support 'statement_timeout'. This proxy catches
    CatalogException for SET commands so end-to-end tests work across versions.
    The real connection's execute method is read-only, so we wrap it.
    """

    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)

    def execute(self, sql, *args, **kwargs):
        if isinstance(sql, str) and sql.strip().upper().startswith("SET "):
            try:
                return self._conn.execute(sql, *args, **kwargs)
            except duckdb.CatalogException:
                return None
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _patched_duckdb_connect(*args, **kwargs):
    """Create a DuckDB connection wrapped in the proxy."""
    conn = duckdb.connect(*args, **kwargs)
    return _DuckDBConnectionProxy(conn)


# ---------------------------------------------------------------------------
# Tests: CLI argument parsing
# ---------------------------------------------------------------------------

class TestCLIArgParsing:
    """Test _parse_register_bq() and build_parser()."""

    def test_requires_sql(self):
        """Parser should fail when --sql is missing."""
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])

    def test_register_bq_parsing(self):
        """'alias=SELECT ...' parses into (alias, sql) tuple."""
        result = _parse_register_bq("traffic=SELECT report_date FROM `proj.ds.table`")
        assert result == ("traffic", "SELECT report_date FROM `proj.ds.table`")

    def test_register_bq_invalid_format(self):
        """Missing '=' should raise ArgumentTypeError."""
        with pytest.raises(argparse.ArgumentTypeError, match="Invalid --register-bq format"):
            _parse_register_bq("no_equals_sign_here")

    def test_register_bq_empty_sql(self):
        """Alias with empty SQL after '=' should raise."""
        with pytest.raises(argparse.ArgumentTypeError, match="Empty SQL"):
            _parse_register_bq("alias=")

    def test_register_bq_empty_alias(self):
        """'=SELECT ...' (empty alias) should raise."""
        with pytest.raises(argparse.ArgumentTypeError, match="Invalid --register-bq format"):
            _parse_register_bq("=SELECT 1")

    def test_multiple_register_bq(self):
        """Multiple --register-bq args should be collected into a list."""
        parser = build_parser()
        args = parser.parse_args([
            "--sql", "SELECT 1",
            "--register-bq", "t1=SELECT a FROM x",
            "--register-bq", "t2=SELECT b FROM y",
        ])
        assert len(args.bq_registrations) == 2
        assert args.bq_registrations[0] == ("t1", "SELECT a FROM x")
        assert args.bq_registrations[1] == ("t2", "SELECT b FROM y")

    def test_default_format_is_none(self):
        """Default --format should be None (uses config default at runtime)."""
        parser = build_parser()
        args = parser.parse_args(["--sql", "SELECT 1"])
        assert args.fmt is None

    def test_explicit_format(self):
        """Explicit --format should be respected."""
        parser = build_parser()
        args = parser.parse_args(["--sql", "SELECT 1", "--format", "csv"])
        assert args.fmt == "csv"

    def test_invalid_format_rejected(self):
        """Invalid --format value should cause parser error."""
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--sql", "SELECT 1", "--format", "xml"])

    def test_no_register_bq_yields_empty_list(self):
        """When no --register-bq is provided, bq_registrations defaults to []."""
        parser = build_parser()
        args = parser.parse_args(["--sql", "SELECT 1"])
        assert args.bq_registrations == []

    def test_register_bq_sql_with_equals(self):
        """SQL containing '=' should be parsed correctly (split only on first '=')."""
        result = _parse_register_bq("view=SELECT * FROM t WHERE col = 5")
        assert result[0] == "view"
        assert result[1] == "SELECT * FROM t WHERE col = 5"

    def test_quiet_flag(self):
        """--quiet should set quiet=True."""
        parser = build_parser()
        args = parser.parse_args(["--sql", "SELECT 1", "--quiet"])
        assert args.quiet is True

    def test_max_rows_parsing(self):
        """--max-rows should be parsed as integer."""
        parser = build_parser()
        args = parser.parse_args(["--sql", "SELECT 1", "--max-rows", "500"])
        assert args.max_rows == 500


# ---------------------------------------------------------------------------
# Tests: Local view setup
# ---------------------------------------------------------------------------

class TestLocalViewSetup:
    """Test _setup_local_views via create_local_views with tmp_path fixture."""

    def test_creates_views_from_parquet(self, tmp_local_project, duckdb_conn):
        """Local tables should be available as DuckDB views after setup."""
        project_root, data_dir = tmp_local_project

        with patch("scripts.duckdb_manager.find_project_root", return_value=project_root):
            from src.remote_query import _setup_local_views
            created = _setup_local_views(duckdb_conn, data_dir, quiet=True)

        assert "orders" in created
        assert "products" in created

        # Verify data is queryable
        count = duckdb_conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        assert count == 5

    def test_skips_remote_tables(self, tmp_local_project, duckdb_conn):
        """Remote tables (query_mode='remote') should NOT create local views."""
        project_root, data_dir = tmp_local_project

        with patch("scripts.duckdb_manager.find_project_root", return_value=project_root):
            from src.remote_query import _setup_local_views
            created = _setup_local_views(duckdb_conn, data_dir, quiet=True)

        assert "traffic" not in created

        # Verify the remote table is not queryable
        tables = [row[0] for row in duckdb_conn.execute("SHOW TABLES").fetchall()]
        assert "traffic" not in tables

    def test_includes_hybrid_tables(self, tmp_local_project, duckdb_conn):
        """Hybrid tables (query_mode='hybrid') should create local views."""
        project_root, data_dir = tmp_local_project

        with patch("scripts.duckdb_manager.find_project_root", return_value=project_root):
            from src.remote_query import _setup_local_views
            created = _setup_local_views(duckdb_conn, data_dir, quiet=True)

        assert "inventory" in created

        count = duckdb_conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]
        assert count == 2


# ---------------------------------------------------------------------------
# Tests: BQ registration with safety checks
# ---------------------------------------------------------------------------

class TestBQRegistration:
    """Test BQ result validation and registration (mocked BigQuery)."""

    @staticmethod
    def _make_mock_bq_client(count_result: int = 100, schema_fields: int = 5):
        """Create a mock BQ client that returns controlled count and schema.

        Args:
            count_result: Row count returned by COUNT(*) query
            schema_fields: Number of fields in the schema
        """
        mock_client = MagicMock()

        # COUNT(*) query result
        count_row = MagicMock()
        count_row.__getitem__ = MagicMock(return_value=count_result)
        count_iter = iter([count_row])

        # Schema query result (LIMIT 0)
        mock_schema_fields = [MagicMock() for _ in range(schema_fields)]
        mock_schema = MagicMock()
        mock_schema.__len__ = MagicMock(return_value=schema_fields)

        # Use side_effect to return different results for different queries
        def query_side_effect(sql):
            job = MagicMock()
            if sql.startswith("SELECT COUNT(*)"):
                result = MagicMock()
                result.__iter__ = MagicMock(return_value=iter([count_row]))
                job.result.return_value = result
            elif "LIMIT 0" in sql:
                result = MagicMock()
                result.schema = mock_schema_fields
                job.result.return_value = result
            return job

        mock_client.query.side_effect = query_side_effect
        return mock_client

    def test_count_check_blocks_large_result(self):
        """BQ sub-query exceeding max_rows should raise RemoteQueryError."""
        mock_client = self._make_mock_bq_client(count_result=1_000_000)

        with pytest.raises(RemoteQueryError, match="would return 1,000,000 rows"):
            _validate_bq_result_size(
                bq_client=mock_client,
                sql="SELECT * FROM big_table",
                alias="big_table",
                max_rows=500_000,
            )

    def test_validates_small_result_passes(self):
        """BQ sub-query within limits should return the row count."""
        mock_client = self._make_mock_bq_client(count_result=1000)

        row_count = _validate_bq_result_size(
            bq_client=mock_client,
            sql="SELECT * FROM small_table",
            alias="small_table",
            max_rows=500_000,
        )

        assert row_count == 1000

    def test_memory_estimate_blocks_huge_result(self):
        """_register_bq_views should refuse when estimated memory exceeds 2 GB."""
        # Create a mock that passes count check but fails memory check
        # 500K rows x 100 cols x 50 bytes/cell = ~2384 MB > 2048 MB limit
        mock_client = self._make_mock_bq_client(count_result=500_000, schema_fields=100)

        conn = duckdb.connect(":memory:")
        try:
            with patch("src.remote_query._create_bq_client", return_value=mock_client), \
                 patch.dict(os.environ, {"BIGQUERY_PROJECT": "test-proj"}):
                with pytest.raises(RemoteQueryError, match="estimated memory"):
                    _register_bq_views(
                        conn=conn,
                        registrations=[("huge", "SELECT * FROM huge_table")],
                        max_bq_rows=1_000_000,
                        timeout_seconds=60,
                        quiet=True,
                    )
        finally:
            conn.close()

    def test_registers_small_result(self):
        """BQ sub-query within all limits should register successfully."""
        # Small result: 100 rows x 5 cols = ~0.02 MB
        mock_client = self._make_mock_bq_client(count_result=100, schema_fields=5)

        # Mock register_bq_table to return the row count
        conn = duckdb.connect(":memory:")
        try:
            with patch("src.remote_query._create_bq_client", return_value=mock_client), \
                 patch("src.remote_query.register_bq_table", return_value=100) as mock_reg, \
                 patch.dict(os.environ, {"BIGQUERY_PROJECT": "test-proj"}):
                results = _register_bq_views(
                    conn=conn,
                    registrations=[("small_view", "SELECT * FROM small_table")],
                    max_bq_rows=500_000,
                    timeout_seconds=60,
                    quiet=True,
                )

            assert results == {"small_view": 100}
            mock_reg.assert_called_once()
        finally:
            conn.close()

    def test_missing_bigquery_project_raises(self):
        """Missing BIGQUERY_PROJECT env var should raise RemoteQueryError."""
        conn = duckdb.connect(":memory:")
        try:
            with patch.dict(os.environ, {}, clear=True):
                with pytest.raises(RemoteQueryError, match="BIGQUERY_PROJECT"):
                    _register_bq_views(
                        conn=conn,
                        registrations=[("v", "SELECT 1")],
                        max_bq_rows=100,
                        timeout_seconds=60,
                    )
        finally:
            conn.close()

    def test_empty_registrations_returns_empty(self):
        """Empty registration list should return empty dict without BQ calls."""
        conn = duckdb.connect(":memory:")
        try:
            result = _register_bq_views(
                conn=conn,
                registrations=[],
                max_bq_rows=100,
                timeout_seconds=60,
            )
            assert result == {}
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Tests: Memory estimation
# ---------------------------------------------------------------------------

class TestMemoryEstimation:
    """Test _estimate_memory_mb calculation."""

    def test_small_table(self):
        """100 rows x 10 cols = 50_000 bytes ~ 0.048 MB."""
        result = _estimate_memory_mb(100, 10)
        assert abs(result - 50_000 / (1024 * 1024)) < 0.001

    def test_large_table(self):
        """1M rows x 50 cols x 50 bytes = ~2384 MB."""
        result = _estimate_memory_mb(1_000_000, 50)
        expected = (1_000_000 * 50 * 50) / (1024 * 1024)
        assert abs(result - expected) < 0.01

    def test_zero_rows(self):
        """Zero rows should return 0 MB."""
        assert _estimate_memory_mb(0, 50) == 0.0

    def test_zero_columns(self):
        """Zero columns should return 0 MB."""
        assert _estimate_memory_mb(1000, 0) == 0.0


# ---------------------------------------------------------------------------
# Tests: Output formatting
# ---------------------------------------------------------------------------

class TestOutputFormatting:
    """Test _print_table and _format_output for various formats."""

    def test_table_format_aligned(self, capsys):
        """Table format should produce aligned columns with header separator."""
        columns = ["id", "name", "value"]
        rows = [(1, "alice", 100), (2, "bob", 200)]

        _print_table(columns, rows)

        output = capsys.readouterr().out
        lines = output.strip().split("\n")

        # Header line
        assert "id" in lines[0]
        assert "name" in lines[0]
        assert "value" in lines[0]

        # Separator line
        assert "-+-" in lines[1]

        # Data rows
        assert "alice" in lines[2]
        assert "bob" in lines[3]

        # Row count footer
        assert "(2 rows)" in output

    def test_table_format_empty_result(self, capsys):
        """Empty result should print '(empty result)'."""
        _print_table(["col1"], [])

        output = capsys.readouterr().out
        assert "(empty result)" in output

    def test_table_format_null_values(self, capsys):
        """None values should be rendered as 'NULL'."""
        _print_table(["col"], [(None,)])

        output = capsys.readouterr().out
        assert "NULL" in output

    def test_csv_format(self, tmp_path, duckdb_conn):
        """CSV output should contain header + data rows."""
        duckdb_conn.execute("CREATE TABLE test AS SELECT 1 AS id, 'hello' AS msg")

        output_path = str(tmp_path / "result.csv")
        _format_output(
            conn=duckdb_conn,
            sql="SELECT * FROM test",
            fmt="csv",
            output_path=output_path,
            max_rows=1000,
        )

        with open(output_path) as f:
            reader = csv.reader(f)
            header = next(reader)
            rows = list(reader)

        assert header == ["id", "msg"]
        assert len(rows) == 1
        assert rows[0][1] == "hello"

    def test_csv_format_to_stdout(self, capsys, duckdb_conn):
        """CSV with no output path should write to stdout."""
        duckdb_conn.execute("CREATE TABLE test AS SELECT 42 AS val")

        _format_output(
            conn=duckdb_conn,
            sql="SELECT * FROM test",
            fmt="csv",
            output_path=None,
            max_rows=1000,
        )

        output = capsys.readouterr().out
        assert "val" in output
        assert "42" in output

    def test_json_format(self, tmp_path, duckdb_conn):
        """JSON output should contain a list of dicts."""
        duckdb_conn.execute(
            "CREATE TABLE test AS SELECT 1 AS id, 'world' AS msg"
        )

        output_path = str(tmp_path / "result.json")
        _format_output(
            conn=duckdb_conn,
            sql="SELECT * FROM test",
            fmt="json",
            output_path=output_path,
            max_rows=1000,
        )

        with open(output_path) as f:
            data = json.load(f)

        assert len(data) == 1
        assert data[0]["id"] == 1
        assert data[0]["msg"] == "world"

    def test_json_format_to_stdout(self, capsys, duckdb_conn):
        """JSON with no output path should print to stdout."""
        duckdb_conn.execute("CREATE TABLE test AS SELECT 99 AS num")

        _format_output(
            conn=duckdb_conn,
            sql="SELECT * FROM test",
            fmt="json",
            output_path=None,
            max_rows=1000,
        )

        output = capsys.readouterr().out
        data = json.loads(output)
        assert data[0]["num"] == 99

    def test_parquet_write(self, tmp_path, duckdb_conn):
        """Parquet output should create a readable parquet file."""
        duckdb_conn.execute(
            "CREATE TABLE test AS SELECT 1 AS id, 2.5 AS val"
        )

        output_path = str(tmp_path / "output" / "result.parquet")

        with patch("src.remote_query._load_remote_query_config", return_value={
            "output_dir": str(tmp_path / "default_output"),
            "timeout_seconds": 300,
            "max_result_rows": 100_000,
            "max_bq_registration_rows": 500_000,
            "default_format": "table",
        }):
            _format_output(
                conn=duckdb_conn,
                sql="SELECT * FROM test",
                fmt="parquet",
                output_path=output_path,
                max_rows=1000,
            )

        assert Path(output_path).exists()

        # Read it back and verify
        result = pq.read_table(output_path)
        assert result.num_rows == 1
        assert result.num_columns == 2
        assert result.column_names == ["id", "val"]

    def test_parquet_default_path(self, tmp_path, duckdb_conn):
        """Parquet with no output path should use config default dir."""
        duckdb_conn.execute("CREATE TABLE test AS SELECT 1 AS x")

        default_dir = str(tmp_path / "default_output")
        with patch("src.remote_query._load_remote_query_config", return_value={
            "output_dir": default_dir,
            "timeout_seconds": 300,
            "max_result_rows": 100_000,
            "max_bq_registration_rows": 500_000,
            "default_format": "table",
        }):
            _format_output(
                conn=duckdb_conn,
                sql="SELECT * FROM test",
                fmt="parquet",
                output_path=None,
                max_rows=1000,
            )

        expected_path = Path(default_dir) / "result.parquet"
        assert expected_path.exists()

    def test_unknown_format_raises(self, duckdb_conn):
        """Unknown format should raise RemoteQueryError."""
        duckdb_conn.execute("CREATE TABLE test AS SELECT 1 AS id")

        with pytest.raises(RemoteQueryError, match="Unknown format"):
            _format_output(
                conn=duckdb_conn,
                sql="SELECT * FROM test",
                fmt="xml",
                output_path=None,
                max_rows=1000,
            )


# ---------------------------------------------------------------------------
# Tests: End-to-end (local-only, no BQ mocking needed)
# ---------------------------------------------------------------------------

class TestEndToEnd:
    """End-to-end tests with local Parquet data only (no BigQuery dependency).

    Uses _patched_duckdb_connect to handle DuckDB version differences
    (statement_timeout may not be supported in all versions).
    """

    _CONFIG = {
        "timeout_seconds": 300,
        "max_result_rows": 100_000,
        "max_bq_registration_rows": 500_000,
        "default_format": "table",
        "output_dir": "/tmp/remote_query_test",
    }

    def _run(self, tmp_local_project, **kwargs):
        """Helper to run execute_remote_query with standard patches."""
        project_root, data_dir = tmp_local_project
        config = dict(self._CONFIG)
        config.update(kwargs.pop("config_overrides", {}))

        with patch("scripts.duckdb_manager.find_project_root", return_value=project_root), \
             patch("src.remote_query._load_remote_query_config", return_value=config), \
             patch("src.remote_query.duckdb") as mock_duckdb_mod:
            mock_duckdb_mod.connect = _patched_duckdb_connect
            kwargs.setdefault("data_dir", data_dir)
            kwargs.setdefault("bq_registrations", [])
            kwargs.setdefault("quiet", True)
            execute_remote_query(**kwargs)

    def test_local_only_query(self, tmp_local_project, capsys):
        """Execute a query against local Parquet views and verify table output."""
        self._run(
            tmp_local_project,
            sql="SELECT COUNT(*) AS cnt FROM orders",
            fmt="table",
        )

        output = capsys.readouterr().out
        assert "cnt" in output
        assert "5" in output

    def test_local_join_query(self, tmp_local_project, capsys):
        """JOIN between two local tables should work."""
        self._run(
            tmp_local_project,
            sql=(
                "SELECT p.name, SUM(o.amount) AS total "
                "FROM orders o JOIN products p ON o.product_id = p.product_id "
                "GROUP BY p.name ORDER BY total DESC"
            ),
            fmt="json",
        )

        output = capsys.readouterr().out
        data = json.loads(output)
        assert len(data) == 3
        # Widget: orders 1,3 -> 10+30=40
        widget = next(r for r in data if r["name"] == "Widget")
        assert widget["total"] == 40.0

    def test_result_row_limit(self, tmp_local_project, capsys):
        """Result exceeding max_rows should be truncated."""
        self._run(
            tmp_local_project,
            sql="SELECT * FROM orders ORDER BY order_id",
            fmt="table",
            max_rows=2,
            quiet=False,
            config_overrides={"max_result_rows": 2},
        )

        out = capsys.readouterr().out
        # Table output should show exactly 2 data rows
        assert "(2 rows)" in out

    def test_csv_output_to_file(self, tmp_local_project, tmp_path):
        """End-to-end CSV output written to a file."""
        output_path = str(tmp_path / "result.csv")

        self._run(
            tmp_local_project,
            sql="SELECT order_id, amount FROM orders ORDER BY order_id",
            fmt="csv",
            output=output_path,
        )

        with open(output_path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        assert len(rows) == 5
        assert rows[0]["order_id"] == "1"
        assert rows[0]["amount"] == "10.0"

    def test_hybrid_table_queryable(self, tmp_local_project, capsys):
        """Hybrid table should be accessible in local queries."""
        self._run(
            tmp_local_project,
            sql="SELECT SUM(stock) AS total_stock FROM inventory",
            fmt="json",
        )

        output = capsys.readouterr().out
        data = json.loads(output)
        assert data[0]["total_stock"] == 300

    def test_quiet_mode_suppresses_stderr(self, tmp_local_project, capsys):
        """With quiet=True, no progress messages should appear on stderr."""
        self._run(
            tmp_local_project,
            sql="SELECT COUNT(*) AS cnt FROM orders",
            fmt="table",
            quiet=True,
        )

        err = capsys.readouterr().err
        # In quiet mode, _log_progress should not emit anything
        assert "Setting up" not in err
        assert "local views" not in err
