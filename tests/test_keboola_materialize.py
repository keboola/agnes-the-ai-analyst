"""Tests for the Keboola materialize_query path."""
import hashlib
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from connectors.keboola import extractor as kbe


def test_materialize_query_writes_parquet_and_returns_metadata(tmp_path, monkeypatch):
    """Mock-mode: feed in a fake KeboolaAccess that yields a fake DuckDB
    connection accepting `COPY ... TO '...' (FORMAT PARQUET)` and just
    writes a small parquet via duckdb's own primitive on a tmp DB.
    """
    import duckdb
    real_conn = duckdb.connect(":memory:")
    # Pre-create a small relation the fake materialize "copies".
    real_conn.execute("CREATE TABLE t AS SELECT 1 AS x, 'hello' AS y UNION ALL SELECT 2, 'world'")

    class FakeAccess:
        def duckdb_session(self):
            from contextlib import contextmanager
            @contextmanager
            def _cm():
                yield real_conn
            return _cm()
    fake_access = FakeAccess()

    output_dir = tmp_path / "out"
    output_dir.mkdir()

    # Submit a query that selects from the in-memory table (not a real
    # Keboola bucket — the test verifies the COPY/parquet/hash path,
    # not the extension behavior).
    result = kbe.materialize_query(
        table_id="example_subset",
        sql="SELECT * FROM t",
        keboola_access=fake_access,
        output_dir=output_dir,
    )

    parquet_path = output_dir / "example_subset.parquet"
    assert parquet_path.exists()
    assert result["table_id"] == "example_subset"
    assert result["path"] == str(parquet_path)
    assert result["rows"] == 2
    assert result["bytes"] > 0
    # MD5 of the bytes should match what we recompute.
    expected_md5 = hashlib.md5(parquet_path.read_bytes()).hexdigest()
    assert result["md5"] == expected_md5


def test_materialize_query_zero_rows_logs_warning(tmp_path, caplog):
    import duckdb
    real_conn = duckdb.connect(":memory:")
    real_conn.execute("CREATE TABLE t AS SELECT 1 AS x WHERE FALSE")

    class FakeAccess:
        def duckdb_session(self):
            from contextlib import contextmanager
            @contextmanager
            def _cm():
                yield real_conn
            return _cm()

    output_dir = tmp_path / "out"
    output_dir.mkdir()

    with caplog.at_level("WARNING"):
        result = kbe.materialize_query(
            table_id="empty_subset",
            sql="SELECT * FROM t",
            keboola_access=FakeAccess(),
            output_dir=output_dir,
        )
    assert result["rows"] == 0
    assert "0 rows" in caplog.text or "empty" in caplog.text.lower()


def test_materialize_query_rejects_unsafe_table_id(tmp_path):
    """Defense: table_id is interpolated into the parquet filename. SQL/
    path-traversal-unsafe values must be rejected up-front (mirror of BQ
    materialize_query's validation)."""
    class FakeAccess:
        def duckdb_session(self):
            raise AssertionError("should not be called")
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    with pytest.raises(ValueError, match="table_id"):
        kbe.materialize_query(
            table_id="../../etc/passwd",
            sql="SELECT 1",
            keboola_access=FakeAccess(),
            output_dir=output_dir,
        )
