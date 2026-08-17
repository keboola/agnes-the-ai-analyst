"""Tests for the orphaned ``agnes-pgserver-*`` data-dir reaper.

The reaper must protect concurrent sessions: a dir whose ``postmaster.pid``
names a live PID is only touched when the ``agnes-owner.pid`` sentinel names
a DEAD owning pytest process AND the live PID verifiably is a postmaster on
that data dir (#1362). Fresh dirs (possibly mid-initdb, before
``postmaster.pid`` exists) are left alone regardless of pidfile state.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import tests.db_pg.pgserver_reaper as reaper_mod
from tests.db_pg.pgserver_reaper import (
    OWNER_SENTINEL,
    _is_postmaster_for,
    reap_orphaned_pgserver_dirs,
)


def _make_dir(
    tmp_path: Path,
    name: str,
    age_seconds: float,
    pidfile_content: str | None = None,
    owner_pid: int | None = None,
) -> Path:
    d = tmp_path / name
    d.mkdir()
    if pidfile_content is not None:
        (d / "postmaster.pid").write_text(pidfile_content)
    if owner_pid is not None:
        (d / OWNER_SENTINEL).write_text(str(owner_pid))
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


# ---------------------------------------------------------------------------
# Live-orphan reaping via the owner sentinel (#1362)
# ---------------------------------------------------------------------------


def _spawn_sleeper() -> subprocess.Popen:
    """A live process standing in for a leaked postmaster."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


def test_keeps_live_pid_when_owner_is_alive(tmp_path):
    """A live postmaster whose owning pytest still runs is a concurrent
    session — never touched, even though every other reap condition holds."""
    proc = _spawn_sleeper()
    try:
        d = _make_dir(
            tmp_path,
            "agnes-pgserver-live02",
            age_seconds=7200,
            pidfile_content=f"{proc.pid}\n{tmp_path}\n",
            owner_pid=os.getpid(),
        )
        assert reap_orphaned_pgserver_dirs(tmp_path) == []
        assert d.exists()
        assert proc.poll() is None  # and it was not signaled
    finally:
        proc.kill()
        proc.wait()


def test_keeps_live_pid_when_identity_check_fails(tmp_path):
    """Owner dead but the live PID is not verifiably a postmaster on this
    dir (PID reuse) — never signal a foreign process. The sleeper's real
    cmdline is python, so no monkeypatching is needed for the negative."""
    proc = _spawn_sleeper()
    try:
        d = _make_dir(
            tmp_path,
            "agnes-pgserver-reuse1",
            age_seconds=7200,
            pidfile_content=f"{proc.pid}\n{tmp_path}\n",
            owner_pid=_dead_pid(),
        )
        assert reap_orphaned_pgserver_dirs(tmp_path) == []
        assert d.exists()
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait()


def test_stops_and_reaps_live_orphan(tmp_path, monkeypatch):
    """Owner dead + identity confirmed → the postmaster is stopped (SIGTERM)
    and the dir removed. Identity is monkeypatched True because the stand-in
    process is python, not postgres."""
    proc = _spawn_sleeper()
    try:
        d = _make_dir(
            tmp_path,
            "agnes-pgserver-orphan",
            age_seconds=7200,
            pidfile_content=f"{proc.pid}\n{tmp_path}\n",
            owner_pid=_dead_pid(),
        )
        monkeypatch.setattr(reaper_mod, "_is_postmaster_for", lambda pid, pgdata: True)
        removed = reap_orphaned_pgserver_dirs(tmp_path)
        assert removed == [d]
        assert not d.exists()
        # -15 = died by SIGTERM; 0 is what Popen.wait() reports when the
        # reaper's psutil wait() already reaped the child before us.
        assert proc.wait(timeout=5) in (0, -15)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_keeps_live_pid_without_sentinel(tmp_path):
    """Pre-sentinel dirs (created before #1362) keep the old conservative
    behavior: live PID → skip."""
    d = _make_dir(
        tmp_path,
        "agnes-pgserver-old001",
        age_seconds=7200,
        pidfile_content=f"{os.getpid()}\n{tmp_path}\n",
    )
    assert reap_orphaned_pgserver_dirs(tmp_path) == []
    assert d.exists()


def test_is_postmaster_for_rejects_dead_and_foreign_pids(tmp_path):
    assert _is_postmaster_for(_dead_pid(), tmp_path) is False
    # A live python process is not a postmaster for any data dir.
    proc = _spawn_sleeper()
    try:
        assert _is_postmaster_for(proc.pid, tmp_path) is False
    finally:
        proc.kill()
        proc.wait()
