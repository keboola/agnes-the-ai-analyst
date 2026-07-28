"""H1-PARTIAL — atomic check-and-flip under MigrationLock. Cancel and
migrator-flip races cannot leave the system with data on TARGET but
instance.yaml on SOURCE."""
from __future__ import annotations

from pathlib import Path
import re


def test_migrator_flip_wraps_check_and_write_in_migration_lock() -> None:
    """Static-text check: the migrator's call site for
    _check_cancel_before_flip + write_backend_state(target_state, ...)
    must be inside a `with MigrationLock()` block."""
    script = Path("scripts/db_state_migrator.py").read_text()
    # Find the call site (the bare two-line sequence is the pre-fix
    # shape; post-fix it must be wrapped). Look for the function body
    # that contains both `_check_cancel_before_flip(` and
    # `write_backend_state(target_state`.
    lines = script.splitlines()
    # The first occurrence of "write_backend_state(target_state" appears
    # inside the _check_cancel_before_flip docstring (a backtick-quoted
    # narrative reference). The real call site uses `url=` kwarg —
    # filter for that to skip the docstring mention.
    flip_idx = next(
        (i for i, l in enumerate(lines)
         if "write_backend_state(target_state" in l and "url=" in l),
        None,
    )
    assert flip_idx is not None, "flip site not found"
    # Walk backwards up to 20 lines looking for `with MigrationLock`.
    window = "\n".join(lines[max(0, flip_idx - 20):flip_idx + 1])
    assert "with MigrationLock" in window, (
        "H1-PARTIAL: write_backend_state(target_state, ...) is not "
        "wrapped in `with MigrationLock()`. The atomic check-and-flip "
        "guarantee requires the lock to be held across "
        "_check_cancel_before_flip + write_backend_state.\n\n"
        f"Window:\n{window}"
    )


def test_cancel_job_revert_wraps_sentinel_and_write_in_migration_lock() -> None:
    """Static-text check: cancel_job's revert path
    (sentinel.touch + write_backend_state(source_backend, ...)) must
    also be inside `with MigrationLock` so the cancel and the
    migrator flip are mutually exclusive."""
    src = Path("app/api/db_state.py").read_text()
    # Match cancel_job through end-of-file or next top-level definition.
    cancel_fn = re.search(
        r"def cancel_job\(.*?(?=^\S|\Z)",
        src, re.MULTILINE | re.DOTALL,
    )
    assert cancel_fn is not None, "cancel_job function not found"
    body = cancel_fn.group(0)
    assert "with MigrationLock" in body, (
        "H1-PARTIAL: cancel_job's revert (sentinel.touch + "
        "write_backend_state) is not wrapped in `with MigrationLock`. "
        "The atomic check-and-flip guarantee requires both sides "
        "(migrator AND cancel handler) to acquire the lock around "
        "the instance.yaml write.\n\nFunction body:\n" + body[:2000]
    )
