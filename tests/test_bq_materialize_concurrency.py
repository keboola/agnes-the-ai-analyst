"""Per-table_id concurrency: in-process mutex + advisory file lock with
TTL reclaim. Two overlapping materialize_query calls for the same id
must NOT corrupt each other's parquet."""
from __future__ import annotations
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from connectors.bigquery.extractor import (
    materialize_query,
    MaterializeInFlightError,
    _get_table_lock,
    _LOCK_TTL_DEFAULT_SECONDS,
)


@pytest.fixture(autouse=True)
def reset_locks(monkeypatch):
    # Tests must not share lock state across runs.
    import connectors.bigquery.extractor as mod
    monkeypatch.setattr(mod, "_table_locks", {})
    yield


def _slow_bq(stall_seconds: float = 1.0):
    """Build a fake BqAccess whose duckdb_session COPY blocks for
    `stall_seconds` so we can race a second call against it."""
    bq = MagicMock()
    bq.projects.billing = "prj-billing"
    bq.projects.data = "prj-data"

    class _Session:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def execute(self, sql):
            if sql.startswith("SELECT database_name"):
                class _R:
                    def fetchall(self):
                        return [("memory",)]
                return _R()
            if sql.startswith("ATTACH"):
                return MagicMock()
            if sql.startswith("COPY"):
                # Simulate a long-running COPY by writing a stub parquet
                # then sleeping so a second call can race us.
                # Extract the path from the COPY statement.
                import re
                m = re.search(r"TO '([^']+)'", sql)
                assert m
                Path(m.group(1)).write_bytes(b"PARQUET_STUB_HEADER" + b"\x00" * 200)
                time.sleep(stall_seconds)
                return MagicMock()
            if sql.startswith("SELECT count"):
                class _R:
                    def fetchone(self):
                        return (42,)
                return _R()
            return MagicMock()

    bq.duckdb_session.return_value = _Session()
    return bq


def test_concurrent_calls_for_same_id_raise_in_flight(tmp_path):
    bq = _slow_bq(stall_seconds=2.0)

    out_dir = str(tmp_path)
    captured: list = []

    def runner(tag):
        try:
            r = materialize_query(
                table_id="t1", sql="SELECT 1",
                bq=bq, output_dir=out_dir, max_bytes=None,
            )
            captured.append(("ok", tag, r))
        except MaterializeInFlightError as e:
            captured.append(("in_flight", tag, str(e)))
        except Exception as e:
            captured.append(("err", tag, str(e)))

    t1 = threading.Thread(target=runner, args=("first",))
    t2 = threading.Thread(target=runner, args=("second",))
    t1.start()
    time.sleep(0.2)  # let t1 acquire the lock
    t2.start()
    t1.join()
    t2.join()

    outcomes = [c[0] for c in captured]
    assert outcomes.count("ok") == 1, f"expected exactly one success, got {captured}"
    assert outcomes.count("in_flight") == 1


def test_sequential_calls_for_same_id_both_succeed(tmp_path):
    bq = _slow_bq(stall_seconds=0.05)

    out_dir = str(tmp_path)
    r1 = materialize_query(
        table_id="t1", sql="SELECT 1",
        bq=bq, output_dir=out_dir, max_bytes=None,
    )
    r2 = materialize_query(
        table_id="t1", sql="SELECT 1",
        bq=bq, output_dir=out_dir, max_bytes=None,
    )
    assert r1["rows"] == 42
    assert r2["rows"] == 42


def test_different_ids_run_in_parallel(tmp_path):
    bq = _slow_bq(stall_seconds=1.0)
    out_dir = str(tmp_path)
    captured: list = []

    def runner(tid):
        try:
            r = materialize_query(
                table_id=tid, sql="SELECT 1",
                bq=bq, output_dir=out_dir, max_bytes=None,
            )
            captured.append((tid, r["rows"]))
        except Exception as e:
            captured.append((tid, "ERROR"))

    threads = [threading.Thread(target=runner, args=(f"tab_{i}",)) for i in range(3)]
    start = time.time()
    for t in threads: t.start()
    for t in threads: t.join()
    elapsed = time.time() - start
    # If they were serialized, would take >= 3s. Parallel: ~1s.
    assert elapsed < 2.0, f"expected parallel, elapsed={elapsed:.2f}s"
    assert len(captured) == 3
    assert all(c[1] == 42 for c in captured)


def test_stale_file_lock_is_reclaimed_after_ttl(tmp_path, monkeypatch):
    """Verify a stale, unheld .lock file (old mtime, no live flock holder) does NOT
    cause `MaterializeInFlightError`. The reclaim branch in `_try_acquire_file_lock`
    is technically not reached here (the first `_try_open_and_flock` succeeds because
    nobody holds the lock), but exercising the in-flight-by-mtime-only mistake is what
    this test guards against."""
    bq = _slow_bq(stall_seconds=0.05)
    lock_path = Path(tmp_path) / "data" / "t1.parquet.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("")

    # Set mtime to 25h ago (> default 24h TTL).
    old_ts = time.time() - 25 * 3600
    os.utime(lock_path, (old_ts, old_ts))

    r = materialize_query(
        table_id="t1", sql="SELECT 1",
        bq=bq, output_dir=str(tmp_path), max_bytes=None,
    )
    assert r["rows"] == 42


def test_fresh_file_lock_blocks_with_in_flight_error(tmp_path, monkeypatch):
    """Force a fresh .lock file (mtime within TTL) and verify a new
    call raises rather than reclaims."""
    bq = _slow_bq(stall_seconds=0.05)
    lock_path = Path(tmp_path) / "data" / "t1.parquet.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # Open the lock file and HOLD a fcntl exclusive lock so the materialize
    # call's flock(LOCK_NB) sees a real conflicting lock — relying on
    # mtime-only would let the test pass even if flock acquisition was
    # broken.
    import fcntl
    holder = open(lock_path, "w")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(MaterializeInFlightError):
            materialize_query(
                table_id="t1", sql="SELECT 1",
                bq=bq, output_dir=str(tmp_path), max_bytes=None,
            )
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()


def test_lock_ttl_reads_from_instance_config(tmp_path, monkeypatch):
    """When `materialize.lock_ttl_seconds` is set in instance.yaml, that
    value overrides the default."""
    # Patches `app.instance_config.get_value` directly. This works because
    # `_get_lock_ttl_seconds` re-imports `get_value` on every call (see
    # extractor.py for the deferred-import rationale). If a future change
    # hoists the import to module-level, this patch must change to target
    # `connectors.bigquery.extractor.get_value` instead.
    monkeypatch.setattr(
        "app.instance_config.get_value",
        lambda *args, **kw: 60 if args == ("materialize", "lock_ttl_seconds") else kw.get("default"),
    )

    from connectors.bigquery.extractor import _get_lock_ttl_seconds
    assert _get_lock_ttl_seconds() == 60
