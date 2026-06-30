"""`agnes update` — one idempotent, best-effort convergence of the workspace + CLI.

A single command that brings a workspace (and the CLI itself) to the instance's
correct, current, healthy state — and the recommended way to repair a broken
install or pick up a new release. Runs the same whether triggered automatically
(SessionStart hook, detached) or manually (`agnes update` typed in a terminal).

Steps, in order, each wrapped so one failure never aborts the rest:

  1. CLI binary self-upgrade — the ONLY step with a rollback: a direct uv/pip
     reinstall guarded by a smoke test, with best-effort rollback to the prior
     wheel where one is available (in `cli/commands/self_upgrade.py`; a fully
     staged swap is a separate, not-yet-implemented hardening). A
     freshly-installed binary becomes active on the NEXT `agnes` invocation:
     the running interpreter can't replace itself, and `os.execv` is unreliable
     on Windows, so there is deliberately NO re-exec. Steps 2-6 run on the
     current binary.
  2. Workspace template — OVERRIDE: safe 3-way merge (backs up analyst edits to
     `.bak`) only when the server template SHA moved; DEFAULT: refresh the
     server-rendered CLAUDE.md, backing it up before overwrite.
  3. Agnes-owned settings — hooks / statusLine / managed slash-commands. Agnes
     owns these in BOTH modes and (re)asserts them authoritatively; foreign
     hook entries and a user statusLine are preserved.
  4. Marketplace plugins — bootstrap when the clone is missing, else cheap
     `--check` and a full reconcile only on drift.
  5. Data — `agnes pull` (MD5-skip, atomic sidecar swap; already idempotent).
  6. Report — append a JSON line to `<workspace>/.claude/agnes/update.log`
     (rotated), refresh the sentinel, record the outcome.

Only ONE update runs at a time: the command holds
`~/.config/agnes/update.lock` (cross-platform `filelock`); a second invocation
from any source exits 0 immediately. The OS releases the lock on process exit
(including crash), so the next run always proceeds. The command sets
`AGNES_NO_UPDATE_CHECK=1` for itself and its children so its own internal
`agnes` sub-invocations don't re-trigger the background auto-update.

Steps other than the CLI swap have no rollback and need none — they are
idempotent / safe to re-run, so a partial or failed run converges on the next
run.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import typer

from cli.config import _config_dir, get_server_url, get_token, get_workspace_root

update_app = typer.Typer(
    name="update",
    help="Converge this workspace + the agnes CLI to the instance's current healthy state.",
    invoke_without_command=True,
)

# Keep the report from growing unbounded across hundreds of SessionStart runs.
_REPORT_MAX_BYTES = 256 * 1024


def _agnes_version() -> str:
    try:
        import importlib.metadata as _md

        return _md.version("agnes-the-ai-analyst")
    except Exception:
        return "unknown"


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _resolve_workspace() -> Optional[Path]:
    """Locate the analyst workspace, so `agnes update` works from any cwd.

    Order: ``AGNES_LOCAL_DIR`` (set by Claude Code's hook subprocess) →
    ``get_workspace_root()`` (the config anchor written by ``agnes init``) →
    the current dir IF it looks initialised. Returns ``None`` when no
    initialised workspace can be found — the CLI step still runs (it is
    workspace-independent); the workspace steps are skipped with a note.
    """
    env_dir = os.environ.get("AGNES_LOCAL_DIR")
    if env_dir:
        return Path(env_dir).resolve()
    root = get_workspace_root()
    if root:
        return Path(root).resolve()
    cwd = Path.cwd()
    if (cwd / ".claude" / "init-complete").exists() or (cwd / ".claude" / "settings.json").exists():
        return cwd
    return None


def _run_step(name: str, fn: Callable[[], None], report: list[dict]) -> None:
    """Run one convergence step, swallowing any failure into the report.

    Best-effort contract: a single broken step (corrupt file, network blip,
    programming error) must never abort the remaining steps or flip the exit
    code. ``typer.Exit`` raised by reused command internals is treated as a
    recorded outcome, not a fatal error.
    """
    try:
        fn()
    except typer.Exit as exc:  # reused internals signal via exit codes
        code = getattr(exc, "exit_code", 0)
        if code not in (0, None):
            report.append({"stage": name, "status": "error", "detail": f"exit_code={code}"})
    except Exception as exc:  # noqa: BLE001 — best-effort by design
        report.append({"stage": name, "status": "error", "detail": f"{type(exc).__name__}: {exc}"})


# --------------------------------------------------------------------------- #
# Step 1 — CLI binary (only step with a rollback)
# --------------------------------------------------------------------------- #
def _step_cli(*, quiet: bool, report: list[dict]) -> None:
    from cli.commands import self_upgrade as su
    from cli.update_check import UpdateInfo
    from cli.upgrade_status import record_outcome

    info = su._resolve_info(force=False)
    if not isinstance(info, UpdateInfo):
        # CLI already current, offline, or unreachable — nothing to swap.
        report.append({"stage": "cli", "status": "ok", "detail": "already current / offline"})
        return
    rc = su._do_install_with_smoke_and_rollback(info, quiet=quiet)
    record_outcome(success=(rc == 0))
    if rc == 0:
        report.append({
            "stage": "cli", "status": "updated",
            "detail": f"{info.installed} -> {info.latest} (active next run)",
        })
    else:
        report.append({"stage": "cli", "status": "error", "detail": "install failed; rolled back to current"})


# --------------------------------------------------------------------------- #
# Step 2 — workspace template (OVERRIDE merge / DEFAULT CLAUDE.md)
# --------------------------------------------------------------------------- #
def _step_workspace(workspace: Path, *, server_url: str, token: str, report: list[dict]) -> None:
    from cli.lib.initial_workspace import (
        apply_update,
        download_zip,
        probe_status,
        write_agnes_env,
    )
    from cli.lib.override import read_override_metadata
    from src.initial_workspace import is_override_workspace

    status = probe_status(server_url, token)
    if status is not None and status.configured:
        # OVERRIDE mode (Initial Workspace Template configured).
        if not status.synced:
            report.append({"stage": "workspace", "status": "skipped",
                           "detail": "template configured but not synced (ask admin to Sync now)"})
            return
        sentinel = read_override_metadata(workspace) or {}
        if not is_override_workspace(workspace):
            report.append({"stage": "workspace", "status": "skipped",
                           "detail": "no override sentinel; run `agnes update-workspace` once interactively"})
        elif sentinel.get("template_sha") == status.template_sha:
            report.append({"stage": "workspace", "status": "ok", "detail": "template already current"})
        else:
            # A missing stored baseline (pre-baseline install, or a workspace
            # that was moved — the baseline is keyed by absolute path) is safe:
            # the 3-way engine backs up every file that differs from the new
            # template before overwriting, and apply_update() then establishes
            # the baseline so the next run is a precise merge.
            new_zip = download_zip(server_url, token)
            result = apply_update(workspace, new_zip, status, server_url, token,
                                  agnes_version=_agnes_version())
            report.append({"stage": "workspace", "status": "merged", "detail": {
                "created": len(result.created),
                "updated": len(result.updated),
                "backed_up": [b for _, b in result.backed_up],
                "template_sha": (status.template_sha or "")[:10],
            }})
        # Refresh per-tenant operator params regardless of the merge decision.
        try:
            write_agnes_env(workspace, server_url, token)
        except Exception:  # noqa: BLE001 — best-effort env refresh
            pass
    else:
        # DEFAULT mode — no template; CLAUDE.md is server-rendered.
        _refresh_default_claude_md(workspace, server_url=server_url, token=token, report=report)


def _refresh_default_claude_md(workspace: Path, *, server_url: str, token: str, report: list[dict]) -> None:
    from cli.client import api_get
    from cli.lib.pull import _override_server_env
    from src.initial_workspace import _unique_bak_path

    with _override_server_env(server_url, token):
        resp = api_get("/api/welcome", params={"server_url": server_url})
    resp.raise_for_status()
    content = resp.json().get("content", "")
    if not content:
        report.append({"stage": "workspace", "status": "skipped", "detail": "empty /api/welcome content"})
        return
    claude_md = workspace / "CLAUDE.md"
    if claude_md.exists() and claude_md.read_text(encoding="utf-8") == content:
        report.append({"stage": "workspace", "status": "ok", "detail": "CLAUDE.md already current"})
        return
    backup_name = ""
    if claude_md.exists():
        bak = _unique_bak_path(claude_md.with_name(f"CLAUDE.md.bak.{_utc_stamp()}"))
        bak.write_bytes(claude_md.read_bytes())
        backup_name = bak.name
    claude_md.write_text(content, encoding="utf-8")
    report.append({"stage": "workspace", "status": "refreshed",
                   "detail": f"CLAUDE.md updated{f' (backup {backup_name})' if backup_name else ''}"})


# --------------------------------------------------------------------------- #
# Step 3 — Agnes-owned settings (hooks / statusline / commands), both modes
# --------------------------------------------------------------------------- #
def _step_agnes_owned(workspace: Path, *, report: list[dict]) -> None:
    from cli.lib.commands import install_claude_commands
    from cli.lib.hooks import install_claude_hooks

    install_claude_hooks(workspace)
    install_claude_commands(workspace)
    report.append({"stage": "agnes-owned", "status": "ok", "detail": "hooks / statusline / commands reasserted"})


# --------------------------------------------------------------------------- #
# Step 4 — marketplace plugins (bootstrap if missing; full reconcile on drift)
# --------------------------------------------------------------------------- #
def _step_marketplace(*, report: list[dict]) -> None:
    from cli.commands.refresh_marketplace import _EXIT_MARKETPLACE_DRIFT, refresh_marketplace
    from cli.lib.marketplace import CLONE_DIR

    def _invoke(*, check: bool, bootstrap: bool) -> int:
        try:
            refresh_marketplace(check=check, bootstrap=bootstrap)
            return 0
        except typer.Exit as exc:
            return int(getattr(exc, "exit_code", 0) or 0)

    if not (CLONE_DIR / ".git").is_dir():
        rc = _invoke(check=False, bootstrap=True)
        report.append({"stage": "marketplace", "status": "bootstrapped" if rc == 0 else "error",
                       "detail": f"clone missing; bootstrap exit={rc}"})
        return

    rc = _invoke(check=True, bootstrap=False)
    if rc == _EXIT_MARKETPLACE_DRIFT:
        full = _invoke(check=False, bootstrap=False)
        report.append({"stage": "marketplace", "status": "reconciled" if full == 0 else "error",
                       "detail": f"drift detected; reconcile exit={full}"})
    elif rc == 0:
        report.append({"stage": "marketplace", "status": "ok", "detail": "plugins already current"})
    else:
        report.append({"stage": "marketplace", "status": "error", "detail": f"check exit={rc}"})


# --------------------------------------------------------------------------- #
# Step 5 — data pull
# --------------------------------------------------------------------------- #
def _step_pull(workspace: Path, *, server_url: str, token: str, quiet: bool, report: list[dict]) -> None:
    from cli.lib.pull import run_pull

    result = run_pull(server_url, token, workspace, dry_run=False,
                      skip_materialize=False, show_progress=not quiet)
    if getattr(result, "errors", None):
        report.append({"stage": "pull", "status": "error", "detail": list(result.errors)})
    else:
        report.append({"stage": "pull", "status": "ok",
                       "detail": f"{result.tables_updated} tables, {result.parquets_total} parquets"})


# --------------------------------------------------------------------------- #
# Step 6 — report
# --------------------------------------------------------------------------- #
def _write_report(workspace: Path, entry: dict) -> Optional[Path]:
    log = workspace / ".claude" / "agnes" / "update.log"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        # Rotate: keep the tail when the file grows past the cap so the log is
        # bounded but still carries recent history.
        if log.exists() and log.stat().st_size > _REPORT_MAX_BYTES:
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
            log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        return log
    except OSError:
        return None


@update_app.callback(invoke_without_command=True)
def update(
    quiet: bool = typer.Option(
        False, "--quiet", help="Suppress progress output (SessionStart hook path). Errors still go to the report."
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit the run report as a single JSON object on stdout."
    ),
) -> None:
    """Converge the workspace + CLI; safe to run repeatedly and from any cwd."""
    from cli.lib.push_lock import acquire_path_or_skip

    # Disable nested auto-update detection in this process AND its children
    # (run_pull / refresh-marketplace / the smoke-test `agnes --version`).
    os.environ["AGNES_NO_UPDATE_CHECK"] = "1"

    report: list[dict] = []

    lock_file = _config_dir() / "update.lock"
    with acquire_path_or_skip(lock_file) as lock:
        if lock is None:
            if not quiet and not as_json:
                typer.echo("Another `agnes update` is already running — exiting.")
            raise typer.Exit(0)

        # `agnes update` is the repair path, so read ALL persisted-config
        # values under the best-effort boundary: get_server_url/get_token AND
        # _resolve_workspace (which re-reads config.yaml via get_workspace_root)
        # must degrade into skipped workspace steps + a report line, not a raw
        # traceback out of the command meant to fix a broken install. A None
        # token routes to the "no token configured" skip below; a None
        # workspace routes to the "no initialised workspace" skip.
        server_url = ""
        token: Optional[str] = None
        workspace: Optional[Path] = None
        try:
            server_url = get_server_url()
            token = get_token()
            workspace = _resolve_workspace()
        except Exception as exc:  # noqa: BLE001 — best-effort, mirror _run_step
            report.append({"stage": "config", "status": "error",
                           "detail": f"{type(exc).__name__}: {exc}"})

        # Step 1 — CLI binary (workspace-independent; always runs).
        _run_step("cli", lambda: _step_cli(quiet=quiet, report=report), report)

        if workspace is None:
            report.append({"stage": "workspace", "status": "skipped",
                           "detail": "no initialised workspace found (run from the workspace or `agnes init`)"})
        elif not token:
            report.append({"stage": "workspace", "status": "skipped", "detail": "no token configured"})
        else:
            # Workspace-relative steps need cwd == workspace (marketplace installs
            # plugins with --scope project into cwd, and the report log lands
            # under the workspace). If we can't enter the workspace (deleted,
            # not a dir, unreadable), DO NOT run those steps from the launching
            # cwd — that would scatter plugin/settings writes into an unrelated
            # directory. Record the failure and skip; the workspace-independent
            # CLI step above already ran.
            prev_cwd = Path.cwd()
            try:
                os.chdir(workspace)
            except OSError as exc:
                report.append({
                    "stage": "workspace", "status": "error",
                    "detail": f"cannot enter workspace {workspace}: {exc}; skipped workspace steps",
                })
            else:
                try:
                    _run_step("workspace", lambda: _step_workspace(
                        workspace, server_url=server_url, token=token, report=report), report)
                    _run_step("agnes-owned", lambda: _step_agnes_owned(workspace, report=report), report)
                    _run_step("marketplace", lambda: _step_marketplace(report=report), report)
                    _run_step("pull", lambda: _step_pull(
                        workspace, server_url=server_url, token=token, quiet=quiet, report=report), report)
                finally:
                    try:
                        os.chdir(prev_cwd)
                    except OSError:
                        pass

        entry = {
            "ts": _utc_stamp(),
            "agnes_version": _agnes_version(),
            "workspace": str(workspace) if workspace else None,
            "steps": report,
        }
        log_path = _write_report(workspace, entry) if workspace else None

    if as_json:
        typer.echo(json.dumps(entry))
        return
    if quiet:
        return

    typer.echo("agnes update — convergence report:")
    for step in report:
        typer.echo(f"  [{step['status']}] {step['stage']}: {step['detail']}")
    if log_path:
        typer.echo(f"Report: {log_path}")
    if any(s["stage"] == "cli" and s["status"] == "updated" for s in report):
        typer.echo("CLI was updated — run `agnes update` once more to finish converging on the new version.")
