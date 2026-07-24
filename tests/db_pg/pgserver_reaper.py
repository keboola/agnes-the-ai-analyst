"""Reaper for orphaned ``agnes-pgserver-*`` Postgres data directories.

``_start_pgserver`` in ``tests/db_pg/conftest.py`` creates its data dir via
``tempfile.mkdtemp(prefix="agnes-pgserver-")`` and removes it in a ``finally``
block at session end. A hard-killed run (SIGKILL, OOM kill, crash after the
disk fills up) never reaches that ``finally``, leaving a ~300 MB data dir
behind — and, because the fixture uses ``cleanup_mode=None``, possibly a
still-running detached postmaster that pgserver itself will never stop.

Deliberately conservative — a skipped dir costs ~300 MB of disk until the
next run; a wrongly reaped dir kills a concurrent worktree session's live
Postgres:

- only ``agnes-pgserver-*`` dirs directly under the given temp root;
- a dir with no ``postmaster.pid`` yet is kept until ``min_age_seconds``, in
  case a concurrent session is still running initdb;
- a dir whose ``postmaster.pid`` names a live postmaster is reclaimed only
  when ``OWNER_FILE`` proves the pytest session that created it is gone.
  The postmaster's own PID cannot decide this: pgserver detaches it, so it
  reads ``ppid == 1`` while its session is alive AND after that session
  dies. Killing on liveness or parentage destroys a peer session's database;
- a dir with no owner file (created before this convention) is never killed;
- an unreadable or unparsable ``postmaster.pid`` keeps the dir (never guess).
"""

from __future__ import annotations

import os
import shutil
import signal
import time
from pathlib import Path
from typing import Callable

MIN_AGE_SECONDS = 3600


OWNER_FILE = "agnes-owner.pid"


def _owning_session_is_dead(pgdata: Path) -> bool:
    """True when the pytest session that created ``pgdata`` is gone.

    The postmaster's own PID cannot answer this. ``pgserver`` starts a
    **detached** postmaster: it has ``ppid == 1`` from birth, while its
    session is alive and using it, and it keeps running after that session
    dies. So neither liveness nor parentage of the postmaster distinguishes
    "orphan" from "a peer session's database", and killing on either signal
    destroys live peers' data.

    ``_start_pgserver`` therefore records its own PID in ``OWNER_FILE`` when
    it creates the dir. That PID is the only reliable answer.

    Fails closed: no owner file, or anything unreadable, means "not an
    orphan". The cost of a wrong "yes" is killing a live session's database;
    the cost of a wrong "no" is ~400 MB until someone cleans up.
    """
    try:
        owner = int((pgdata / OWNER_FILE).read_text().splitlines()[0].strip())
    except (OSError, ValueError, IndexError):
        return False
    return not _pid_alive(owner)


def _terminate(pid: int) -> None:
    """Stop an orphaned postmaster. SIGINT is Postgres' *fast shutdown*;
    SIGTERM is *smart* shutdown, which waits for clients to disconnect and so
    hangs on an orphan that still has open backends."""
    try:
        os.kill(pid, signal.SIGINT)
    except OSError:
        return
    for _ in range(20):
        time.sleep(0.1)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except OSError:
            return
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # PermissionError etc. — the process exists, just not ours
    return True


def _postmaster_pid(pgdata: Path) -> int | None:
    """PID from the first line of ``postmaster.pid``, or None if unreadable."""
    try:
        return int((pgdata / "postmaster.pid").read_text().splitlines()[0].strip())
    except (OSError, ValueError, IndexError):
        return None


def reap_orphaned_pgserver_dirs(
    tmp_root: Path,
    *,
    min_age_seconds: int = MIN_AGE_SECONDS,
    kill_orphans: bool = True,
    is_orphan_fn: Callable[[Path], bool] = _owning_session_is_dead,
    kill_fn: Callable[[int], None] = _terminate,
) -> list[Path]:
    """Remove orphaned pgserver data dirs under ``tmp_root``; return removed paths.

    With ``kill_orphans`` (default), a dir whose owning pytest session is gone
    is reclaimed and its detached postmaster stopped. Without it, any live
    postmaster keeps its dir, which leaks permanently once the owner is gone
    because a detached postmaster's PID stays live forever.
    """
    removed: list[Path] = []
    now = time.time()
    for d in tmp_root.glob("agnes-pgserver-*"):
        try:
            if not d.is_dir():
                continue
            has_pidfile = (d / "postmaster.pid").exists()
            # The min-age guard protects a dir that may be mid-initdb, before
            # its pidfile exists. Once there IS a pidfile its state is
            # unambiguous, so age stops being informative.
            if not has_pidfile and now - d.stat().st_mtime < min_age_seconds:
                continue
            if has_pidfile:
                pid = _postmaster_pid(d)
                if pid is None:
                    continue  # unparsable, never guess
                if _pid_alive(pid):
                    if not (kill_orphans and is_orphan_fn(d)):
                        continue  # a peer session's live server
                    kill_fn(pid)
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d)
        except OSError:
            continue  # racing another session's cleanup — never fail the suite
    return removed
