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


# ---------------------------------------------------------------------------
# Orphaned postmasters
#
# A run killed hard (SIGKILL, OOM, disk full) leaves the postmaster running,
# reparented to init. Its pidfile therefore names a *live* PID forever, so the
# liveness check above skips that dir on every future run: the ~400 MB leaks
# permanently and compounds with each interrupted run. Distinguish an orphan
# (parented to init, no session owns it) from a concurrent session's live
# server, and reclaim only the former.
# ---------------------------------------------------------------------------


def _write_owner(d: Path, pid: int) -> None:
    (d / "agnes-owner.pid").write_text(f"{pid}\n")


def test_reaps_dir_whose_owning_session_is_dead(tmp_path):
    """The postmaster is alive (pgserver detaches it, so it survives its
    session and is never reparented-detectable), but the pytest session that
    created the dir is gone. That is the orphan case."""
    killed = []
    d = _make_dir(
        tmp_path, "agnes-pgserver-orph01", age_seconds=7200,
        pidfile_content=f"{os.getpid()}\n{tmp_path}\n",
    )
    _write_owner(d, _dead_pid())

    removed = reap_orphaned_pgserver_dirs(tmp_path, kill_fn=killed.append)

    assert removed == [d]
    assert not d.exists()
    assert killed == [os.getpid()], "the orphaned postmaster must be stopped"


def test_keeps_dir_whose_owning_session_is_alive(tmp_path):
    """A peer session's postmaster is ALSO detached (ppid == 1). Only the
    owner-session liveness distinguishes it from an orphan, so this is the
    regression guard against killing a live peer's database."""
    killed = []
    d = _make_dir(
        tmp_path, "agnes-pgserver-peer01", age_seconds=7200,
        pidfile_content=f"{os.getpid()}\n{tmp_path}\n",
    )
    _write_owner(d, os.getpid())  # this very process = a live owner

    removed = reap_orphaned_pgserver_dirs(tmp_path, kill_fn=killed.append)

    assert removed == []
    assert d.exists()
    assert killed == [], "never signal a live session's server"


def test_live_postmaster_without_owner_file_is_left_alone(tmp_path):
    """Dirs from before the owner file existed: stay conservative, never
    guess from the postmaster PID alone."""
    killed = []
    d = _make_dir(
        tmp_path, "agnes-pgserver-legacy", age_seconds=7200,
        pidfile_content=f"{os.getpid()}\n{tmp_path}\n",
    )

    removed = reap_orphaned_pgserver_dirs(tmp_path, kill_fn=killed.append)

    assert removed == []
    assert d.exists()
    assert killed == []


def test_kill_orphans_disabled_preserves_legacy_behaviour(tmp_path):
    killed = []
    d = _make_dir(
        tmp_path, "agnes-pgserver-orph02", age_seconds=7200,
        pidfile_content=f"{os.getpid()}\n{tmp_path}\n",
    )
    _write_owner(d, _dead_pid())

    removed = reap_orphaned_pgserver_dirs(tmp_path, kill_orphans=False, kill_fn=killed.append)

    assert removed == []
    assert d.exists()
    assert killed == []


def test_dead_pidfile_is_reaped_regardless_of_age(tmp_path):
    """The min-age guard exists for dirs mid-initdb, before a pidfile exists.
    A pidfile naming a dead PID is unambiguous, so age is irrelevant."""
    d = _make_dir(
        tmp_path, "agnes-pgserver-fresh2", age_seconds=0,
        pidfile_content=f"{_dead_pid()}\n{tmp_path}\n",
    )

    removed = reap_orphaned_pgserver_dirs(tmp_path)

    assert removed == [d]
    assert not d.exists()
