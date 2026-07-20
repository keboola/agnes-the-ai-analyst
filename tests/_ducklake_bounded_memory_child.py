"""Child-process helpers for
``tests/test_orchestrator_ducklake.py::test_ingest_bounded_memory_streams_large_table``.

Not a test module itself (no ``test_*`` prefix, not collected by pytest).

Two subcommands, run as two SEPARATE processes by the parent test:

- ``build <data_dir> <n_rows>``: writes a real multi-hundred-MB
  parquet-backed extract under ``<data_dir>/extracts/bigsrc``. Building N
  million rows of pyarrow arrays inflates *this* process's own peak RSS
  high-water mark (``ru_maxrss`` never comes back down within a process)
  well past anything the ingest step itself needs — that's why this is a
  separate process from ``ingest`` below, not just a separate function.
- ``ingest <data_dir> <memory_limit>``: imports the full app stack (which
  is itself not free — ``src.orchestrator`` pulls in FastAPI, the BigQuery
  connector, etc.) BEFORE taking its "before" RSS snapshot, then runs the
  real ``SyncOrchestrator().rebuild()`` against the extract ``build``
  produced, and reports the *marginal* peak RSS growth caused specifically
  by that call — not by process startup or fixture construction. Prints
  ``RESULT_OK <row_count> <delta_mb>`` on success, or ``RESULT_FAIL
  <error>`` and exits nonzero on failure.

Splitting fixture-build and ingest-measurement into separate processes (and,
within the ``ingest`` process, taking the "before" snapshot only after all
imports have already happened) is what makes the parent test's RSS delta
assertion meaningful instead of vacuously passing — see that test's
docstring for the numbers this was calibrated against.
"""

from __future__ import annotations

import os
import sys

# Running as `python tests/_ducklake_bounded_memory_child.py` puts `tests/`
# (not the repo root) at sys.path[0], so `import src...` below would fail
# unless the caller happens to set PYTHONPATH/cwd just right. Insert the
# repo root explicitly so this script is invokable from anywhere.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _rss_mb(ru_maxrss: int) -> float:
    # ru_maxrss is bytes on macOS/BSD, KB on Linux.
    divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
    return ru_maxrss / divisor


def _cmd_build(data_dir: str, n_rows: int) -> int:
    import pyarrow as pa
    import pyarrow.parquet as pq
    import duckdb

    extracts_dir = os.path.join(data_dir, "extracts")
    source_dir = os.path.join(extracts_dir, "bigsrc")
    data_subdir = os.path.join(source_dir, "data")
    os.makedirs(data_subdir, exist_ok=True)

    pq_path = os.path.join(data_subdir, "bigtable.parquet")
    ids = pa.array(range(n_rows), type=pa.int64())
    val = pa.array([i % 1000 for i in range(n_rows)], type=pa.int64())
    s = pa.array([f"row-{i:08d}-payload-xxxxxxxxxxxxxxxxxxxx" for i in range(n_rows)], type=pa.string())
    arrow_table = pa.table({"id": ids, "val": val, "s": s})
    pq.write_table(arrow_table, pq_path, row_group_size=100_000)
    size_bytes = os.path.getsize(pq_path)
    del arrow_table, ids, val, s

    db_path = os.path.join(source_dir, "extract.duckdb")
    conn = duckdb.connect(db_path)
    try:
        conn.execute(
            """CREATE TABLE _meta (
                table_name VARCHAR, description VARCHAR, rows BIGINT,
                size_bytes BIGINT, extracted_at TIMESTAMP,
                query_mode VARCHAR DEFAULT 'local'
            )"""
        )
        safe = pq_path.replace("'", "''")
        conn.execute(f"CREATE OR REPLACE VIEW \"bigtable\" AS SELECT * FROM read_parquet('{safe}')")
        conn.execute(
            "INSERT INTO _meta VALUES ('bigtable', '', ?, ?, current_timestamp, 'local')",
            [n_rows, size_bytes],
        )
    finally:
        conn.close()

    print(f"BUILD_OK {size_bytes}")
    return 0


def _cmd_ingest(data_dir: str, memory_limit: str) -> int:
    import resource

    os.environ["DATA_DIR"] = data_dir
    os.environ["AGNES_ANALYTICS_BACKEND"] = "ducklake"
    os.environ.pop("AGNES_DUCKLAKE_CATALOG_DSN", None)
    os.environ.pop("AGNES_DUCKLAKE_DATA_PATH", None)

    # All the heavy imports (src.orchestrator pulls in FastAPI, the
    # BigQuery connector, etc.) happen BEFORE the "before" snapshot below,
    # so the measured delta reflects only the ingest call itself.
    import src.analytics_backend as ab
    import src.ducklake_session as ds
    from src.orchestrator import SyncOrchestrator

    ab.reset_analytics_backend_cache()
    ds.close_ducklake_sessions()
    ds._DUCKLAKE_WRITE_MEMORY_LIMIT = memory_limit

    try:
        rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        result = SyncOrchestrator().rebuild()
        rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

        if "bigtable" not in result.get("bigsrc", []):
            print(f"RESULT_FAIL rebuild() did not ingest bigsrc.bigtable: {result!r}")
            return 1

        r = ds.get_ducklake_read()
        try:
            cnt = r.execute('SELECT count(*) FROM lake."bigsrc"."bigtable"').fetchone()[0]
        finally:
            r.close()

        delta_mb = _rss_mb(rss_after) - _rss_mb(rss_before)
        print(f"RESULT_OK {cnt} {delta_mb:.1f}")
        return 0
    except Exception as e:  # noqa: BLE001 - reporting to parent, not re-raising
        print(f"RESULT_FAIL {type(e).__name__}: {e}")
        return 1


def main() -> int:
    mode = sys.argv[1]
    if mode == "build":
        return _cmd_build(sys.argv[2], int(sys.argv[3]))
    if mode == "ingest":
        return _cmd_ingest(sys.argv[2], sys.argv[3])
    print(f"RESULT_FAIL unknown mode {mode!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
