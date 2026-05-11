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
def acquire_or_skip(workspace: Path) -> Iterator[FileLock | None]:
    """Yield the held lock, or ``None`` if the lock can't be acquired.

    Non-blocking (``timeout=0``). Two ways acquisition can fail, both
    treated the same way (yield ``None`` so the caller can ``return`` /
    ``sys.exit(0)`` quietly):

    - ``filelock.Timeout`` — another push is already running. Expected
      when multiple SessionEnd hooks fire simultaneously after the user
      closes several Claude Code sessions at once: exactly one acquires
      the lock and runs, the rest no-op.
    - ``OSError`` — the lock file can't be created or opened (read-only
      filesystem, ``.claude/`` not writable, disk full, hardware I/O
      error). Rare; when it happens the operator's environment has
      bigger problems than missing session uploads. We swallow it so
      ``agnes push`` exits cleanly instead of dumping an opaque
      traceback to stderr or, in the SessionEnd hook context, crashing
      silently under ``|| true``.
    """
    lock = FileLock(str(lock_path(workspace)))
    try:
        with lock.acquire(timeout=0):
            yield lock
    except (Timeout, OSError):
        yield None
