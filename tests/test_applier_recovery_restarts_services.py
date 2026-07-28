"""B3-NEW — _recover_stuck_jobs must restart agnes-app-1 and
agnes-scheduler-1 after marking a stuck job failed, otherwise the
services stay DOWN indefinitely after a SIGKILL/OOM mid-tick."""
from __future__ import annotations

from pathlib import Path
import re


def test_recover_stuck_jobs_restarts_app_and_scheduler() -> None:
    """The recovery function must include a compose 'up -d' or
    equivalent restart call covering app + scheduler. Static-text
    check matches the pattern used by other applier guards (H5-NEW,
    H8-NEW)."""
    script = Path("scripts/ops/agnes-state-applier.sh").read_text()

    # Find the function body.
    fn_match = re.search(
        r"_recover_stuck_jobs\(\)\s*\{(.*?)^\}",
        script, re.MULTILINE | re.DOTALL,
    )
    assert fn_match is not None, "function _recover_stuck_jobs not found"
    body = fn_match.group(1)

    # The body must include a restart of the app and scheduler. We
    # accept any of these forms:
    #   docker start agnes-app-1 agnes-scheduler-1
    #   docker compose up -d app scheduler
    #   docker compose up -d
    #   dc up -d ... app scheduler  (script-local alias)
    has_restart = (
        ("docker start agnes-app-1" in body and "agnes-scheduler-1" in body)
        or ("docker compose up -d" in body
            and ("app" in body and "scheduler" in body))
        or ("dc up -d" in body
            and ("app" in body and "scheduler" in body))
    )
    assert has_restart, (
        "B3-NEW: _recover_stuck_jobs revert instance.yaml but does NOT "
        "restart app + scheduler. After a SIGKILL/OOM mid-migrator the "
        "services stay DOWN until the next successful migration or a "
        "manual restart. Recovery must include `dc up -d --no-deps "
        "--force-recreate app scheduler` (or equivalent) "
        "after reverting instance.yaml.\n\n"
        f"Function body:\n{body}"
    )


def test_recovery_restart_is_after_revert() -> None:
    """The restart MUST happen AFTER the instance.yaml revert, not
    before — otherwise the app boots against the *_in_progress state
    and crashes during init."""
    script = Path("scripts/ops/agnes-state-applier.sh").read_text()
    fn_match = re.search(
        r"_recover_stuck_jobs\(\)\s*\{(.*?)^\}",
        script, re.MULTILINE | re.DOTALL,
    )
    body = fn_match.group(1)
    lines = body.splitlines()
    write_yaml_line = next(
        (i for i, l in enumerate(lines) if "write_instance_yaml" in l),
        None,
    )
    restart_line = next(
        (i for i, l in enumerate(lines)
         if "docker start" in l or "docker compose up" in l or "dc up -d" in l),
        None,
    )
    assert write_yaml_line is not None, (
        "write_instance_yaml line not found in _recover_stuck_jobs"
    )
    assert restart_line is not None, (
        "restart line not found in _recover_stuck_jobs"
    )
    assert restart_line > write_yaml_line, (
        f"B3-NEW: app+scheduler restart (line {restart_line+1}) must "
        f"be AFTER write_instance_yaml revert (line {write_yaml_line+1})"
    )
