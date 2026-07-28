"""Tests for the orphaned ``agnes-pgserver-*`` data-dir reaper.

The reaper must protect concurrent sessions: a dir whose ``postmaster.pid``
names a live PID is never touched, and fresh dirs (possibly mid-initdb,
before ``postmaster.pid`` exists) are left alone regardless of pidfile state.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

from tests.db_pg.pgserver_reaper import reap_orphaned_pgserver_dirs


def _make_dir(tmp_path: Path, name: str, age_seconds: float, pidfile_content: str | None = None) -> Path:
    d = tmp_path / name
    d.mkdir()
    if pidfile_content is not None:
        (d / "postmaster.pid").write_text(pidfile_content)
    old = time.time() - age_seconds
    os.utime(d, (old, old))
    return d


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_reaps_old_dir_without_pidfile(tmp_path):
    d = _make_dir(tmp_path, "agnes-pgserver-dead01", age_seconds=7200)
    removed = reap_orphaned_pgserver_dirs(tmp_path)
    assert removed == [d]
    assert not d.exists()


def test_reaps_old_dir_with_dead_pid(tmp_path):
    d = _make_dir(tmp_path, "agnes-pgserver-dead02", age_seconds=7200, pidfile_content=f"{_dead_pid()}\n{tmp_path}\n")
    removed = reap_orphaned_pgserver_dirs(tmp_path)
    assert removed == [d]
    assert not d.exists()


def test_keeps_old_dir_with_live_pid(tmp_path):
    d = _make_dir(tmp_path, "agnes-pgserver-live01", age_seconds=7200, pidfile_content=f"{os.getpid()}\n{tmp_path}\n")
    removed = reap_orphaned_pgserver_dirs(tmp_path)
    assert removed == []
    assert d.exists()


def test_keeps_fresh_dir_without_pidfile(tmp_path):
    # A fresh dir may belong to a concurrent session still running initdb.
    d = _make_dir(tmp_path, "agnes-pgserver-fresh1", age_seconds=0)
    removed = reap_orphaned_pgserver_dirs(tmp_path)
    assert removed == []
    assert d.exists()


def test_keeps_old_dir_with_unparsable_pidfile(tmp_path):
    d = _make_dir(tmp_path, "agnes-pgserver-junk01", age_seconds=7200, pidfile_content="not-a-pid\n")
    removed = reap_orphaned_pgserver_dirs(tmp_path)
    assert removed == []
    assert d.exists()


def test_ignores_unrelated_dirs(tmp_path):
    d = _make_dir(tmp_path, "pytest-of-somebody", age_seconds=7200)
    removed = reap_orphaned_pgserver_dirs(tmp_path)
    assert removed == []
    assert d.exists()
