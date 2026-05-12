"""Startup sweep of stale `.parquet.lock` files (Issue #260).

The lock acquire path already reclaims stale locks lazily on the next
materialize attempt, but a dedicated startup sweep removes zombies
sitting next to parquets without waiting for the next sync.
"""

from pathlib import Path


def test_sweep_returns_zero_when_no_data_dir(tmp_path: Path):
    """Missing directory must not raise — operators starting on a fresh
    VM before any extracts exist should see a clean startup."""
    from connectors.bigquery.extractor import sweep_stale_parquet_locks
    assert sweep_stale_parquet_locks(tmp_path / "nonexistent") == 0


def test_sweep_keeps_fresh_locks(tmp_path: Path, monkeypatch):
    """A lock file mtime'd within the TTL stays put."""
    from connectors.bigquery.extractor import sweep_stale_parquet_locks
    extracts = tmp_path / "extracts" / "bigquery" / "data"
    extracts.mkdir(parents=True)
    lock = extracts / "t.parquet.lock"
    lock.touch()
    monkeypatch.setenv("AGNES_INSTANCE_CONFIG", str(tmp_path / "no.yaml"))
    n = sweep_stale_parquet_locks(tmp_path / "extracts")
    assert n == 0
    assert lock.exists()


def test_sweep_removes_stale_locks(tmp_path: Path):
    """A lock mtime'd > TTL ago gets unlinked."""
    import os
    import time
    from connectors.bigquery.extractor import sweep_stale_parquet_locks, _get_lock_ttl_seconds
    extracts = tmp_path / "extracts" / "bigquery" / "data"
    extracts.mkdir(parents=True)
    lock = extracts / "old.parquet.lock"
    lock.touch()
    # Backdate mtime to 2× TTL ago.
    ttl = _get_lock_ttl_seconds()
    ancient = time.time() - (ttl * 2)
    os.utime(lock, (ancient, ancient))
    n = sweep_stale_parquet_locks(tmp_path / "extracts")
    assert n == 1
    assert not lock.exists()


def test_sweep_handles_multiple_sources(tmp_path: Path):
    """Recursive search covers bq + keboola + jira layouts under one root."""
    import os, time
    from connectors.bigquery.extractor import sweep_stale_parquet_locks, _get_lock_ttl_seconds
    for source in ("bigquery", "keboola", "jira"):
        d = tmp_path / "extracts" / source / "data"
        d.mkdir(parents=True)
        lock = d / "t.parquet.lock"
        lock.touch()
        ancient = time.time() - (_get_lock_ttl_seconds() * 2)
        os.utime(lock, (ancient, ancient))
    n = sweep_stale_parquet_locks(tmp_path / "extracts")
    assert n == 3
