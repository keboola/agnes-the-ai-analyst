"""Pre-flight guards run once at pytest session start.

The full suite writes 20-50 GB into pytest's basetemp (see the
``tmp_path_retention_count`` note in ``pytest.ini``) and boots a real
postmaster per session. Two cheap checks up front turn the two ways that
has gone wrong into an immediate, actionable message instead of a failure
90% into a 45-minute run:

* **Overlapping sessions.** ``tmp_path_retention_count = 1`` reclaims the
  previous session's tree at the *start* of the next run, which assumes runs
  are serial. Two concurrent sessions each retain their own tree and neither
  sweeps the other, so peak disk doubles and both slow to a crawl competing
  for cores.
* **Starting with no headroom.** Filling the disk mid-run kills pytest with
  an ``INTERNALERROR`` traceback and no test results at all, which reads as a
  code failure rather than an environment one.

Both guards are advisory and fail open. A guard that cannot measure must
never be the reason a suite doesn't run.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional

# Headroom a full run needs. Chosen from observed usage (20-50 GB per the
# pytest.ini note) minus the reclaim the startup sweep performs.
DEFAULT_MIN_FREE_GB = 15

_BYTES_PER_GB = 1024**3


def free_bytes(path: Path) -> int:
    """Bytes available to an unprivileged caller on ``path``'s filesystem."""
    st = os.statvfs(str(path))
    return st.f_bavail * st.f_frsize


def disk_preflight(
    path: Path,
    *,
    min_free_gb: int = DEFAULT_MIN_FREE_GB,
    free_bytes_fn: Callable[[Path], int] = free_bytes,
) -> Optional[str]:
    """Return an actionable message when ``path`` has less than
    ``min_free_gb`` free, else ``None``.

    Fails open: if free space can't be determined, return ``None`` rather
    than block the run.
    """
    try:
        avail = free_bytes_fn(path)
    except OSError:
        return None

    avail_gb = avail / _BYTES_PER_GB
    if avail_gb >= min_free_gb:
        return None

    return (
        f"Low disk: {avail_gb:.1f} GB free on {path}, this suite wants "
        f"{min_free_gb} GB. A full run writes 20-50 GB of fixtures and will "
        f"die mid-run with an INTERNALERROR rather than a test result. "
        f"Reclaim space first: stale 'pytest-of-*' trees and orphaned "
        f"'agnes-pgserver-*' data dirs under the temp root are the usual "
        f"culprits."
    )


def write_session_lock(lock_path: Path, pid: Optional[int] = None) -> None:
    """Record ``pid`` (default: this process) as the owner of the suite lock."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(f"{os.getpid() if pid is None else pid}\n")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists, just not ours to signal
    return True


def concurrent_session_holder(
    lock_path: Path,
    *,
    pid_alive_fn: Callable[[int], bool] = _pid_alive,
) -> Optional[int]:
    """PID of another **live** suite session holding ``lock_path``, else ``None``.

    ``None`` when the lock is absent, stale (owner died), owned by this very
    process, or unparsable. Never guess from a malformed lock, a false
    positive would block a legitimate run.
    """
    try:
        raw = lock_path.read_text().splitlines()[0].strip()
        pid = int(raw)
    except (OSError, ValueError, IndexError):
        return None

    if pid == os.getpid():
        return None
    return pid if pid_alive_fn(pid) else None
