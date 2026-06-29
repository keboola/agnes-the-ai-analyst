"""Cross-platform single-instance lock for `agnes push`.

Wraps :class:`filelock.FileLock` (which delegates to ``fcntl.flock`` on POSIX
and ``msvcrt.locking`` on Windows) into a context manager that returns
``None`` when another push is already running. Callers use it via:

.. code-block:: python

    with acquire_or_skip(workspace) as lock:
        if lock is None:
            return  # silent exit — another push holds the lock
        do_push()

The OS releases the lock automatically when the holding process exits
(including crashes), so we do NOT track PIDs or stale-lock ages. The lock
file persists between runs but stays empty — :class:`filelock` only uses
it for the kernel-level lock handle.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from filelock import FileLock, Timeout


_LOCK_FILENAME = "agnes-push.lock"


def lock_path(workspace: Path) -> Path:
    """Resolve ``<workspace>/.claude/agnes-push.lock``."""
    claude_dir = workspace / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    return claude_dir / _LOCK_FILENAME


@contextmanager
def acquire_path_or_skip(lock_file: Path) -> Iterator[FileLock | None]:
    """Single-instance lock at an arbitrary ``lock_file`` path.

    Generalises :func:`acquire_or_skip` to any lock location (e.g.
    ``~/.config/agnes/update.lock`` for ``agnes update``, not just the
    per-workspace push lock). Non-blocking (``timeout=0``). Yields the
    held lock, or ``None`` when it can't be acquired — same two failure
    modes, both swallowed so the caller can ``return`` / ``sys.exit(0)``
    quietly:

    - ``filelock.Timeout`` — another holder is already running. Exactly
      one process acquires and runs; the rest no-op.
    - ``OSError`` — the lock file can't be created/opened (read-only fs,
      parent dir not writable, disk full). Rare; swallowed so we exit
      cleanly rather than dump a traceback.

    The OS releases the lock automatically when the holding process exits
    (including crashes), so there are no stale-lock / PID-tracking
    concerns — the next run always proceeds.
    """
    try:
        lock_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        yield None
        return
    lock = FileLock(str(lock_file))
    try:
        with lock.acquire(timeout=0):
            yield lock
    except (Timeout, OSError):
        yield None


@contextmanager
def acquire_or_skip(workspace: Path) -> Iterator[FileLock | None]:
    """Per-workspace push lock at ``<workspace>/.claude/agnes-push.lock``.

    Thin wrapper over :func:`acquire_path_or_skip` for the ``agnes push``
    SessionEnd-hook path: when multiple sessions close at once, exactly one
    acquires the lock and runs, the rest no-op.
    """
    with acquire_path_or_skip(lock_path(workspace)) as lock:
        yield lock
