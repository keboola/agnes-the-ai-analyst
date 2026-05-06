"""`agnes refresh-marketplace` — keep the local Claude Code marketplace clone current.

The marketplace is registered with Claude Code as a *local clone path*
(see `setup_instructions._marketplace_block` and the design rationale
there). After the initial clone, server-side changes (new plugins added
to a user's RBAC view, version bumps, removed plugins) reach the user
only when this command runs:

  1. `git pull --ff-only` against the clone with PAT injection (per-pull
     credential helper, no persistent change to the user's git config —
     PAT stays out of `.git/config` URL at rest).
  2. `claude plugin marketplace update agnes` so Claude Code re-reads the
     refreshed manifest.
  3. Optionally (`--auto-upgrade`) iterates installed plugins from the
     `agnes` marketplace and runs `claude plugin update <name>@agnes` for
     each, picking up version bumps without manual prompting.

Used by:
- Manual invocation: `agnes refresh-marketplace` after a known
  marketplace change, or just to verify the clone is healthy.
- SessionStart hook: `agnes refresh-marketplace --quiet 2>/dev/null || true`
  runs every Claude Code session so users get marketplace changes without
  re-running setup.

Design choices:
- **No-op when the clone is missing.** Workspaces that don't use the
  marketplace (no plugin grants, or skipped step 5) shouldn't see hook
  noise. Exits 0 silently if `~/.agnes/marketplace/.git` isn't there.
- **No-op when claude isn't in PATH.** The git pull still runs, so the
  next session that does have claude available picks up the changes
  via Claude Code's natural startup re-read of the registered marketplace.
- **PAT injection only via env-var.** Never appears in argv, so `ps`
  on Linux/macOS or `tasklist /v` on Windows can't observe it. The
  one-shot credential helper is scoped to this single git invocation
  via `git -c credential.helper=...`, so unrelated git commands the user
  later runs don't see our helper or our token.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import typer

from cli.config import get_token
from cli.error_render import render_error
from cli.lib.marketplace import CLONE_DIR, MARKETPLACE_NAME


refresh_marketplace_app = typer.Typer(
    help="Refresh the Claude Code marketplace clone (git pull + claude marketplace update)."
)


# Per-invocation credential helper. `!<command>` syntax tells git to run
# the rest as a shell command (via MSYS sh on Windows, native sh elsewhere).
# The helper function reads the PAT from $AGNES_TOKEN — set in env for the
# subprocess only, never on the command line — and emits the credential
# protocol's two key=value lines on stdout. Git invokes the helper only on
# auth challenge from the remote, so the token is read at most once per pull.
_CREDENTIAL_HELPER = '!f() { printf "username=x\\npassword=%s\\n" "$AGNES_TOKEN"; }; f'


@refresh_marketplace_app.callback(invoke_without_command=True)
def refresh_marketplace(
    quiet: bool = typer.Option(
        False, "--quiet",
        help="Suppress success stdout (errors and warnings still surface on stderr).",
    ),
    auto_upgrade: bool = typer.Option(
        False, "--auto-upgrade",
        help=(
            "After refresh, iterate installed plugins from the agnes "
            "marketplace and run `claude plugin update <name>@agnes` on each."
        ),
    ),
):
    """Pull the marketplace clone, then nudge Claude Code to re-read it."""
    if not (CLONE_DIR / ".git").is_dir():
        # No clone → nothing to refresh. Hook contexts hit this on every
        # workspace that didn't go through step 5; silent exit keeps logs
        # clean. Manual invocation gets a hint so the user knows why.
        if not quiet:
            typer.echo(
                f"No marketplace clone at {CLONE_DIR} — nothing to refresh. "
                "Re-run setup from the dashboard if you want to install plugins."
            )
        raise typer.Exit(0)

    token = get_token()
    if not token:
        typer.echo(
            render_error(0, {"detail": {
                "kind": "auth_failed",
                "hint": "No token. Run: agnes auth import-token --token <PAT>",
            }}),
            err=True,
        )
        raise typer.Exit(1)

    pull_ok = _git_pull(token, quiet=quiet)
    if not pull_ok:
        # Pull failure already surfaced via stderr; exit non-zero so hook
        # consumers can detect it (the hook itself swallows non-zero via
        # `|| true`, but a manual `agnes refresh-marketplace` should fail).
        raise typer.Exit(1)

    _claude_marketplace_update(quiet=quiet)

    if auto_upgrade:
        _claude_auto_upgrade(quiet=quiet)


def _git_pull(token: str, *, quiet: bool) -> bool:
    """Run `git pull --ff-only` in CLONE_DIR with PAT-bearing credential helper.

    Returns True on success (exit 0), False on any failure. Stderr from git
    is forwarded so the operator can see the real cause (network, ref-not-fast-forward, ...).
    """
    env = {**os.environ, "AGNES_TOKEN": token}
    cmd = [
        "git",
        "-c", f"credential.helper={_CREDENTIAL_HELPER}",
        "-C", str(CLONE_DIR),
        "pull", "--ff-only",
    ]
    try:
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        # git itself missing — should be impossible if step 4 (preflight)
        # ran, but guard anyway.
        typer.echo("error: `git` not found in PATH; cannot refresh marketplace.", err=True)
        return False

    if result.returncode != 0:
        # Forward git's diagnostic verbatim; it's actionable as-is.
        if result.stdout:
            typer.echo(result.stdout, err=True)
        if result.stderr:
            typer.echo(result.stderr, err=True)
        return False

    if not quiet:
        # `git pull` already prints "Already up to date." or a list of
        # changed files; just relay it.
        if result.stdout:
            typer.echo(result.stdout.rstrip())
    return True


def _claude_marketplace_update(*, quiet: bool) -> None:
    """Tell Claude Code to re-read the marketplace clone.

    Soft-fail: if `claude` isn't in PATH (yet — e.g. install order on a
    fresh machine), warn but continue. The git pull happened, so the next
    Claude Code session that does have it picks up the changes during its
    natural marketplace re-read on startup.
    """
    if shutil.which("claude") is None:
        typer.echo(
            "warn: `claude` not in PATH — git pull succeeded, but Claude Code "
            "won't see the changes until the next session start.",
            err=True,
        )
        return
    cmd = ["claude", "plugin", "marketplace", "update", MARKETPLACE_NAME]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        typer.echo(
            f"warn: `claude plugin marketplace update {MARKETPLACE_NAME}` "
            f"exited {result.returncode}.",
            err=True,
        )
        if result.stderr:
            typer.echo(result.stderr.rstrip(), err=True)
        return
    if not quiet and result.stdout:
        typer.echo(result.stdout.rstrip())


def _claude_auto_upgrade(*, quiet: bool) -> None:
    """`claude plugin update <name>@agnes` for each installed agnes plugin.

    Best-effort. If `claude plugin list --json` doesn't return parseable
    JSON, warn and bail rather than fail the command — the manifest update
    already happened, so the user just doesn't get auto-version-bump.
    """
    if shutil.which("claude") is None:
        # Already warned by _claude_marketplace_update; don't double-print.
        return
    plugins = _list_installed_agnes_plugins()
    if plugins is None:
        typer.echo(
            "warn: could not enumerate installed plugins for --auto-upgrade; "
            "skipping. Plugins from the agnes marketplace can be updated "
            "manually via `claude plugin update <name>@agnes`.",
            err=True,
        )
        return
    if not plugins:
        if not quiet:
            typer.echo("No installed plugins from the agnes marketplace; nothing to upgrade.")
        return
    for name in plugins:
        cmd = ["claude", "plugin", "update", f"{name}@{MARKETPLACE_NAME}"]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            typer.echo(
                f"warn: `claude plugin update {name}@{MARKETPLACE_NAME}` "
                f"exited {result.returncode}.",
                err=True,
            )
            if result.stderr:
                typer.echo(result.stderr.rstrip(), err=True)
            continue
        if not quiet and result.stdout:
            typer.echo(result.stdout.rstrip())


def _list_installed_agnes_plugins() -> Optional[list[str]]:
    """Best-effort enumeration of plugins installed from the agnes marketplace.

    Returns None if we can't get a structured answer (claude missing, --json
    flag unsupported, or output not parseable). The caller treats None as
    "warn and skip"; an empty list is a definite "nothing to do".
    """
    cmd = ["claude", "plugin", "list", "--json"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    # Best-guess shape — `claude plugin list --json` returns a list of
    # objects with at least `name` and `marketplace` (or `source`) fields.
    # If the schema differs, we treat it as unknown and return None so the
    # caller falls back to the warning path.
    names: list[str] = []
    if not isinstance(payload, list):
        return None
    for entry in payload:
        if not isinstance(entry, dict):
            return None
        marketplace = entry.get("marketplace") or entry.get("source")
        name = entry.get("name")
        if marketplace == MARKETPLACE_NAME and isinstance(name, str):
            names.append(name)
    return names
