"""Generic Python subprocess helper for memory-isolated job execution.

Spawns a Python subprocess that imports the requested entry module, feeds
arguments as JSON via stdin, and reads back a JSON result from stdout.
The subprocess exits after writing its result — **all memory returns to
the OS**, bypassing the anon-arena retention that compounds when
DuckDB-heavy jobs run in-process across many iterations of a loop (see
`app/api/sync.py` profile / materialize loops).

Why a worker module instead of `multiprocessing`:
- `multiprocessing.Process` forks the current interpreter; child inherits
  the parent's anon mmap arenas via copy-on-write. Memory cleanup on
  child exit is reliable, but the cold-start cost of importing app
  modules is amortized only weakly because each child re-pickles the
  shared state from the parent. For the bursty per-table calls these
  loops make, `python -m <worker_module>` with a clean Python interpreter
  is simpler to reason about and isolates true OS-process boundaries.
- The trade-off is a fixed ~300-500 ms cold-start tax per call. The
  materialize / profile work itself takes seconds-to-minutes against
  real tables, so the overhead is in the noise; the memory headroom is
  what matters.

Contract for worker modules:
- They MUST read a single JSON value from stdin and produce a single JSON
  value on stdout. Anything else on stdout (debug prints, etc.) breaks
  the protocol; route human-readable logs to stderr.
- Non-zero exit code propagates as `SubprocessJobError` with stderr
  attached. Workers can use specific exit codes to signal categories
  (1 = generic failure, 2 = partial — see the extractor's exit code
  convention in `app/api/sync.py:_run_sync`).

Error / timeout behavior mirrors the extractor subprocess pattern in
``app/api/sync.py`` (SIGTERM grace window then SIGKILL on timeout) so
the operational experience stays consistent.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class SubprocessJobError(RuntimeError):
    """Raised when a subprocess job fails (non-zero exit, JSON parse error,
    timeout). Carries the stderr tail for caller-side logging."""

    def __init__(self, message: str, *, stderr: str = "", exit_code: Optional[int] = None):
        super().__init__(message)
        self.stderr = stderr
        self.exit_code = exit_code


def run_subprocess_job(
    module: str,
    args: dict,
    *,
    timeout_sec: int = 300,
    env_extra: Optional[dict] = None,
    cwd: Optional[Path] = None,
) -> Any:
    """Run a Python subprocess job and return its JSON result.

    Args:
        module: Module path callable via ``python -m <module>``.
            Worker must implement ``main()`` (or be runnable as a module
            entry point) that reads JSON from stdin and writes JSON to
            stdout. See ``src/_profiler_worker.py`` for the canonical
            example.
        args: JSON-serializable dict passed to the worker via stdin.
            The worker's first action is typically
            ``args = json.load(sys.stdin)``.
        timeout_sec: Hard upper bound on subprocess wall-time. On
            timeout the process group is sent SIGTERM, given 10 s to
            clean up, then SIGKILL'd. Default 300 s — appropriate for
            the bursty per-table profile / materialize loops; raise it
            for whole-pass jobs.
        env_extra: Optional environment-variable overlay on top of the
            inherited environment. Use this to pass secrets the worker
            needs (e.g. KEBOOLA_STORAGE_TOKEN) without putting them on
            the argv / stdin protocol.
        cwd: Working directory for the subprocess. Defaults to the
            repository root inferred from this file's location, which
            matches the in-process import behavior so relative paths in
            the worker resolve identically.

    Returns:
        The JSON value the worker wrote to stdout. Type depends on the
        worker — typically a dict.

    Raises:
        SubprocessJobError: on any failure mode (non-zero exit, JSON
            parse error, timeout). The exception's ``stderr`` attribute
            carries the captured stderr tail for caller logging /
            re-raising into the operator's view.
    """
    if cwd is None:
        # Repository root — three levels up from this file
        # (.../src/_subprocess_runner.py → .../src → .../<repo>).
        cwd = Path(__file__).resolve().parent.parent

    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)

    cmd = [sys.executable, "-m", module]
    payload = json.dumps(args)

    # start_new_session=True so a timeout can take down the worker AND
    # any grandchild processes it might have spawned (e.g. DuckDB
    # background threads, requests connection pool workers) in one
    # process-group kill. Matches the extractor-subprocess pattern at
    # app/api/sync.py:_run_sync.
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(cwd),
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(input=payload, timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        # SIGTERM the whole process group first to give the worker a
        # chance to flush stderr / release resources cleanly.
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""
        raise SubprocessJobError(
            f"subprocess timed out after {timeout_sec}s (module={module})",
            stderr=(stderr or "")[-2000:],
            exit_code=None,
        )

    if proc.returncode != 0:
        raise SubprocessJobError(
            f"subprocess exited {proc.returncode} (module={module})",
            stderr=(stderr or "")[-2000:],
            exit_code=proc.returncode,
        )

    if not stdout.strip():
        raise SubprocessJobError(
            f"subprocess produced empty stdout (module={module})",
            stderr=(stderr or "")[-2000:],
            exit_code=proc.returncode,
        )

    try:
        return json.loads(stdout)
    except json.JSONDecodeError as e:
        raise SubprocessJobError(
            f"subprocess stdout is not valid JSON (module={module}): {e}; "
            f"stdout tail: {stdout[-500:]!r}",
            stderr=(stderr or "")[-2000:],
            exit_code=proc.returncode,
        ) from e
