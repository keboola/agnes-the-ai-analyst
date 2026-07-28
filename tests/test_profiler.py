"""Tests for the data profiler batch functions and profile_table output format."""

import tempfile
from datetime import date
from pathlib import Path

import duckdb
import pytest

from src.profiler import (
    TableInfo,
    _batch_base_stats,
    _batch_boolean_stats,
    _batch_date_stats,
    _batch_numeric_stats,
    _batch_string_stats,
    _round,
    classify_type,
    profile_table,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def con_with_data():
    """Create a DuckDB connection with a materialized test table."""
    con = duckdb.connect()
    con.execute("""
        CREATE TABLE tbl AS SELECT * FROM (VALUES
            (1,   'alice',   TRUE,  DATE '2023-01-15', 10.5),
            (2,   'bob',     FALSE, DATE '2023-06-20', 0.0),
            (3,   'alice',   TRUE,  DATE '2024-01-10', -3.2),
            (4,   NULL,      NULL,  NULL,              NULL),
            (5,   'charlie', FALSE, DATE '2024-07-01', 100.0)
        ) AS t(id, name, active, created, amount)
    """)
    yield con
    con.close()


@pytest.fixture
def parquet_dir(tmp_path):
    """Write a small parquet file and return its path."""
    con = duckdb.connect()
    parquet_path = tmp_path / "test_table.parquet"
    con.execute(f"""
        COPY (
            SELECT * FROM (VALUES
                (1,   'alice',   TRUE,  DATE '2023-01-15', 10.5),
                (2,   'bob',     FALSE, DATE '2023-06-20', 0.0),
                (3,   'alice',   TRUE,  DATE '2024-01-10', -3.2),
                (4,   NULL,      NULL,  NULL,              NULL),
                (5,   'charlie', FALSE, DATE '2024-07-01', 100.0)
            ) AS t(id, name, active, created, amount)
        ) TO '{parquet_path}' (FORMAT PARQUET)
    """)
    con.close()
    return parquet_path


# ---------------------------------------------------------------------------
# Unit tests: batch functions
# ---------------------------------------------------------------------------
class TestBatchBaseStats:
    def test_counts(self, con_with_data):
        result = _batch_base_stats(con_with_data, "tbl", ["id", "name", "active", "created", "amount"])
        # id: 5 non-null, 5 unique
        assert result["id"] == (5, 5)
        # name: 4 non-null (one NULL), 3 unique (alice, bob, charlie)
        assert result["name"] == (4, 3)
        # active: 4 non-null, 2 unique (TRUE, FALSE)
        assert result["active"] == (4, 2)
        # created: 4 non-null, 4 unique dates
        assert result["created"] == (4, 4)
        # amount: 4 non-null, 4 unique
        assert result["amount"] == (4, 4)

    def test_empty_columns(self, con_with_data):
        result = _batch_base_stats(con_with_data, "tbl", [])
        assert result == {}


class TestBatchNumericStats:
    def test_aggregates(self, con_with_data):
        result = _batch_numeric_stats(con_with_data, "tbl", ["id", "amount"])
        assert "id" in result
        assert "amount" in result

        # id: min=1, max=5
        assert result["id"]["min"] == 1
        assert result["id"]["max"] == 5

        # amount: min=-3.2, max=100.0, has zeros=1, negative=1
        # DuckDB may return Decimal for DECIMAL columns; compare as float
        assert float(result["amount"]["min"]) == pytest.approx(-3.2)
        assert float(result["amount"]["max"]) == pytest.approx(100.0)
        assert result["amount"]["zeros"] == 1
        assert result["amount"]["negative"] == 1

    def test_empty(self, con_with_data):
        result = _batch_numeric_stats(con_with_data, "tbl", [])
        assert result == {}


class TestBatchStringStats:
    def test_lengths(self, con_with_data):
        result = _batch_string_stats(con_with_data, "tbl", ["name"])
        assert "name" in result
        # alice=5, bob=3, charlie=7 -> min=3, max=7
        assert result["name"]["min_length"] == 3
        assert result["name"]["max_length"] == 7
        assert result["name"]["avg_length"] == pytest.approx(5.0)  # (5+3+5+7)/4 = 5.0

    def test_empty(self, con_with_data):
        result = _batch_string_stats(con_with_data, "tbl", [])
        assert result == {}


class TestBatchDateStats:
    def test_range(self, con_with_data):
        result = _batch_date_stats(con_with_data, "tbl", ["created"], {"created": "DATE"})
        assert "created" in result
        assert result["created"]["earliest"] == "2023-01-15"
        assert result["created"]["latest"] == "2024-07-01"
        assert result["created"]["span_days"] is not None
        assert result["created"]["span_days"] > 0

    def test_empty(self, con_with_data):
        result = _batch_date_stats(con_with_data, "tbl", [], {})
        assert result == {}


class TestBatchBooleanStats:
    def test_counts(self, con_with_data):
        result = _batch_boolean_stats(con_with_data, "tbl", ["active"])
        assert "active" in result
        assert result["active"]["true_count"] == 2
        assert result["active"]["false_count"] == 2
        assert result["active"]["true_pct"] == 50.0

    def test_empty(self, con_with_data):
        result = _batch_boolean_stats(con_with_data, "tbl", [])
        assert result == {}


# ---------------------------------------------------------------------------
# Integration test: profile_table output format
# ---------------------------------------------------------------------------
class TestProfileTable:
    def test_output_structure(self, parquet_dir):
        """Verify profile_table produces the expected JSON structure."""
        table = TableInfo(
            table_id="test.test_table",
            name="test_table",
            description="Test table",
            primary_key="id",
            sync_strategy="full",
        )
        profile = profile_table(
            table=table,
            parquet_path=parquet_dir,
            all_tables=[table],
            sync_state={},
            metrics_map={},
        )

        # Top-level keys
        assert profile["table_id"] == "test.test_table"
        assert profile["row_count"] == 5
        assert profile["column_count"] == 5
        assert profile["sampled"] is False
        assert "columns" in profile
        assert "sample_rows" in profile
        assert "alerts" in profile
        assert "variable_types" in profile

        # Column profiles
        col_names = [c["name"] for c in profile["columns"]]
        assert "id" in col_names
        assert "name" in col_names
        assert "active" in col_names
        assert "created" in col_names
        assert "amount" in col_names

        # Check column structure
        for col in profile["columns"]:
            assert "name" in col
            assert "type" in col
            assert "type_category" in col
            assert "completeness_pct" in col
            assert "null_count" in col
            assert "unique_count" in col
            assert "unique_pct" in col
            assert "sample_values" in col
            assert "is_primary_key" in col
            assert "alerts" in col

        # Numeric column has numeric_stats
        amount_col = next(c for c in profile["columns"] if c["name"] == "amount")
        assert "numeric_stats" in amount_col
        ns = amount_col["numeric_stats"]
        assert "min" in ns
        assert "max" in ns
        assert "mean" in ns
        assert "median" in ns
        assert "histogram" in ns

        # String column has string_stats
        name_col = next(c for c in profile["columns"] if c["name"] == "name")
        assert "string_stats" in name_col
        ss = name_col["string_stats"]
        assert "min_length" in ss
        assert "top_values" in ss

        # Date column has date_stats
        created_col = next(c for c in profile["columns"] if c["name"] == "created")
        assert "date_stats" in created_col
        ds = created_col["date_stats"]
        assert "earliest" in ds
        assert "latest" in ds
        assert "histogram" in ds

        # Boolean column has boolean_stats
        active_col = next(c for c in profile["columns"] if c["name"] == "active")
        assert "boolean_stats" in active_col
        bs = active_col["boolean_stats"]
        assert "true_count" in bs
        assert "false_count" in bs

    def test_sample_rows(self, parquet_dir):
        """Verify sample_rows are populated."""
        table = TableInfo(
            table_id="test.test_table",
            name="test_table",
            description="Test",
            primary_key="id",
            sync_strategy="full",
        )
        profile = profile_table(table, parquet_dir, [table], {}, {})
        assert len(profile["sample_rows"]) == 5
        assert "id" in profile["sample_rows"][0]


# ---------------------------------------------------------------------------
# Unit tests: helpers
# ---------------------------------------------------------------------------
class TestClassifyType:
    def test_numeric(self):
        assert classify_type("INTEGER") == "NUMERIC"
        assert classify_type("BIGINT") == "NUMERIC"
        assert classify_type("DOUBLE") == "NUMERIC"
        assert classify_type("FLOAT") == "NUMERIC"

    def test_string(self):
        assert classify_type("VARCHAR") == "STRING"
        assert classify_type("TEXT") == "STRING"

    def test_boolean(self):
        assert classify_type("BOOLEAN") == "BOOLEAN"
        assert classify_type("BOOL") == "BOOLEAN"

    def test_date(self):
        assert classify_type("DATE") == "DATE"
        assert classify_type("TIMESTAMP") == "TIMESTAMP"
        assert classify_type("TIMESTAMP WITH TIME ZONE") == "TIMESTAMP"


class TestProfileTableEmpty:
    def test_empty_table(self, tmp_path):
        """Verify profiler handles empty tables gracefully."""
        con = duckdb.connect()
        parquet_path = tmp_path / "empty.parquet"
        con.execute(f"""
            COPY (
                SELECT 1 AS id, 'x' AS name WHERE false
            ) TO '{parquet_path}' (FORMAT PARQUET)
        """)
        con.close()

        table = TableInfo(
            table_id="test.empty",
            name="empty",
            description="Empty table",
            primary_key="id",
            sync_strategy="full",
        )
        profile = profile_table(table, parquet_path, [table], {}, {})
        assert profile["row_count"] == 0
        assert profile["column_count"] == 2
        assert profile["missing_cells_pct"] == 0.0


class TestClassifyTypeParameterized:
    def test_decimal_with_precision(self):
        assert classify_type("DECIMAL(18,3)") == "NUMERIC"
        assert classify_type("DECIMAL(4,1)") == "NUMERIC"

    def test_numeric_with_precision(self):
        assert classify_type("NUMERIC(10,2)") == "NUMERIC"


class TestRound:
    def test_float(self):
        assert _round(3.14159) == 3.14

    def test_none(self):
        assert _round(None) is None

    def test_int(self):
        assert _round(42) == 42

    def test_nan(self):
        assert _round(float("nan")) is None

    def test_inf(self):
        assert _round(float("inf")) is None
