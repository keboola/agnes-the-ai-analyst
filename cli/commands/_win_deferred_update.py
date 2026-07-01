"""Windows-only deferred self-update helper (spawned detached by
``cli/commands/self_upgrade.py``).

On Windows a running executable image and its loaded ``.dll`` / ``.pyd`` files
are held under a mandatory lock, so ``uv tool install --force`` cannot replace
the very venv the running agnes lives in — it fails with ``os error 5`` (Access
is denied) while removing the ``Scripts`` dir, and a half-done in-place swap
CORRUPTS the install. POSIX lets you unlink a running binary; Windows does not.

This helper is the VS-Code-style fix: it runs OUTSIDE the agnes tool venv
(launched by a NON-target Python interpreter, from a temp copy of this file so
it holds no handle inside the venv), waits for the agnes process that spawned it
to exit (releasing the lock), then performs ``uv tool install --force``, verifies
the new binary, and rolls back to the last-known-good cached wheel on failure.
The outcome is written to ``upgrade_status.json`` so it surfaces on the next
non-quiet agnes command.

Two Windows realities this handles beyond the swap itself:
1. HEADLESS — every child process is spawned ``CREATE_NO_WINDOW``. The helper
   has no console, so a console child would otherwise get a fresh one allocated,
   flashing a window per ``tasklist`` poll / ``uv`` retry / verify. (What the
   operator first saw as "blinking".)
2. STATUSLINE CONTENTION — the tool venv is re-locked on every ``agnes
   statusline`` render, which can beat a naive retry. So the install is gated on
   a best-effort "is the venv free" probe (attempt in the gap between renders,
   not against a guaranteed lock) and retried patiently, and for the duration of
   the swap a ``deferred-update.active`` sentinel tells the statusline to step
   aside.

Uses ONLY the standard library plus the ``uv`` executable on PATH — it never
imports ``cli``, so it holds no lock on the tool venv it is replacing.

Invocation (all args positional; rollback_wheel optional / may be empty):

    <non-target-python> _win_deferred_update.py <parent_pid> <staged_wheel>
        <expected_version> <config_dir> [<rollback_wheel>]
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time

_WAIT_TIMEOUT_S = 120.0      # how long to wait for the agnes process to exit
_INSTALL_BUDGET_S = 300.0    # keep retrying while the venv is locked, up to ~5 min
_INSTALL_BACKOFF_S = 2.0

# Windows: run every child console program WITHOUT popping a console window.
# The helper itself is spawned CREATE_NO_WINDOW, but a windowless parent that
# launches a console child makes Windows allocate a FRESH console for that child
# — that is the flashing the operator saw (`tasklist` polled ~1/s in the wait
# loop, `uv` retried, the verify `agnes --version`). Propagating CREATE_NO_WINDOW
# to every child keeps the whole deferred update headless. The flag exists only
# on Windows; it is 0 elsewhere so this module's unit tests still run on POSIX.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# uv-tool package name — used to locate the venv python we probe for "is the
# tool venv currently in use" before each install attempt.
_TOOL_PKG = "agnes-the-ai-analyst"

# While swapping the venv the helper drops this sentinel in the config dir;
# `agnes statusline` sees it and steps aside so the status bar isn't relaunching
# the very venv `uv tool install --force` is trying to replace on every render.
_UPDATING_SENTINEL = "deferred-update.active"


def _log(config_dir: str, msg: str) -> None:
    try:
        p = os.path.join(config_dir, "deferred-update.log")
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    """True if ``pid`` is still running (Windows ``tasklist``)."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return str(pid) in (out.stdout or "")


def _wait_for_exit(pid: int, *, timeout_s: float = _WAIT_TIMEOUT_S,
                   interval_s: float = 1.0) -> None:
    """Block until ``pid`` exits or the timeout elapses (best-effort — we
    proceed either way; the retry loop absorbs a still-held lock)."""
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if not _pid_alive(pid):
            return
        time.sleep(interval_s)


def _venv_python() -> "str | None":
    """Path to the tool venv's ``python.exe`` — the file the swap must replace,
    used only as a best-effort "is the venv in use right now" probe. ``None`` if
    uv can't tell us where the tool dir is (probe then degrades to "attempt")."""
    try:
        out = subprocess.run(
            ["uv", "tool", "dir"], capture_output=True, text=True,
            timeout=10, creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    return os.path.join(out.stdout.strip(), _TOOL_PKG, "Scripts", "python.exe")


def _venv_free(py_path: "str | None") -> bool:
    """Best-effort: ``True`` when the venv ``python.exe`` is NOT currently running
    — a running image is locked against write/delete on Windows, so opening it
    ``r+b`` raises ``PermissionError`` iff some agnes process (a statusline
    render, another session) holds it. Opening for write shares read, so it never
    blocks a concurrent launch, and we write nothing. Unknown/undiscoverable →
    ``True`` (attempt anyway; a locked attempt fails cleanly and we just retry)."""
    if not py_path or not os.path.exists(py_path):
        return True
    try:
        with open(py_path, "r+b"):
            return True
    except OSError:
        return False


def _uv_install(wheel: str, *, budget_s: float = _INSTALL_BUDGET_S,
                backoff_s: float = _INSTALL_BACKOFF_S) -> int:
    """``uv tool install --force <wheel>`` (headless), retried while the tool
    venv is held by another agnes process — the statusline / other sessions
    re-grab the lock between renders, so a single attempt often loses the race.

    Attempts only when the venv python looks free, so an attempt lands in the gap
    between renders rather than hammering `uv` (real resolve/link work) against a
    guaranteed lock; near the deadline it attempts regardless. A locked attempt
    fails cleanly — `uv` aborts before touching the install (that is why the
    failed swap never corrupted anything) — so retrying is always safe. Returns 0
    on success, else the last rc."""
    py = _venv_python()
    deadline = time.monotonic() + max(0.0, budget_s)
    rc = 1
    while True:
        near_deadline = time.monotonic() >= deadline - backoff_s
        if _venv_free(py) or near_deadline:
            try:
                rc = subprocess.run(
                    ["uv", "tool", "install", "--force", wheel],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=_NO_WINDOW,
                ).returncode
            except OSError:
                rc = 1
            if rc == 0:
                return 0
        if time.monotonic() >= deadline:
            return rc
        # Jittered backoff so we don't beat in lockstep with a ~1 Hz statusline.
        time.sleep(backoff_s + (time.monotonic() % 0.7))


def _installed_version_ok(expected_version: str) -> bool:
    """Run the freshly-installed agnes and confirm it boots and reports the
    expected version. Resolves the binary at the uv tool bin dir (not via PATH)
    when possible, and sets the recursion sentinel so its own update check is
    inert."""
    binp = ""
    try:
        out = subprocess.run(["uv", "tool", "dir", "--bin"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=_NO_WINDOW)
        if out.returncode == 0:
            binp = out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        binp = ""
    exe = os.path.join(binp, "agnes.exe") if binp else "agnes"
    env = {**os.environ, "AGNES_NO_UPDATE_CHECK": "1",
           "AGNES_SELF_UPGRADE_IN_PROGRESS": "1"}
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True,
                           timeout=30, env=env, creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and expected_version in (r.stdout or "")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_status(config_dir: str, *, success: bool, reason: str | None = None) -> None:
    """Mirror ``cli.upgrade_status.record_outcome`` from outside the venv:
    reset the failure counter on success, increment + record reason on failure."""
    p = os.path.join(config_dir, "upgrade_status.json")
    prior_failures = 0
    try:
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                prior = json.load(fh)
            v = prior.get("consecutive_failures", 0)
            if isinstance(v, int) and v >= 0:
                prior_failures = v
    except (OSError, json.JSONDecodeError):
        prior_failures = 0
    entry = {
        "last_attempt_ts": time.time(),
        "last_outcome": "success" if success else "failure",
        "consecutive_failures": 0 if success else prior_failures + 1,
    }
    if not success and reason:
        entry["last_failure_reason"] = reason[:200]
    try:
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(entry, fh)
    except OSError:
        pass


def _record_last_known_good(config_dir: str, wheel: str, version: str) -> None:
    """Record the just-installed (verified) wheel as the rollback artifact for
    the NEXT upgrade, matching the schema `cli.commands.self_upgrade` reads."""
    try:
        meta = {
            "version": version,
            "wheel_filename": os.path.basename(wheel),
            "sha256": _sha256(wheel),
        }
        with open(os.path.join(config_dir, "last_known_good.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(meta, fh)
    except OSError:
        pass


def _set_updating(config_dir: str) -> None:
    """Drop the "swap in progress" sentinel so `agnes statusline` steps aside."""
    try:
        with open(os.path.join(config_dir, _UPDATING_SENTINEL), "w",
                  encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%dT%H:%M:%S"))
    except OSError:
        pass


def _clear_updating(config_dir: str) -> None:
    """Remove the sentinel. A crash instead leaves a stale one, which the
    statusline ignores past its TTL — so the status bar can never be wedged."""
    try:
        os.remove(os.path.join(config_dir, _UPDATING_SENTINEL))
    except OSError:
        pass


def run(parent_pid: int, staged_wheel: str, expected_version: str,
        config_dir: str, rollback_wheel: str | None = None) -> int:
    """Full deferred-update flow. Returns process exit code (0 success)."""
    _log(config_dir, f"deferred update start: pid={parent_pid} wheel={staged_wheel} "
                     f"expect={expected_version} rollback={rollback_wheel or '-'}")
    _wait_for_exit(parent_pid)

    # Ask short agnes commands (the statusline) to step aside for the swap, so
    # they aren't re-locking the venv `uv` is replacing on every render. Cleared
    # in `finally` — a crash leaves only a stale sentinel, ignored past its TTL.
    _set_updating(config_dir)
    try:
        rc = _uv_install(staged_wheel)
        if rc != 0:
            _log(config_dir, f"install failed rc={rc} (venv still locked after retries)")
            _write_status(config_dir, success=False,
                         reason=f"windows deferred install rc={rc} (venv locked)")
            return 2

        if _installed_version_ok(expected_version):
            _record_last_known_good(config_dir, staged_wheel, expected_version)
            _write_status(config_dir, success=True)
            _log(config_dir, f"SUCCESS: installed {expected_version}")
            return 0

        # Verify failed → roll back to the last-known-good cached wheel if we have one.
        _log(config_dir, "verify failed after install")
        if rollback_wheel and os.path.exists(rollback_wheel):
            rb = _uv_install(rollback_wheel)
            _log(config_dir, f"rollback to {rollback_wheel} rc={rb}")
        else:
            _log(config_dir, "no rollback wheel available; leaving as-is")
        _write_status(config_dir, success=False,
                     reason=f"windows deferred: smoke failed for {expected_version}")
        return 1
    finally:
        _clear_updating(config_dir)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 4:
        return 64  # usage error
    parent_pid = int(args[0])
    staged_wheel = args[1]
    expected_version = args[2]
    config_dir = args[3]
    rollback_wheel = args[4] if len(args) > 4 and args[4] else None
    return run(parent_pid, staged_wheel, expected_version, config_dir, rollback_wheel)


if __name__ == "__main__":
    raise SystemExit(main())
