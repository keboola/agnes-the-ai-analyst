"""`agnes global` — the user-scope (all-repositories) Agnes layer (spec §6).

`enable` idempotently converges five artifacts (user-scope stack plugins,
one user-scope stdio MCP entry, a marker-fenced rails block in the user's
CLAUDE.md, an optional user-level SessionStart hook, and the config flag);
`disable` reverts exactly what enable wrote; `status` reports each
artifact. The module is named `global_scope` because `global` is a Python
keyword; the command name is registered as `global` in cli/main.py.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

import typer

from cli.client import api_get
from cli.config import get_token, load_config, save_config
from cli.error_render import render_error
from cli.lib.user_scope import (
    install_user_session_hook,
    load_global_rails,
    rails_block_state,
    remove_rails_block,
    remove_user_session_hook,
    upsert_rails_block,
    user_claude_md_path,
    user_session_hook_state,
)

global_app = typer.Typer(help="Manage the user-scope (all-repositories) Agnes layer")

MCP_SERVER_NAME = "agnes"


def _verify_credentials() -> bool:
    """One cheap authenticated probe — same as `agnes init` step 2."""
    try:
        return api_get("/api/catalog/tables").status_code == 200
    except Exception:
        return False


def _claude_cmd() -> Optional[list[str]]:
    from cli.commands.refresh_marketplace import _claude_base_cmd

    return _claude_base_cmd()


def _agnes_binary() -> str:
    """Absolute path of the agnes launcher (spec §6.1 step 3)."""
    found = shutil.which("agnes") or sys.argv[0]
    return str(Path(found).resolve())


def _mcp_entry_info() -> tuple[str, Optional[str]]:
    """(`'ours' | 'foreign' | 'absent'`, registered command path or None).

    `claude mcp get agnes` prints "Command: <path>" and "Args: <args>".
    Ours == the command's basename is the agnes launcher AND the args are
    exactly the `mcp` subcommand. The output header always contains the
    literal "agnes" (it is the lookup name), so matching on the whole
    output would classify ANY same-named entry as ours — the basename
    check is the actual discriminator (review finding on the first cut).
    """
    base = _claude_cmd()
    if base is None:
        return "absent", None
    result = subprocess.run(
        [*base, "mcp", "get", MCP_SERVER_NAME],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        return "absent", None
    command: Optional[str] = None
    args: Optional[str] = None
    for line in (result.stdout or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("Command:"):
            command = stripped[len("Command:") :].strip() or None
        elif stripped.startswith("Args:"):
            args = stripped[len("Args:") :].strip()
    is_ours = (
        command is not None and Path(command).name.lower() in ("agnes", "agnes.exe", "agnes.cmd") and args == "mcp"
    )
    return ("ours" if is_ours else "foreign"), command


def _mcp_entry_state() -> str:
    return _mcp_entry_info()[0]


def run_convergence(*, want_hook: bool, force: bool, report: list[dict]) -> None:
    """Shared engine for `enable` and `agnes update`'s `global` step.

    Every step is check-then-act; failures are recorded, never raised.
    Workspace-independent and safe from any cwd — the user-target reconcile
    performs no cwd writes (spec §7.1) and everything else targets the
    user's home-scope files only (spec §8).
    """

    from cli.commands.refresh_marketplace import (
        _EXIT_MARKETPLACE_DRIFT,
        CLONE_DIR,
        refresh_marketplace,
        run_user_scope_reconcile,
    )

    # 1 — marketplace clone + plugins (user scope; no cwd writes, spec §7.1)
    if not (CLONE_DIR / ".git").is_dir():
        # Spec §6.1 step 1: reuse the existing bootstrap path (clone +
        # `claude plugin marketplace add` + first reconcile), then fall
        # through — the reconcile below is an idempotent no-op after it.
        try:
            refresh_marketplace(check=False, bootstrap=True, target="user")
            report.append({"stage": "marketplace", "status": "bootstrapped", "detail": str(CLONE_DIR)})
        except typer.Exit as exc:
            report.append(
                {
                    "stage": "marketplace",
                    "status": "error",
                    "detail": f"bootstrap exit={getattr(exc, 'exit_code', 1)}",
                }
            )
    elif not load_config().get("workspace_root"):
        # Pure-MCP persona (enable without any anchored workspace): the
        # workspace marketplace step — the only place that fetches the
        # clone — never runs for them, so stack changes would stay frozen
        # at bootstrap. Do the cheap drift-check + refresh here.
        try:
            refresh_marketplace(check=True, bootstrap=False, target="user")
        except typer.Exit as exc:
            if int(getattr(exc, "exit_code", 0) or 0) == _EXIT_MARKETPLACE_DRIFT:
                try:
                    refresh_marketplace(check=False, bootstrap=False, target="user")
                except typer.Exit:
                    pass

    if not (CLONE_DIR / ".git").is_dir():
        report.append({"stage": "plugins", "status": "skipped", "detail": f"no marketplace clone at {CLONE_DIR}"})
    else:
        try:
            events = run_user_scope_reconcile(quiet=True)
            changed = sum(len(v) for v in events.values())
            report.append(
                {
                    "stage": "plugins",
                    "status": "reconciled" if changed else "ok",
                    "detail": json.dumps({k: v for k, v in events.items() if v})
                    if changed
                    else "user-scope plugins current",
                }
            )
        except Exception as exc:  # noqa: BLE001 — convergence must not abort
            report.append({"stage": "plugins", "status": "error", "detail": str(exc)})

    # 2 — MCP entry (via the claude CLI only — spec §6.4)
    state, registered_cmd = _mcp_entry_info()
    if state == "ours" and registered_cmd and not Path(registered_cmd).exists():
        # Launcher moved since enable (pipx→uv reinstall, new venv): the
        # registered path is dead — re-register with the live binary
        # (spec §7.2c "repair the binary path if the launcher moved").
        base = _claude_cmd()
        if base is None:
            report.append({"stage": "mcp", "status": "error", "detail": "`claude` CLI not on PATH"})
        else:
            subprocess.run(
                [*base, "mcp", "remove", MCP_SERVER_NAME, "-s", "user"],
                capture_output=True,
                text=True,
                check=False,
            )
            result = subprocess.run(
                [*base, "mcp", "add", "--scope", "user", MCP_SERVER_NAME, "--", _agnes_binary(), "mcp"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            report.append(
                {
                    "stage": "mcp",
                    "status": "repaired" if result.returncode == 0 else "error",
                    "detail": f"registered binary was missing ({registered_cmd}); re-added",
                }
            )
    elif state == "ours":
        report.append({"stage": "mcp", "status": "ok", "detail": "user-scope stdio entry present"})
    elif state == "foreign" and not force:
        report.append(
            {
                "stage": "mcp",
                "status": "skipped",
                "detail": (
                    f"an MCP server named '{MCP_SERVER_NAME}' exists and is not ours; re-run with --force to replace"
                ),
            }
        )
    else:
        base = _claude_cmd()
        if base is None:
            report.append({"stage": "mcp", "status": "error", "detail": "`claude` CLI not on PATH"})
        else:
            if state == "foreign":
                subprocess.run(
                    [*base, "mcp", "remove", MCP_SERVER_NAME, "-s", "user"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            result = subprocess.run(
                [*base, "mcp", "add", "--scope", "user", MCP_SERVER_NAME, "--", _agnes_binary(), "mcp"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            report.append(
                {
                    "stage": "mcp",
                    "status": "added" if result.returncode == 0 else "error",
                    "detail": (result.stderr or result.stdout or "").strip()[:200] or "registered",
                }
            )

    # 3 — rails block
    outcome = upsert_rails_block(user_claude_md_path(), load_global_rails())
    report.append({"stage": "rails", "status": outcome, "detail": str(user_claude_md_path())})

    # 4 — user-level SessionStart hook (default ON, spec §13.2)
    if want_hook:
        report.append(
            {
                "stage": "hook",
                "status": install_user_session_hook(),
                "detail": "SessionStart -> detached `agnes update --quiet`",
            }
        )
    else:
        report.append({"stage": "hook", "status": "skipped", "detail": "--no-hook / global_hook=false"})


def _print_report(report: list[dict], *, as_json: bool, trailer: Optional[str] = None) -> None:
    if as_json:
        typer.echo(json.dumps({"report": report}))
        return
    for row in report:
        typer.echo(f"  {row['stage']:<12} {row['status']:<14} {row['detail']}")
    if trailer:
        typer.echo(trailer)


@global_app.command("enable")
def enable(
    no_hook: bool = typer.Option(False, "--no-hook", help="Do not install the user-level SessionStart update hook."),
    force: bool = typer.Option(False, "--force", help="Replace a foreign MCP server entry named 'agnes'."),
    as_json: bool = typer.Option(False, "--json", help="Emit a single JSON report."),
):
    """Enable Agnes in every repository: user-scope plugins, MCP entry,
    rails block, SessionStart hook, config flag. Idempotent."""
    if _claude_cmd() is None:
        typer.echo(
            render_error(
                0,
                {
                    "detail": {
                        "kind": "partial_state",
                        "hint": "`claude` CLI not found on PATH — install Claude Code first.",
                    }
                },
            ),
            err=True,
        )
        raise typer.Exit(1)
    if not get_token() or not _verify_credentials():
        typer.echo(
            render_error(
                401,
                {
                    "detail": {
                        "kind": "auth_failed",
                        "hint": "No working Agnes credentials. Run `agnes auth login` or `agnes init` first.",
                    }
                },
            ),
            err=True,
        )
        raise typer.Exit(1)

    if not load_config().get("workspace_root"):
        typer.echo(
            "note: no anchored workspace yet — local-data MCP tools resolve nothing until `agnes init` runs.",
            err=True,
        )

    import contextlib
    import io

    report: list[dict] = []
    # --json emits exactly ONE JSON object on stdout, but the bootstrap
    # path inside run_convergence (git clone + first plugin installs)
    # echoes progress unsuppressed — sink stdout under --json, mirroring
    # `_step_global` (same bug class as the #1105 review incident).
    sink = contextlib.redirect_stdout(io.StringIO()) if as_json else contextlib.nullcontext()
    with sink:
        run_convergence(want_hook=not no_hook, force=force, report=report)
    save_config({"global_scope": True, "global_hook": not no_hook})
    report.append({"stage": "flag", "status": "ok", "detail": "global_scope=true"})
    _print_report(
        report,
        as_json=as_json,
        trailer="Global layer enabled — restart Claude Code sessions to pick it up.",
    )


@global_app.command("disable")
def disable(
    as_json: bool = typer.Option(False, "--json", help="Emit a single JSON report."),
):
    """Revert exactly what `enable` wrote. Marketplace registration and the
    clone stay (the workspace flow uses them too, spec §6.2)."""
    report: list[dict] = []

    from cli.commands.refresh_marketplace import _list_installed_agnes_plugins

    base = _claude_cmd()
    installed = _list_installed_agnes_plugins("user") if base else None
    if installed:
        removed: list[str] = []
        for name in sorted(installed):
            result = subprocess.run(
                [*base, "plugin", "uninstall", f"{name}@agnes", "--scope", "user"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if result.returncode == 0:
                removed.append(name)
        report.append({"stage": "plugins", "status": "removed", "detail": ", ".join(removed) or "none"})
    elif installed is None:
        report.append({"stage": "plugins", "status": "unknown", "detail": "`claude` CLI unavailable — nothing changed"})
    else:
        report.append({"stage": "plugins", "status": "ok", "detail": "no user-scope stack plugins installed"})

    state = _mcp_entry_state()
    if state == "ours" and base:
        subprocess.run(
            [*base, "mcp", "remove", MCP_SERVER_NAME, "-s", "user"],
            capture_output=True,
            text=True,
            check=False,
        )
        report.append({"stage": "mcp", "status": "removed", "detail": "user-scope entry removed"})
    elif state == "foreign":
        report.append(
            {"stage": "mcp", "status": "skipped", "detail": "entry named 'agnes' is not ours — left in place"}
        )
    else:
        report.append({"stage": "mcp", "status": "ok", "detail": "no entry"})

    report.append(
        {"stage": "rails", "status": remove_rails_block(user_claude_md_path()), "detail": str(user_claude_md_path())}
    )
    report.append({"stage": "hook", "status": remove_user_session_hook(), "detail": "user-level SessionStart"})
    save_config({"global_scope": False, "global_hook": False})
    report.append({"stage": "flag", "status": "ok", "detail": "global_scope=false"})
    _print_report(report, as_json=as_json)


@global_app.command("status")
def status(
    as_json: bool = typer.Option(False, "--json", help="Emit a single JSON document."),
):
    """One row per artifact: ok | missing | drifted | … with the repair hint
    (`agnes global enable` re-runs convergence)."""
    from cli.commands.refresh_marketplace import (
        CLONE_DIR,
        _list_installed_agnes_plugins,
        _read_marketplace_plugin_versions,
    )

    cfg = load_config()
    artifacts: list[dict] = []

    manifest = _read_marketplace_plugin_versions() if (CLONE_DIR / ".git").is_dir() else None
    installed = _list_installed_agnes_plugins("user") if _claude_cmd() else None
    if manifest is None or installed is None:
        artifacts.append(
            {"artifact": "plugins", "state": "unknown", "detail": "marketplace clone or `claude` CLI unavailable"}
        )
    else:
        missing = sorted(set(manifest) - set(installed))
        drifted = sorted(n for n in manifest if n in installed and installed[n] != manifest[n])
        state = "ok" if not missing and not drifted else "drifted"
        artifacts.append(
            {
                "artifact": "plugins",
                "state": state,
                "detail": f"{len(installed)}/{len(manifest)} user-scope; missing: {missing or '—'}; stale: {drifted or '—'}",
            }
        )

    mcp_state, registered_cmd = _mcp_entry_info()
    if mcp_state == "ours" and registered_cmd and not Path(registered_cmd).exists():
        # Spec §6.3: "present + binary path exists" — a dead launcher path
        # is drift, repaired by the next convergence run.
        artifacts.append(
            {"artifact": "mcp", "state": "drifted", "detail": f"registered binary missing: {registered_cmd}"}
        )
    else:
        artifacts.append(
            {
                "artifact": "mcp",
                "state": {"ours": "ok", "foreign": "drifted", "absent": "missing"}[mcp_state],
                "detail": f"`claude mcp get {MCP_SERVER_NAME}` -> {mcp_state}",
            }
        )
    artifacts.append(
        {
            "artifact": "rails",
            "state": rails_block_state(user_claude_md_path(), load_global_rails()),
            "detail": str(user_claude_md_path()),
        }
    )
    hook_state = user_session_hook_state()
    if not cfg.get("global_hook", False) and hook_state == "missing":
        hook_state = "disabled"
    artifacts.append({"artifact": "hook", "state": hook_state, "detail": "user-level SessionStart"})
    artifacts.append(
        {
            "artifact": "flag",
            "state": "ok" if cfg.get("global_scope") else "missing",
            "detail": f"global_scope={bool(cfg.get('global_scope'))}, global_hook={bool(cfg.get('global_hook'))}",
        }
    )

    if as_json:
        typer.echo(json.dumps({"artifacts": artifacts}))
    else:
        for row in artifacts:
            typer.echo(f"  {row['artifact']:<8} {row['state']:<10} {row['detail']}")
        if any(r["state"] not in ("ok", "disabled") for r in artifacts):
            typer.echo("Repair: agnes global enable")
