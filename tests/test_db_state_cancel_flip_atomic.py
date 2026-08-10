"""H1-PARTIAL — atomic check-and-flip under MigrationLock. Cancel and
migrator-flip races cannot leave the system with data on TARGET but
instance.yaml on SOURCE."""
from __future__ import annotations

from pathlib import Path
import re


def test_migrator_flip_wraps_check_and_write_in_migration_lock() -> None:
    """The cancel re-check, the flip and its read-back all run under one lock.

    Follows the indirection rather than a text window: the three steps live in
    a `_flip_and_verify` helper, so a purely lexical "is there a `with
    MigrationLock` above this line" check reports a false failure the moment
    the body moves into a function. What the invariant actually requires is
    that the helper hold all three AND that no call site invoke it outside the
    lock — the read-back especially, since a cancel landing after the write
    would otherwise be reported as a failure instead of a cancellation.
    """
    script = Path("scripts/db_state_migrator.py").read_text()
    lines = script.splitlines()

    fn_start = next(
        (i for i, ln in enumerate(lines) if ln.strip().startswith("def _flip_and_verify(")),
        None,
    )
    assert fn_start is not None, (
        "H1-PARTIAL: expected the check + flip + read-back to live in a "
        "`_flip_and_verify` helper; if it was inlined again, re-point this guard"
    )
    # Body ends at the next line indented no deeper than the `def`.
    indent = len(lines[fn_start]) - len(lines[fn_start].lstrip())
    fn_end = fn_start + 1
    while fn_end < len(lines) and (not lines[fn_end].strip() or (len(lines[fn_end]) - len(lines[fn_end].lstrip())) > indent):
        fn_end += 1
    body = "\n".join(lines[fn_start:fn_end])

    for needed in ("_check_cancel_before_flip(", "write_backend_state(target_state", "read_backend_state()"):
        assert needed in body, (
            f"H1-PARTIAL: `{needed}` is not inside `_flip_and_verify`. All three must share "
            "the lock — splitting them is how a raced cancel becomes a reported failure."
        )

    call_sites = [
        i
        for i, ln in enumerate(lines)
        if "_flip_and_verify()" in ln and not ln.strip().startswith("def ")
    ]
    assert call_sites, "no call site for _flip_and_verify"
    for i in call_sites:
        window = "\n".join(lines[max(0, i - 3):i + 1])
        assert "with MigrationLock" in window, (
            "H1-PARTIAL: `_flip_and_verify()` is invoked outside `with MigrationLock()` "
            f"at line {i + 1}. The atomic check-and-flip guarantee requires the lock.\n\n"
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
