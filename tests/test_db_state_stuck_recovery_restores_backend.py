"""H5-NEW — stuck-running recovery restores database.backend from
the in_progress placeholder back to source_backend.

B5's heartbeat-based recovery marks a stale-.alive job failed but
must also call write_instance_yaml to restore the overlay from
``*_in_progress`` back to ``source_backend``. Without the restore,
the next migration retry reads ``*_in_progress`` as the current
backend, the migrator CLI receives ``source_backend='side_car_in_progress'``,
and rejects — state machine wedged until an operator manually edits
instance.yaml.
"""
from __future__ import annotations

from pathlib import Path


def test_recover_stuck_jobs_function_exists() -> None:
    """H5-NEW: stuck-running recovery must be extracted into a
    ``_recover_stuck_jobs`` function so it can be tested in isolation
    and called cleanly from the main tick loop.
    """
    script = Path("scripts/ops/agnes-state-applier.sh").read_text()
    assert "_recover_stuck_jobs()" in script, (
        "H5-NEW: _recover_stuck_jobs() function must be defined in "
        "agnes-state-applier.sh. Pre-fix, recovery was inlined into the "
        "main tick loop with no write_instance_yaml restore call."
    )


def test_recover_stuck_jobs_calls_write_instance_yaml() -> None:
    """H5-NEW: _recover_stuck_jobs must call write_instance_yaml with
    source_backend (and source_url) to restore instance.yaml from the
    *_in_progress placeholder.

    Without this call, a host crash mid-migration leaves instance.yaml
    at ``backend: side_car_in_progress`` (or ``cloud_in_progress``).
    The next migration retry reads that label as the current backend
    and the migrator rejects with ``source_backend='side_car_in_progress'``.
    """
    script = Path("scripts/ops/agnes-state-applier.sh").read_text()
    lines = script.splitlines()

    # Find _recover_stuck_jobs function body.
    start = None
    for i, line in enumerate(lines):
        if line.strip().startswith("_recover_stuck_jobs()"):
            start = i
            break
    assert start is not None, "_recover_stuck_jobs() function not found"

    # Find the closing brace of the function.
    depth = 0
    end = start
    for i, line in enumerate(lines[start:], start):
        stripped = line.strip()
        if "{" in stripped:
            depth += stripped.count("{") - stripped.count("}")
        elif "}" in stripped:
            depth -= stripped.count("}") - stripped.count("{")
        if depth == 0 and i > start:
            end = i
            break

    body = "\n".join(lines[start : end + 1])

    # The function must call write_instance_yaml.
    assert "write_instance_yaml" in body, (
        "H5-NEW: _recover_stuck_jobs() must call write_instance_yaml "
        "to restore instance.yaml from *_in_progress to source_backend. "
        f"Function body:\n{body}"
    )

    # The call must include source_backend — not a hardcoded string.
    write_lines = [
        l for l in body.splitlines()
        if "write_instance_yaml" in l and not l.lstrip().startswith("#")
    ]
    assert write_lines, (
        "H5-NEW: write_instance_yaml call not found in _recover_stuck_jobs body"
    )
    # At least one call must reference source_backend (local var).
    assert any("source_backend" in l for l in write_lines), (
        "H5-NEW: write_instance_yaml call in _recover_stuck_jobs must pass "
        "source_backend. Current calls:\n  " + "\n  ".join(write_lines)
    )


def test_recover_stuck_jobs_passes_source_url() -> None:
    """H5-NEW: the write_instance_yaml call in _recover_stuck_jobs must
    also pass source_url (second argument) so that cloud-sourced
    migrations (side_car → cloud, cloud → side_car) restore the URL.

    Empty source_url is correct for duckdb sources — write_instance_yaml
    already handles that by dropping the url key. What matters is that
    the variable is passed, not omitted, so the CALLER decides (not the
    function's missing-arg default).
    """
    script = Path("scripts/ops/agnes-state-applier.sh").read_text()
    lines = script.splitlines()

    start = None
    for i, line in enumerate(lines):
        if line.strip().startswith("_recover_stuck_jobs()"):
            start = i
            break
    assert start is not None, "_recover_stuck_jobs() function not found"

    depth = 0
    end = start
    for i, line in enumerate(lines[start:], start):
        stripped = line.strip()
        if "{" in stripped:
            depth += stripped.count("{") - stripped.count("}")
        elif "}" in stripped:
            depth -= stripped.count("}") - stripped.count("{")
        if depth == 0 and i > start:
            end = i
            break

    body = "\n".join(lines[start : end + 1])

    write_lines = [
        l for l in body.splitlines()
        if "write_instance_yaml" in l and not l.lstrip().startswith("#")
    ]
    assert write_lines, "write_instance_yaml not called in _recover_stuck_jobs"

    offending = [l for l in write_lines if "source_url" not in l]
    assert not offending, (
        "H5-NEW: write_instance_yaml calls in _recover_stuck_jobs must also "
        "pass source_url; otherwise cloud-source migrations that crash mid-flight "
        "are restored with backend=cloud but no url, causing the next boot to "
        "fail with 'Postgres URL unset'. Offending calls:\n  "
        + "\n  ".join(offending)
    )


def test_recover_stuck_jobs_reads_source_backend_before_update() -> None:
    """H5-NEW: source_backend must be extracted from the job JSON
    BEFORE update_job is called (which rewrites the file).

    The sequence must be:
      1. read source_backend from job JSON
      2. call update_job (marks status=failed, overwrites the file)
      3. call write_instance_yaml with the pre-read source_backend

    If source_backend is read after update_job, it still works because
    update_job does not wipe source_backend. However, the canonical
    defensive pattern reads it first to be resilient against any future
    update_job changes that might drop that field.
    """
    script = Path("scripts/ops/agnes-state-applier.sh").read_text()
    lines = script.splitlines()

    start = None
    for i, line in enumerate(lines):
        if line.strip().startswith("_recover_stuck_jobs()"):
            start = i
            break
    assert start is not None, "_recover_stuck_jobs() function not found"

    depth = 0
    end = start
    for i, line in enumerate(lines[start:], start):
        stripped = line.strip()
        if "{" in stripped:
            depth += stripped.count("{") - stripped.count("}")
        elif "}" in stripped:
            depth -= stripped.count("}") - stripped.count("{")
        if depth == 0 and i > start:
            end = i
            break

    body_lines = lines[start : end + 1]

    # Find the first line that reads source_backend and the first
    # update_job call.
    source_backend_line = None
    update_job_line = None
    for j, l in enumerate(body_lines):
        stripped = l.lstrip()
        if stripped.startswith("#"):
            continue
        if source_backend_line is None and "source_backend" in l and ("python3" in l or "jq" in l or "=" in l):
            source_backend_line = j
        if update_job_line is None and "update_job" in l and not stripped.startswith("#"):
            update_job_line = j

    assert source_backend_line is not None, (
        "H5-NEW: source_backend must be extracted from the job JSON inside "
        "_recover_stuck_jobs(); not found."
    )
    assert update_job_line is not None, (
        "_recover_stuck_jobs() must call update_job to mark the job failed"
    )
    assert source_backend_line < update_job_line, (
        "H5-NEW: source_backend must be read BEFORE update_job() is called. "
        f"source_backend assigned at body line {source_backend_line}, "
        f"update_job called at body line {update_job_line}."
    )


def test_inline_stuck_recovery_replaced_by_function_call() -> None:
    """H5-NEW: the original inline stuck-recovery loop must no longer
    be a bare top-level for-loop — it must be replaced by (or wrapped
    in) a call to _recover_stuck_jobs.

    We assert that _recover_stuck_jobs is called somewhere outside its
    own definition (i.e., the function is actually invoked in the main
    tick body, not just defined but never called).
    """
    script = Path("scripts/ops/agnes-state-applier.sh").read_text()
    lines = script.splitlines()

    # Count occurrences of _recover_stuck_jobs — definition + call.
    occurrences = [
        (i, l) for i, l in enumerate(lines)
        if "_recover_stuck_jobs" in l and not l.lstrip().startswith("#")
    ]
    # We expect at least 2: the definition line (contains "()") and at
    # least one invocation line (no "()").
    definitions = [l for _, l in occurrences if "_recover_stuck_jobs()" in l]
    invocations = [l for _, l in occurrences if "_recover_stuck_jobs()" not in l]
    assert definitions, "_recover_stuck_jobs() function definition not found"
    assert invocations, (
        "H5-NEW: _recover_stuck_jobs must be CALLED in the main tick body, "
        "not just defined. Add `_recover_stuck_jobs` after the function definition."
    )
