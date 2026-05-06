"""`agnes refresh-marketplace` — keep the local Claude Code marketplace clone current.

The marketplace is registered with Claude Code as a *local clone path*
(see `setup_instructions._marketplace_block` and the design rationale
there). After the initial clone, server-side changes (new plugins added
to a user's RBAC view, version bumps, removed plugins) reach the user
only when this command runs:

  1. ``git fetch`` against the clone with PAT injection (per-pull
     credential helper, no persistent change to the user's git config —
     PAT stays out of `.git/config` URL at rest), then ``git reset
     --hard FETCH_HEAD``. The bare repo on the server is rebuilt as a
     fresh orphan commit on every content change (see
     `app/marketplace_server/git_backend.py:build_bare_repo` —
     `commit.parents = []`), so a normal `pull --ff-only` would hit
     "Not possible to fast-forward" the moment the server-side manifest
     changes. We treat the local clone as a snapshot mirror, not a
     history we own.
  2. ``claude plugin marketplace update agnes`` so Claude Code re-reads
     the refreshed manifest.
  3. **Auto-install missing plugins** — read the now-fresh
     ``.claude-plugin/marketplace.json`` from the clone, list the plugins
     installed in this workspace (filtered by ``projectPath``), and run
     ``claude plugin install <name>@agnes --scope project`` for any
     plugin in the marketplace that isn't yet installed here. This is
     default behavior because the agnes marketplace IS the admin-curated
     plugin set for this user — RBAC has already decided what they get,
     so propagating that decision into the workspace mirrors the intent.
  4. Optionally (``--auto-upgrade``) iterate installed agnes-marketplace
     plugins and run ``claude plugin update <name>@agnes`` for each,
     picking up version bumps without manual prompting.

Used by:
- Manual invocation: ``agnes refresh-marketplace`` after a known
  marketplace change, or just to verify the clone is healthy.
- SessionStart hook: ``agnes refresh-marketplace --quiet 2>/dev/null || true``
  runs every Claude Code session so users get marketplace changes (new
  plugins auto-installed, removed plugins surfaced, optionally auto-
  upgraded versions) without re-running setup.

Design choices:
- **No-op when the clone is missing.** Workspaces that don't use the
  marketplace (no plugin grants, or skipped step 5) shouldn't see hook
  noise. Exits 0 silently if `~/.agnes/marketplace/.git` isn't there.
- **No-op when claude isn't in PATH.** The git fetch+reset still runs,
  so the next session that does have claude available picks up the
  changes via Claude Code's natural startup re-read of the registered
  marketplace. Auto-install is also skipped (it requires `claude`).
- **PAT injection only via env-var.** Never appears in argv, so `ps`
  on Linux/macOS or `tasklist /v` on Windows can't observe it. The
  one-shot credential helper is scoped to this single git invocation
  via `git -c credential.helper=...`, so unrelated git commands the user
  later runs don't see our helper or our token.
- **Auto-install scope: project.** Mirrors the initial setup-instructions
  install line (`--scope project`), so plugins land in the workspace
  that the user is currently in (where the SessionStart hook fires).
  Workspace match is enforced by filtering `claude plugin list --json`
  on `projectPath == cwd`; without that, a plugin installed in
  workspace A would mistakenly count as already-installed in B.
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
    help="Refresh the Claude Code marketplace clone (git fetch + claude marketplace update + auto-install)."
)


# Per-invocation credential helper. `!<command>` syntax tells git to run
# the rest as a shell command (via MSYS sh on Windows, native sh elsewhere).
# The helper function reads the PAT from $AGNES_TOKEN — set in env for the
# subprocess only, never on the command line — and emits the credential
# protocol's two key=value lines on stdout. Git invokes the helper only on
# auth challenge from the remote, so the token is read at most once per fetch.
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
            "After refresh + auto-install, iterate already-installed plugins "
            "from the agnes marketplace and run `claude plugin update <name>@agnes` "
            "on each to pick up version bumps."
        ),
    ),
):
    """Sync the marketplace clone, re-register with Claude, and install any new grants."""
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

    fetch_ok = _git_fetch_and_reset(token, quiet=quiet)
    if not fetch_ok:
        # Fetch/reset failure already surfaced via stderr; exit non-zero so
        # hook consumers can detect it (the hook itself swallows non-zero via
        # `|| true`, but a manual `agnes refresh-marketplace` should fail).
        raise typer.Exit(1)

    _claude_marketplace_update(quiet=quiet)

    # Auto-install runs after marketplace update so claude knows about any
    # newly-listed plugins before we ask it to install them.
    _auto_install_missing(quiet=quiet)

    if auto_upgrade:
        _claude_auto_upgrade(quiet=quiet)


def _git_fetch_and_reset(token: str, *, quiet: bool) -> bool:
    """Fetch from origin then hard-reset to FETCH_HEAD.

    Why not `pull --ff-only`? The marketplace bare repo on the server
    rebuilds as a brand-new orphan commit (`commit.parents = []`) on
    every content change — see `app/marketplace_server/git_backend.py:
    build_bare_repo`. Two snapshots have unrelated histories, so a
    fast-forward is mathematically impossible. We treat the local clone
    as a snapshot mirror: whatever the server has now becomes our local
    HEAD, no merge attempted.

    Returns True on success, False on any failure. Stderr from git is
    forwarded so the operator can see the real cause.
    """
    env = {**os.environ, "AGNES_TOKEN": token}
    fetch_cmd = [
        "git",
        "-c", f"credential.helper={_CREDENTIAL_HELPER}",
        "-C", str(CLONE_DIR),
        "fetch", "origin",
    ]
    try:
        fetch = subprocess.run(fetch_cmd, env=env, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        typer.echo("error: `git` not found in PATH; cannot refresh marketplace.", err=True)
        return False
    if fetch.returncode != 0:
        if fetch.stdout:
            typer.echo(fetch.stdout, err=True)
        if fetch.stderr:
            typer.echo(fetch.stderr, err=True)
        return False

    reset_cmd = ["git", "-C", str(CLONE_DIR), "reset", "--hard", "FETCH_HEAD"]
    reset = subprocess.run(reset_cmd, capture_output=True, text=True, check=False)
    if reset.returncode != 0:
        if reset.stdout:
            typer.echo(reset.stdout, err=True)
        if reset.stderr:
            typer.echo(reset.stderr, err=True)
        return False

    if not quiet and reset.stdout:
        typer.echo(reset.stdout.rstrip())
    return True


def _claude_marketplace_update(*, quiet: bool) -> None:
    """Tell Claude Code to re-read the marketplace clone.

    Soft-fail: if `claude` isn't in PATH (yet — e.g. install order on a
    fresh machine), warn but continue. The fetch+reset happened, so the
    next Claude Code session that does have it picks up the changes
    during its natural marketplace re-read on startup.
    """
    if shutil.which("claude") is None:
        typer.echo(
            "warn: `claude` not in PATH — git fetch succeeded, but Claude Code "
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


def _auto_install_missing(*, quiet: bool) -> None:
    """Install plugins listed in the agnes marketplace that aren't yet in
    this workspace.

    The agnes marketplace is admin-curated per RBAC — every plugin in the
    served manifest is one that the admin granted to this user. Mirroring
    that grant set into the user's workspace is the propagation step
    that makes "admin adds a plugin" → "user has it on next session".

    Idempotent: re-installing an already-installed plugin is a no-op
    (Claude Code reports "already installed"), so we don't gate on a
    delta calculation — but we DO compute the delta to keep the log
    output minimal in the common case (nothing new to install).
    """
    if shutil.which("claude") is None:
        # _claude_marketplace_update already warned; don't double-print.
        return

    available = _read_marketplace_plugin_names()
    if available is None:
        typer.echo(
            "warn: could not read marketplace.json from the clone; "
            "skipping auto-install.",
            err=True,
        )
        return
    if not available:
        # Empty marketplace (RBAC granted nothing). Nothing to install.
        return

    installed = _list_installed_agnes_plugins_in_cwd()
    if installed is None:
        typer.echo(
            "warn: could not enumerate installed plugins; "
            "skipping auto-install.",
            err=True,
        )
        return

    missing = sorted(available - installed)
    if not missing:
        if not quiet:
            typer.echo(f"All {len(available)} agnes-marketplace plugin(s) already installed.")
        return

    if not quiet:
        typer.echo(
            f"Installing {len(missing)} new plugin(s) from agnes marketplace: "
            + ", ".join(missing)
        )

    for name in missing:
        target = f"{name}@{MARKETPLACE_NAME}"
        cmd = ["claude", "plugin", "install", target, "--scope", "project"]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            typer.echo(
                f"warn: `claude plugin install {target} --scope project` "
                f"exited {result.returncode}.",
                err=True,
            )
            if result.stderr:
                typer.echo(result.stderr.rstrip(), err=True)
            continue
        if not quiet and result.stdout:
            typer.echo(result.stdout.rstrip())


def _claude_auto_upgrade(*, quiet: bool) -> None:
    """`claude plugin update <name>@agnes` for each installed agnes plugin
    in this workspace.

    Best-effort. If the plugin list query fails, warn and bail rather
    than fail the command — the manifest update + auto-install already
    happened, so the user just doesn't get auto-version-bump this run.
    """
    if shutil.which("claude") is None:
        return
    installed = _list_installed_agnes_plugins_in_cwd()
    if installed is None:
        typer.echo(
            "warn: could not enumerate installed plugins for --auto-upgrade; "
            "skipping. Plugins from the agnes marketplace can be updated "
            "manually via `claude plugin update <name>@agnes`.",
            err=True,
        )
        return
    if not installed:
        if not quiet:
            typer.echo("No installed plugins from the agnes marketplace; nothing to upgrade.")
        return
    for name in sorted(installed):
        target = f"{name}@{MARKETPLACE_NAME}"
        cmd = ["claude", "plugin", "update", target]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            typer.echo(
                f"warn: `claude plugin update {target}` exited {result.returncode}.",
                err=True,
            )
            if result.stderr:
                typer.echo(result.stderr.rstrip(), err=True)
            continue
        if not quiet and result.stdout:
            typer.echo(result.stdout.rstrip())


def _read_marketplace_plugin_names() -> Optional[set[str]]:
    """Return the set of plugin names listed in the local marketplace.json.

    Returns None if the file is missing/unreadable/malformed (caller treats
    that as "warn and skip"). Returns an empty set when the manifest is
    valid but lists no plugins (RBAC-empty user — caller treats that as
    "nothing to do, no warning").
    """
    manifest_path = CLONE_DIR / ".claude-plugin" / "marketplace.json"
    try:
        raw = manifest_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    plugins = payload.get("plugins")
    if not isinstance(plugins, list):
        return None
    names: set[str] = set()
    for entry in plugins:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if isinstance(name, str) and name:
            names.add(name)
    return names


def _list_installed_agnes_plugins_in_cwd() -> Optional[set[str]]:
    """Names of installed agnes-marketplace plugins in the current workspace.

    Best-effort enumeration via `claude plugin list --json`. The output
    is a flat list across all workspaces, so we filter by:
      - id ends with `@agnes` (parses out the marketplace from the id field)
      - projectPath equals current working directory (so plugins from
        sibling workspaces don't get counted as already-installed here)

    Returns None if we can't get a structured answer (claude missing,
    --json flag unsupported, output not parseable). Empty set means
    "nothing currently installed in this workspace from agnes".
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
    if not isinstance(payload, list):
        return None

    cwd = Path.cwd().resolve()
    suffix = f"@{MARKETPLACE_NAME}"
    names: set[str] = set()
    for entry in payload:
        if not isinstance(entry, dict):
            return None
        plugin_id = entry.get("id", "")
        if not isinstance(plugin_id, str) or not plugin_id.endswith(suffix):
            continue
        project_path = entry.get("projectPath")
        if not isinstance(project_path, str):
            continue
        try:
            if Path(project_path).resolve() != cwd:
                continue
        except OSError:
            continue
        # Strip the @agnes suffix to get the plain name.
        name = plugin_id[: -len(suffix)]
        if name:
            names.add(name)
    return names
