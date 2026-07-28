"""Startup cleanup of orphaned DuckDB spill files.

DuckDB does not remove ``duckdb_temp_storage_*`` spill files when the
process dies hard (SIGKILL, crash, container stop timeout) — observed
in production as multi-GB dead weight accumulating in
``{STATE_DIR}/duckdb-tmp`` across incidents. Only the app process ever
opens DuckDB against this directory (the scheduler is a pure HTTP
clock), so at app startup — before the first connection exists — every
matching file is by definition an orphan.
"""

import os
import time

from src.db import cleanup_orphaned_temp_files


def _age(path, seconds):
    t = time.time() - seconds
    os.utime(path, times=(t, t))


def test_removes_orphaned_spill_files(tmp_path):
    d = tmp_path / "duckdb-tmp"
    d.mkdir()
    for name, size in [("duckdb_temp_storage_DEFAULT-0.tmp", 1024), ("duckdb_temp_storage_S32K-1.tmp", 2048)]:
        f = d / name
        f.write_bytes(b"x" * size)
        _age(f, 3600)

    removed, freed = cleanup_orphaned_temp_files(d)

    assert removed == 2
    assert freed == 3072
    assert list(d.iterdir()) == []


def test_keeps_fresh_spill_files_of_a_live_process(tmp_path):
    """The age margin makes call-site ordering irrelevant: a file the
    booting (or any live) process just spilled must survive the sweep."""
    d = tmp_path / "duckdb-tmp"
    d.mkdir()
    fresh = d / "duckdb_temp_storage_DEFAULT-9.tmp"
    fresh.write_bytes(b"z" * 512)  # mtime = now

    removed, freed = cleanup_orphaned_temp_files(d)

    assert (removed, freed) == (0, 0)
    assert fresh.exists()


def test_leaves_non_spill_files_alone(tmp_path):
    d = tmp_path / "duckdb-tmp"
    d.mkdir()
    keeper = d / "not-a-spill-file.txt"
    keeper.write_text("keep me")

    removed, freed = cleanup_orphaned_temp_files(d)

    assert removed == 0
    assert freed == 0
    assert keeper.exists()


def test_missing_directory_is_a_noop(tmp_path):
    removed, freed = cleanup_orphaned_temp_files(tmp_path / "does-not-exist")
    assert (removed, freed) == (0, 0)


def test_unremovable_file_does_not_raise(tmp_path, monkeypatch):
    d = tmp_path / "duckdb-tmp"
    d.mkdir()
    f = d / "duckdb_temp_storage_DEFAULT-0.tmp"
    f.write_bytes(b"x")
    _age(f, 3600)

    import pathlib

    def boom(self):
        raise PermissionError("nope")

    monkeypatch.setattr(pathlib.Path, "unlink", boom)
    removed, freed = cleanup_orphaned_temp_files(d)  # must not raise
    assert removed == 0
