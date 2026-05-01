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


def test_keboola_materialize_atomic_write_on_failure(tmp_path, monkeypatch):
    """Devin finding 2026-05-01 (BUG_pr-review-job-3fbd31c9_0003):
    if the COPY raises mid-stream, no partial file is left at the final
    .parquet path AND the .parquet.tmp staging file is cleaned up. Pre-fix,
    materialize_query wrote directly to the final path, so a network/disk
    error mid-COPY would leave a corrupt parquet that the orchestrator
    rebuild could pick up and serve to analysts."""
    from connectors.keboola import extractor as kbe

    output_dir = tmp_path / "data"
    output_dir.mkdir()

    class FakeAccess:
        def duckdb_session(self):
            from contextlib import contextmanager

            class FailingConn:
                def execute(self, sql, *a, **kw):
                    if "COPY" in sql:
                        raise RuntimeError("simulated mid-COPY failure")
                    raise AssertionError("unexpected execute: " + sql)

                def close(self):
                    pass

            @contextmanager
            def _cm():
                yield FailingConn()
            return _cm()

    with pytest.raises(RuntimeError, match="simulated mid-COPY failure"):
        kbe.materialize_query(
            table_id="atomic_test",
            sql="SELECT 1",
            keboola_access=FakeAccess(),
            output_dir=output_dir,
        )

    # Final parquet must NOT exist (we never reached os.replace).
    final_path = output_dir / "atomic_test.parquet"
    assert not final_path.exists(), (
        f"Partial parquet left at final path {final_path} — orchestrator "
        f"rebuild would pick this up and serve corrupt data."
    )
    # tmp file also cleaned up (the extractor unlinks it on COPY failure).
    tmp_path_marker = output_dir / "atomic_test.parquet.tmp"
    assert not tmp_path_marker.exists(), (
        f"Stale .parquet.tmp left at {tmp_path_marker}"
    )


def test_keboola_materialize_uses_tmp_path_during_copy(tmp_path):
    """Atomic-write contract: COPY targets <id>.parquet.tmp first (verifiable
    via the SQL string passed to conn.execute). After success, the file lands
    at <id>.parquet (no .tmp suffix). This documents the contract that
    BUG_pr-review-job-3fbd31c9_0003 closed."""
    import duckdb
    from connectors.keboola import extractor as kbe

    real_conn = duckdb.connect(":memory:")
    real_conn.execute("CREATE TABLE t AS SELECT 1 AS x, 'hello' AS y")

    sqls_seen = []

    class TracingConn:
        """Thin wrapper that records SQL strings. DuckDBPyConnection.execute
        is read-only, so monkey-patching the method directly fails."""

        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *args, **kwargs):
            sqls_seen.append(sql)
            return self._inner.execute(sql, *args, **kwargs)

        def close(self):
            self._inner.close()

    class FakeAccess:
        def duckdb_session(self):
            from contextlib import contextmanager

            @contextmanager
            def _cm():
                yield TracingConn(real_conn)
            return _cm()

    output_dir = tmp_path / "data"
    output_dir.mkdir()

    result = kbe.materialize_query(
        table_id="tmp_path_test",
        sql="SELECT * FROM t",
        keboola_access=FakeAccess(),
        output_dir=output_dir,
    )

    # COPY SQL targeted .parquet.tmp.
    copy_sql = next((s for s in sqls_seen if "COPY" in s), None)
    assert copy_sql is not None, sqls_seen
    assert ".parquet.tmp" in copy_sql, copy_sql

    # Final file landed without .tmp suffix.
    assert (output_dir / "tmp_path_test.parquet").exists()
    assert not (output_dir / "tmp_path_test.parquet.tmp").exists()
    assert result["path"].endswith(".parquet")
    assert not result["path"].endswith(".tmp")
