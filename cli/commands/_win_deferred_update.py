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
to exit (releasing the lock), then performs ``uv tool install --force`` with
bounded retry (the statusline / other sessions can re-grab the lock briefly),
verifies the new binary, and rolls back to the last-known-good cached wheel on
failure. The outcome is written to ``upgrade_status.json`` so it surfaces on the
next non-quiet agnes command.

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

_WAIT_TIMEOUT_S = 120.0     # how long to wait for the agnes process to exit
_INSTALL_RETRIES = 30       # bounded retry while the venv is still locked
_INSTALL_BACKOFF_S = 2.0


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
            capture_output=True, text=True, timeout=10,
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


def _uv_install(wheel: str, *, retries: int = _INSTALL_RETRIES,
                backoff_s: float = _INSTALL_BACKOFF_S) -> int:
    """``uv tool install --force <wheel>`` with bounded retry on failure (the
    lock may linger after the spawning process exits, or be re-grabbed by the
    statusline / another session). Returns 0 on success, else the last rc."""
    rc = 1
    for _ in range(max(1, retries)):
        try:
            rc = subprocess.run(["uv", "tool", "install", "--force", wheel]).returncode
        except OSError:
            rc = 1
        if rc == 0:
            return 0
        time.sleep(backoff_s)
    return rc


def _installed_version_ok(expected_version: str) -> bool:
    """Run the freshly-installed agnes and confirm it boots and reports the
    expected version. Resolves the binary at the uv tool bin dir (not via PATH)
    when possible, and sets the recursion sentinel so its own update check is
    inert."""
    binp = ""
    try:
        out = subprocess.run(["uv", "tool", "dir", "--bin"],
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            binp = out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        binp = ""
    exe = os.path.join(binp, "agnes.exe") if binp else "agnes"
    env = {**os.environ, "AGNES_NO_UPDATE_CHECK": "1",
           "AGNES_SELF_UPGRADE_IN_PROGRESS": "1"}
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True,
                           timeout=30, env=env)
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


def run(parent_pid: int, staged_wheel: str, expected_version: str,
        config_dir: str, rollback_wheel: str | None = None) -> int:
    """Full deferred-update flow. Returns process exit code (0 success)."""
    _log(config_dir, f"deferred update start: pid={parent_pid} wheel={staged_wheel} "
                     f"expect={expected_version} rollback={rollback_wheel or '-'}")
    _wait_for_exit(parent_pid)

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
