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
- only dirs older than ``min_age_seconds`` — a fresh dir may belong to a
  concurrent session still running initdb, before ``postmaster.pid`` exists;
- a dir whose ``postmaster.pid`` names a live PID is never touched (PID
  reuse can therefore keep a dead dir around, which is the cheap direction);
- an unreadable or unparsable ``postmaster.pid`` keeps the dir (never guess).
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

MIN_AGE_SECONDS = 3600


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


def reap_orphaned_pgserver_dirs(tmp_root: Path, *, min_age_seconds: int = MIN_AGE_SECONDS) -> list[Path]:
    """Remove orphaned pgserver data dirs under ``tmp_root``; return removed paths."""
    removed: list[Path] = []
    now = time.time()
    for d in tmp_root.glob("agnes-pgserver-*"):
        try:
            if not d.is_dir():
                continue
            if now - d.stat().st_mtime < min_age_seconds:
                continue
            if (d / "postmaster.pid").exists():
                pid = _postmaster_pid(d)
                if pid is None or _pid_alive(pid):
                    continue
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d)
        except OSError:
            continue  # racing another session's cleanup — never fail the suite
    return removed
