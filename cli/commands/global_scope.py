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
import time
from contextlib import contextmanager
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
    """Absolute path of the agnes launcher (spec §6.1 step 3).

    Deliberately NOT `.resolve()`d. `shutil.which` already returns an absolute
    path, and resolving it additionally dereferences the symlink `uv tool
    install` / `pipx` put in `~/.local/bin` — registering the MCP entry against
    an executable inside the managed tool venv instead of the stable shim. That
    is precisely the path shape the dead-launcher repair branch below exists to
    clean up after: reinstalling or relocating the venv moves the target while
    the shim stays put, so recording the shim makes the entry survive on its
    own (Devin on #1184).

    `sys.argv[0]` IS resolved, because it is the fallback for "agnes is not on
    PATH" and may be relative to the cwd, which the MCP entry cannot be.
    """
    found = shutil.which("agnes")
    if found:
        return str(Path(found))
    return str(Path(sys.argv[0]).resolve())


def _mcp_entry_info() -> tuple[str, Optional[str]]:
    """(`'ours' | 'foreign' | 'absent'`, registered command path or None).

    `claude mcp get agnes` prints "Scope: <scope>", "Command: <path>" and
    "Args: <args>".

    SCOPE FIRST. `mcp get` takes no scope flag — it resolves the name across
    every scope and answers with whichever it finds. This layer only ever
    writes user scope (`--scope user` / `-s user` below), so a hit at project
    or local scope is not our entry and must not be mistaken for one: an
    engineer with a per-project `agnes` entry in some repo's `.mcp.json`
    would otherwise make `enable` believe the all-repositories entry was
    already registered and skip creating it, leaving the global layer
    silently without its MCP server (Devin on #1184). Entries at different
    scopes coexist, so a non-user hit reads as "absent" here.

    Then ours == the command's basename is the agnes launcher AND the args
    are exactly the `mcp` subcommand. The output header always contains the
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
    scope: Optional[str] = None
    for line in (result.stdout or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("Command:"):
            command = stripped[len("Command:") :].strip() or None
        elif stripped.startswith("Args:"):
            args = stripped[len("Args:") :].strip()
        elif stripped.startswith("Scope:"):
            scope = stripped[len("Scope:") :].strip()
    # `Scope: User config (available in all your projects)` — matched on the
    # leading words so the parenthetical gloss is free to change. A `claude`
    # too old to print the line at all leaves `scope` None; treat that as
    # absent rather than assuming user scope, since assuming would resurrect
    # exactly the skip-the-registration bug.
    if scope is None or not scope.lower().startswith("user config"):
        return "absent", None
    is_ours = (
        command is not None
        and Path(command).name.lower() in ("agnes", "agnes.exe", "agnes.cmd")
        and _args_are_mcp(args)
    )
    return ("ours" if is_ours else "foreign"), command


def _args_are_mcp(args: Optional[str]) -> bool:
    """Whether the rendered args are the `mcp` subcommand and nothing else.

    Tolerant of how they are printed, because the alternative fails in the
    worst direction. `mcp get` has no `--json`, so this is screen-scraping: if
    a future `claude` renders args as `["mcp"]`, quotes them, or folds them
    into `Command:` and drops the line, an exact `== "mcp"` test flips EVERY
    enrolled machine to `foreign` at once — convergence starts reporting
    `skipped … re-run with --force`, `status` reports `drifted`, and `disable`
    declines to remove the entry it created itself (Devin on #1184).

    Strictness lives in the binary check above, which is the real
    discriminator: an entry named `agnes` whose command IS the agnes launcher
    is ours whatever the args rendering. Absent args count — that is the
    fold-into-Command shape — but a second argument does not, so a genuinely
    different invocation still reads as foreign.
    """
    if args is None:
        return True
    tokens = [t.strip().strip("\"'") for t in args.strip().strip("[]").split(",")]
    tokens = [t for t in (tok for tok in tokens) if t]
    if len(tokens) == 1 and " " in tokens[0]:
        tokens = tokens[0].split()
    return tokens in ([], ["mcp"])


def _mcp_entry_state() -> str:
    return _mcp_entry_info()[0]


@contextmanager
def _convergence_lock_or_exit(*, as_json: bool):
    """Hold the SAME lock `agnes update` holds, for the duration of a manual
    `enable` / `disable`.

    Both commands and `agnes update`'s `global` step reach `run_convergence`,
    which git-fetches the shared `~/.agnes/marketplace` clone and rewrites
    `~/.claude/settings.json` and `~/.claude/CLAUDE.md`. Once the user-level
    hook is installed, a detached `agnes update` fires from EVERY repository
    the user opens, so racing a hand-typed `agnes global enable` is ordinary
    rather than theoretical. The atomic renames rule out torn files; they do
    not rule out last-writer-wins dropping the other side's hook entry, or a
    `git reset --hard` interleaving with a plugin reconcile (Devin on #1184).

    `agnes update` treats "someone else holds it" as skip-quietly, which is
    right for a background convergence. Here it is not: the user typed the
    command and a silent no-op would look like it worked. So this refuses,
    says why, and leaves retrying to them.

    `_step_global` already holds this lock when it calls `run_convergence`,
    which is why the lock lives in the COMMANDS rather than in the shared
    engine — putting it there would have the update path skip its own step.
    """
    from cli.config import _config_dir
    from cli.lib.push_lock import acquire_path_or_skip

    with acquire_path_or_skip(_config_dir() / "update.lock") as lock:
        if lock is None:
            msg = "another `agnes update` or `agnes global` run holds the convergence lock — retry in a moment."
            if as_json:
                typer.echo(json.dumps({"report": [{"stage": "lock", "status": "busy", "detail": msg}]}))
            else:
                typer.echo(f"error: {msg}", err=True)
            raise typer.Exit(1)
        yield


#: How long a marketplace clone counts as freshly fetched. Long enough that a
#: burst of session starts does not fetch repeatedly, short enough that a stack
#: change an admin made this morning reaches the user today.
_CLONE_FRESH_SECONDS = 6 * 3600


def _clone_is_stale(clone_dir: Path) -> bool:
    """True when nothing has fetched this clone recently.

    Replaces a `workspace_root`-is-unset gate, which asked the wrong question.
    That gate assumed anyone WITH an anchored workspace has their clone kept
    current by the workspace marketplace step — but an engineer who anchored a
    workspace once and then works all day in other repositories, the exact
    persona this layer exists for, never runs that step. Their user-scope
    stack silently froze at whatever the clone last held (Devin on #1184).

    Freshness is the question actually being asked, and it self-balances
    across personas: `_step_marketplace` runs BEFORE the global step in
    `agnes update`, so a workspace user reaches here with a just-fetched clone
    and skips, while a global-only user reaches here with a stale one and
    refreshes. Neither fetches twice.

    `FETCH_HEAD` is written by every `git fetch`; a clone that has only ever
    been cloned has none, so `HEAD` stands in. Neither readable — treat as
    stale and let the refresh decide.
    """
    for name in ("FETCH_HEAD", "HEAD"):
        try:
            age = time.time() - (clone_dir / ".git" / name).stat().st_mtime
        except OSError:
            continue
        return age > _CLONE_FRESH_SECONDS
    return True


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
    elif _clone_is_stale(CLONE_DIR):
        # The workspace marketplace step is the only other place that fetches
        # the clone, so when it has not run recently, stack changes stay
        # frozen at whatever the clone last held. Do the cheap drift-check +
        # refresh here.
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
        # CONVERGE, don't merely abstain. Treating `--no-hook` as "do not
        # install" left a hook a previous `enable` had installed firing in
        # every repository, while the config said `global_hook: false` and
        # `status` reported it `ok` — the flag, the report and reality all
        # disagreeing, with `agnes global disable` (which tears down the whole
        # layer) as the only way out (Devin on #1184).
        outcome = remove_user_session_hook()
        report.append(
            {
                "stage": "hook",
                "status": "removed" if outcome == "removed" else "skipped",
                "detail": "--no-hook / global_hook=false"
                + ("; removed the previously installed hook" if outcome == "removed" else ""),
            }
        )


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
    with _convergence_lock_or_exit(as_json=as_json), sink:
        run_convergence(want_hook=not no_hook, force=force, report=report)
    save_config({"global_scope": True, "global_hook": not no_hook})
    report.append({"stage": "flag", "status": "ok", "detail": "global_scope=true"})

    # `run_convergence` records failures rather than raising (its docstring is
    # explicit), so the per-stage `error` rows have to reach the exit code
    # here or a half-installed layer exits 0 under the line "Global layer
    # enabled" — and the engineer only finds out later, when the data tools
    # are not there (Devin on #1184).
    #
    # The FLAG is still written, unlike in `disable`. The two are not
    # symmetric: leaving the layer marked on is what keeps `agnes update`
    # converging it, so the pieces that failed get retried on their own. What
    # must not survive is the claim of success.
    failed = [r for r in report if r.get("status") == "error"]
    _print_report(
        report,
        as_json=as_json,
        trailer=(
            "Global layer enabled — restart Claude Code sessions to pick it up."
            if not failed
            else (
                "Global layer PARTIALLY enabled — "
                + ", ".join(f"{r['stage']} failed" for r in failed)
                + ". It stays enabled so `agnes update` keeps retrying; re-run "
                "`agnes global enable` once the cause is fixed."
            )
        ),
    )
    if failed:
        raise typer.Exit(1)


@global_app.command("disable")
def disable(
    as_json: bool = typer.Option(False, "--json", help="Emit a single JSON report."),
):
    """Revert exactly what `enable` wrote. Marketplace registration and the
    clone stay (the workspace flow uses them too, spec §6.2)."""
    report: list[dict] = []

    from cli.commands.refresh_marketplace import _list_installed_agnes_plugins

    # Every artifact this layer installed keeps loading in EVERY repository
    # until it is actually removed, so the off flag may only be written once
    # the removals really happened. Anything left behind is recorded here and
    # keeps the layer marked enabled — see the flag decision at the end.
    left_behind: list[str] = []

    with _convergence_lock_or_exit(as_json=as_json):
        base = _claude_cmd()
        installed = _list_installed_agnes_plugins("user") if base else None
        if installed:
            removed: list[str] = []
            failed: list[str] = []
            for name in sorted(installed):
                result = subprocess.run(
                    [*base, "plugin", "uninstall", f"{name}@agnes", "--scope", "user"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
                (removed if result.returncode == 0 else failed).append(name)
            if failed:
                left_behind.append(f"plugins: {', '.join(failed)}")
            report.append(
                {
                    "stage": "plugins",
                    "status": "error" if failed else "removed",
                    "detail": (f"removed {', '.join(removed) or 'none'}; FAILED {', '.join(failed)}")
                    if failed
                    else (", ".join(removed) or "none"),
                }
            )
        elif installed is None:
            left_behind.append("plugins: could not be listed")
            report.append(
                {"stage": "plugins", "status": "unknown", "detail": "`claude` CLI unavailable — nothing changed"}
            )
        else:
            report.append({"stage": "plugins", "status": "ok", "detail": "no user-scope stack plugins installed"})

        state = _mcp_entry_state()
        if state == "ours" and base:
            rm = subprocess.run(
                [*base, "mcp", "remove", MCP_SERVER_NAME, "-s", "user"],
                capture_output=True,
                text=True,
                check=False,
            )
            if rm.returncode != 0:
                left_behind.append("mcp: entry could not be removed")
            report.append(
                {
                    "stage": "mcp",
                    "status": "removed" if rm.returncode == 0 else "error",
                    "detail": "user-scope entry removed" if rm.returncode == 0 else f"remove exited {rm.returncode}",
                }
            )
        elif state == "ours" and not base:
            # `_mcp_entry_state` needs `claude` too, so this is unreachable today;
            # keep the arm so a future probe change cannot make it fall through to
            # the "no entry" branch and silently under-report.
            left_behind.append("mcp: `claude` CLI unavailable")
            report.append({"stage": "mcp", "status": "unknown", "detail": "`claude` CLI unavailable"})
        elif state == "foreign":
            # Not ours to remove, so not something disable owes the user — the
            # layer we installed is gone either way.
            report.append(
                {"stage": "mcp", "status": "skipped", "detail": "entry named 'agnes' is not ours — left in place"}
            )
        else:
            report.append({"stage": "mcp", "status": "ok", "detail": "no entry"})

        rails = remove_rails_block(user_claude_md_path())
        if rails == "skipped_malformed":
            left_behind.append("rails: block left in ~/.claude/CLAUDE.md")
        report.append({"stage": "rails", "status": rails, "detail": str(user_claude_md_path())})

        hook = remove_user_session_hook()
        if hook == "skipped_malformed":
            left_behind.append("hook: user-level SessionStart left in settings.json")
        report.append({"stage": "hook", "status": hook, "detail": "user-level SessionStart"})

        # The flag is the LAST thing written, and only when nothing was left
        # behind. Flipping it regardless was the real hazard: the skills and the
        # tool entry keep loading in every repository the user opens, while the
        # config says the layer is off — so `agnes update` stops converging it and
        # nothing is left that would ever clean them up. A partial disable is
        # therefore still "enabled": the layer stays managed, and re-running
        # `agnes global disable` finishes the job (Devin on #1184).
        if left_behind:
            report.append(
                {
                    "stage": "flag",
                    "status": "skipped",
                    "detail": ("left enabled — still installed: " + "; ".join(left_behind) + ". Fix, then re-run."),
                }
            )
            _print_report(report, as_json=as_json)
            raise typer.Exit(1)

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
