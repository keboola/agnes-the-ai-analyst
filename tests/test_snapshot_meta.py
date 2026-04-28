import json
import pytest
from pathlib import Path

from cli.snapshot_meta import (
    SnapshotMeta,
    write_meta,
    read_meta,
    list_snapshots,
    snapshot_lock,
)


@pytest.fixture
def snap_dir(tmp_path):
    d = tmp_path / "snapshots"
    d.mkdir()
    return d


class TestMetaIO:
    def test_round_trip(self, snap_dir):
        meta = SnapshotMeta(
            name="cz_recent", table_id="bq_view",
            select=["a", "b"], where="a > 1", limit=100, order_by=None,
            fetched_at="2026-04-27T17:30:00Z",
            effective_as_of="2026-04-27T17:30:00Z",
            rows=10, bytes_local=1024,
            estimated_scan_bytes_at_fetch=5_000_000,
            result_hash_md5="abc",
        )
        write_meta(snap_dir, meta)
        got = read_meta(snap_dir, "cz_recent")
        assert got == meta

    def test_read_missing_returns_none(self, snap_dir):
        assert read_meta(snap_dir, "missing") is None

    def test_list_snapshots_empty(self, snap_dir):
        assert list_snapshots(snap_dir) == []

    def test_list_snapshots_with_data(self, snap_dir):
        for name in ("a", "b", "c"):
            (snap_dir / f"{name}.parquet").write_bytes(b"PAR1\\x00\\x00PAR1")
            write_meta(snap_dir, SnapshotMeta(
                name=name, table_id="t", select=None, where=None, limit=None, order_by=None,
                fetched_at="t", effective_as_of="t", rows=0, bytes_local=10,
                estimated_scan_bytes_at_fetch=0, result_hash_md5="",
            ))
        names = sorted(s.name for s in list_snapshots(snap_dir))
        assert names == ["a", "b", "c"]


class TestSnapshotLock:
    def test_lock_is_exclusive(self, snap_dir, tmp_path):
        """Two processes can't both hold the lock at once."""
        import threading, time
        held_at = []
        def worker(label, hold_seconds):
            with snapshot_lock(snap_dir):
                held_at.append((label, time.time()))
                time.sleep(hold_seconds)
                held_at.append((f"{label}-done", time.time()))

        t1 = threading.Thread(target=worker, args=("A", 0.2))
        t2 = threading.Thread(target=worker, args=("B", 0.2))
        t1.start(); time.sleep(0.05); t2.start()
        t1.join(); t2.join()
        # A acquired, A-done, B acquired, B-done — never interleaved
        labels = [x[0] for x in held_at]
        assert labels in (
            ["A", "A-done", "B", "B-done"],
            ["B", "B-done", "A", "A-done"],
        ), f"expected serialized acquisition; got {labels}"
