"""`agnes self-upgrade` — pull the wheel from the server, reinstall, smoke-test,
roll back on failure."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional, Union

import typer

from cli.config import _config_dir, get_server_url
from cli.update_check import UpdateInfo, check, format_outdated_notice

self_upgrade_app = typer.Typer(
    name="self-upgrade",
    help="Reinstall the CLI from the server's currently-shipped wheel.",
    invoke_without_command=True,
)

_SENTINEL_ENV = "AGNES_SELF_UPGRADE_IN_PROGRESS"


class _Unreachable:
    """Sentinel returned by _resolve_info when --force was specified but the
    server probe failed. Distinguishes 'explicitly requested an upgrade and
    we couldn't reach the server' (exit 1, stderr) from 'no upgrade needed'
    (exit 0, silent)."""


_UNREACHABLE = _Unreachable()


def _invalidate_update_cache() -> None:
    """Drop update_check.json so the next CLI invocation re-probes /cli/latest."""
    (_config_dir() / "update_check.json").unlink(missing_ok=True)


def _last_known_good_path() -> Path:
    return _config_dir() / "last_known_good.json"


def _read_last_known_good() -> Optional[str]:
    p = _last_known_good_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("download_url")
    except (OSError, json.JSONDecodeError):
        return None


def _record_last_known_good(download_url: str) -> None:
    p = _last_known_good_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"download_url": download_url}), encoding="utf-8")
    except OSError:
        pass  # best-effort — failure to record must not break the flow


def _uv_tool_bin_path() -> Optional[Path]:
    """Locate the agnes shim uv installed.

    Tries `uv tool dir --bin` (uv >= 0.5). Falls back to uv's documented
    default install location on older uv where `--bin` is rejected.
    """
    bin_dir: Optional[Path] = None
    try:
        out = subprocess.run(
            ["uv", "tool", "dir", "--bin"], capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            bin_dir = Path(out.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        bin_dir = None

    if bin_dir is None:
        if sys.platform == "win32":
            appdata = os.environ.get("APPDATA")
            if appdata:
                bin_dir = Path(appdata) / "uv" / "tools" / "bin"
        else:
            bin_dir = Path.home() / ".local" / "bin"

    if bin_dir is None or not bin_dir.exists():
        return None

    for name in ("agnes.exe", "agnes"):
        candidate = bin_dir / name
        if candidate.exists():
            return candidate
    return None


def _pip_bin_path() -> Optional[Path]:
    """`<venv>/bin/agnes` (POSIX) or `<venv>\\Scripts\\agnes.exe` (Windows)."""
    parent = Path(sys.executable).parent
    name = "agnes.exe" if sys.platform == "win32" else "agnes"
    candidate = parent / name
    return candidate if candidate.exists() else None


def _install_with_uv(download_url: str, *, quiet: bool) -> int:
    out = subprocess.DEVNULL if quiet else None
    return subprocess.run(
        ["uv", "tool", "install", "--force", download_url], stdout=out
    ).returncode


def _install_with_pip(download_url: str, *, quiet: bool) -> int:
    """Install into the SAME interpreter that's running this command.

    sys.executable resolves to the venv that owns the live `agnes` binary.
    `python3` would PATH-resolve to system python on macOS, landing the
    wheel outside the agnes venv. `--user` is wrong inside a uv-tool venv
    (targets ~/.local outside the venv).
    """
    out = subprocess.DEVNULL if quiet else None
    with tempfile.TemporaryDirectory(prefix="agnes_cli.") as td:
        wheel_path = Path(td) / "agnes.whl"
        rc = subprocess.run(
            ["curl", "-fsSL", "-o", str(wheel_path), download_url], stdout=out
        ).returncode
        if rc != 0:
            return rc
        return subprocess.run(
            [sys.executable, "-m", "pip", "install",
             "--force-reinstall", "--no-deps", str(wheel_path)],
            stdout=out,
        ).returncode


def _smoke_test_new_binary(install_method: str, expected_version: str) -> tuple[bool, str]:
    """Exec `<install-path>/agnes --version` and confirm it boots AND reports
    the expected version. Resolves the binary at the install-method-specific
    path rather than via PATH — defends against a stale shadow ahead of the
    freshly-installed binary in $PATH."""
    binary = _uv_tool_bin_path() if install_method == "uv" else _pip_bin_path()
    if binary is None:
        return False, f"agnes binary not found at expected {install_method} install path"
    try:
        env = {**os.environ, "AGNES_NO_UPDATE_CHECK": "1", _SENTINEL_ENV: "1"}
        out = subprocess.run(
            [str(binary), "--version"],
            capture_output=True, text=True, timeout=10, env=env,
        )
        if out.returncode != 0:
            return False, f"exit {out.returncode}: {out.stderr.strip()[:200]}"
        # Use Version() equality (PEP 440-aware) so "0.40.0" doesn't match "0.40.10".
        from packaging.version import InvalidVersion, Version
        tokens = out.stdout.strip().split()
        actual_str = tokens[-1] if tokens else ""
        try:
            if Version(actual_str) != Version(expected_version):
                return False, (
                    f"version mismatch: expected {expected_version}, "
                    f"got {actual_str}"
                )
        except InvalidVersion:
            return False, f"unparseable version output: {out.stdout.strip()[:80]}"
        return True, out.stdout.strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"{type(e).__name__}: {e}"


def _resolve_info(force: bool) -> Union[UpdateInfo, _Unreachable, None]:
    """Returns:
      UpdateInfo  — install this wheel
      _UNREACHABLE — --force specified, server probe failed
      None        — nothing to do (current, or offline without --force)
    """
    # Always invalidate the cache — an explicit `agnes self-upgrade` is
    # the user asking "is there a newer version RIGHT NOW", not "use the
    # 24h cached answer". The cache exists to keep the implicit warning
    # loop in the root callback (`agnes <anything>`) from re-probing
    # `/cli/latest` on every invocation; it has no place gating the
    # explicit upgrade command.
    _invalidate_update_cache()
    info = check(get_server_url(), bypass_disabled=True)
    if info is None:
        return _UNREACHABLE if force else None
    if not info.download_url:
        return None
    if not force and not info.is_outdated():
        return None
    return info


def _do_install_with_smoke_and_rollback(
    info: UpdateInfo, *, quiet: bool
) -> int:
    """Returns the exit code typer should use (0 success, 1 failure)."""
    prior_url = _read_last_known_good()  # may be None on first upgrade

    if shutil.which("uv"):
        rc = _install_with_uv(info.download_url, quiet=quiet)
        method = "uv"
    else:
        rc = _install_with_pip(info.download_url, quiet=quiet)
        method = "pip"

    if rc != 0:
        sys.stderr.write(f"agnes self-upgrade: install failed with exit {rc}\n")
        return 1

    ok, detail = _smoke_test_new_binary(method, expected_version=info.latest)
    if not ok:
        sys.stderr.write(
            f"agnes self-upgrade: new binary failed smoke test ({detail}).\n"
        )
        server = get_server_url().rstrip("/")
        bootstrap_recovery = f"  Manual recovery: curl -fsSL {server}/cli/install.sh | bash\n"
        if prior_url and prior_url != info.download_url:
            sys.stderr.write(f"  rolling back to {prior_url}\n")
            rb_rc = (
                _install_with_uv(prior_url, quiet=True)
                if method == "uv"
                else _install_with_pip(prior_url, quiet=True)
            )
            if rb_rc != 0:
                sys.stderr.write(
                    f"  rollback ALSO failed (rc={rb_rc}); CLI is in a broken state.\n"
                )
                sys.stderr.write(bootstrap_recovery)
        else:
            sys.stderr.write(
                "  no prior wheel URL on record; rollback skipped.\n"
            )
            sys.stderr.write(bootstrap_recovery)
        return 1

    # Convention: record then invalidate. No correctness consequence either way.
    _record_last_known_good(info.download_url)
    _invalidate_update_cache()
    if not quiet:
        typer.echo(f"agnes self-upgrade: installed {info.latest}", err=True)
    return 0


@self_upgrade_app.callback()
def self_upgrade(
    quiet: bool = typer.Option(
        False, "--quiet",
        help="Suppress progress output. Failures still surface on stderr.",
    ),
    check_only: bool = typer.Option(
        False, "--check-only",
        help="Print status, don't install. Exit 1 if outdated.",
    ),
    force: bool = typer.Option(
        False, "--force",
        help="Reinstall the server's current wheel even when already on the latest version.",
    ),
) -> None:
    # Snapshot any prior sentinel so we restore (rather than destroy) it
    # in finally — we own the namespace but a wrapper could legitimately
    # set it.
    prior_sentinel = os.environ.get(_SENTINEL_ENV)
    os.environ[_SENTINEL_ENV] = "1"
    try:
        info = _resolve_info(force)

        # --check-only is read-only intent — never exit non-zero on
        # transport errors. If unreachable, treat as "can't tell, current"
        # and exit 0 silently.
        if check_only:
            if isinstance(info, _Unreachable) or info is None or not info.is_outdated():
                raise typer.Exit(0)
            typer.echo(format_outdated_notice(info), err=True)
            raise typer.Exit(1)

        if isinstance(info, _Unreachable):
            sys.stderr.write(
                f"agnes self-upgrade: cannot reach {get_server_url()}/cli/latest\n"
            )
            raise typer.Exit(1)

        if info is None:
            raise typer.Exit(0)  # nothing to do, silent

        rc = _do_install_with_smoke_and_rollback(info, quiet=quiet)
        raise typer.Exit(rc)
    finally:
        if prior_sentinel is None:
            os.environ.pop(_SENTINEL_ENV, None)
        else:
            os.environ[_SENTINEL_ENV] = prior_sentinel
