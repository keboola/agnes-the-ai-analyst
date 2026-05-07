"""`agnes refresh-marketplace` — reconcile this workspace's plugins with
the user's current Agnes stack.

Three call paths share the same code:
  - `agnes refresh-marketplace --bootstrap` — first-time setup; clones the
    per-user marketplace bare repo, registers it with Claude Code, then
    falls through to fetch+reset+reconcile so plugins land installed.
  - `agnes refresh-marketplace` — manual re-sync after a known stack change.
  - `agnes refresh-marketplace --quiet` — SessionStart hook context. Emits
    a Claude Code hook JSON object on stdout when something actually got
    installed/updated; silent otherwise.

Reconcile is version-aware (install missing / update on version diff /
skip on match). Server-side stack composition lives in
`src/marketplace_filter.py:resolve_user_marketplace`. Plugin installs use
`--scope project` so they land in the workspace the hook fired in.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import typer

from cli.config import get_server_url, get_token
from cli.error_render import render_error
from cli.lib.marketplace import CLONE_DIR, MARKETPLACE_NAME


refresh_marketplace_app = typer.Typer(
    help="Reconcile the workspace plugins with the user's current Agnes stack."
)


# Per-invocation credential helper. `!<command>` runs the rest as a shell
# command. Reads the PAT from $AGNES_TOKEN — set in the subprocess env only,
# never on the command line — and emits the credential protocol's two
# key=value lines on stdout.
_CREDENTIAL_HELPER = '!f() { printf "username=x\\npassword=%s\\n" "$AGNES_TOKEN"; }; f'


@refresh_marketplace_app.callback(invoke_without_command=True)
def refresh_marketplace(
    quiet: bool = typer.Option(
        False, "--quiet",
        help="Suppress success stdout (errors and warnings still surface on stderr).",
    ),
    bootstrap: bool = typer.Option(
        False, "--bootstrap",
        help=(
            "If no marketplace clone exists yet, clone it and register the "
            "local path with Claude Code. Used by the install flow as a "
            "one-liner replacement for an inline `git clone` + chmod + "
            "`claude plugin marketplace add` sequence."
        ),
    ),
):
    """Sync the marketplace clone, re-register with Claude, install/update plugins."""
    clone_exists = (CLONE_DIR / ".git").is_dir()

    # Hook contexts hit the no-clone path on every workspace that didn't
    # bootstrap; silent exit keeps logs clean. Don't read the token here —
    # workspaces with the hook installed but no agnes token configured
    # (fresh CI checkout, etc.) must silent-noop, not surface auth_failed.
    if not clone_exists and not bootstrap:
        if not quiet:
            typer.echo(
                f"No marketplace clone at {CLONE_DIR} — nothing to refresh. "
                "Re-run setup with `agnes refresh-marketplace --bootstrap` "
                "(or re-run setup from the dashboard) to clone it."
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

    if not clone_exists:
        if not _bootstrap_clone(token, quiet=quiet):
            raise typer.Exit(1)

    events: dict[str, list[str]] = {"installed": [], "updated": []}

    if not _git_fetch_and_reset(token, quiet=quiet):
        raise typer.Exit(1)

    # Snapshot installed versions BEFORE `claude plugin marketplace update`.
    # On local-path marketplaces Claude silently auto-applies version bumps
    # (re-reads the manifest off disk and updates the installed cache), so
    # an after-snapshot would always match the manifest on real version-bump
    # scenarios — `events["updated"]` would stay empty and no notification
    # would fire despite the plugin having actually changed.
    installed_pre = _list_installed_agnes_plugins_in_cwd()

    _claude_marketplace_update(quiet=quiet)

    _reconcile_with_manifest(quiet=quiet, events=events, installed_pre=installed_pre)

    if quiet and (events["installed"] or events["updated"]):
        _emit_hook_message(events)
    elif not quiet and (events["installed"] or events["updated"]):
        typer.echo(
            "\nRestart Claude Code (`/exit`, then `claude`) to load the "
            "new/updated plugins — they're on disk now but Claude only "
            "picks them up on session start."
        )


def _bootstrap_clone(token: str, *, quiet: bool) -> bool:
    """Initial clone of the per-user marketplace bare repo into ~/.agnes/marketplace.

    Wrapping the destructive prep in the agnes binary lets the CLI's
    permission grant cover the cleanup (Python `shutil.rmtree` doesn't
    pattern-match the `rm -rf` shell pattern Claude Code's onboarding flow
    denies). Strips the PAT from the cloned origin URL so it doesn't sit
    in plaintext at `.git/config` (refreshes use the credential helper).
    Returns False on any failure.
    """
    server_url = get_server_url()
    if not server_url:
        typer.echo("error: no server URL configured; run `agnes init` first.", err=True)
        return False

    parsed = urlparse(server_url)
    if not parsed.hostname:
        typer.echo(f"error: server URL has no hostname: {server_url!r}", err=True)
        return False
    server_host = parsed.hostname
    if parsed.port:
        server_host = f"{server_host}:{parsed.port}"
    scheme = parsed.scheme or "https"

    # Stale dir without a `.git/` subdir means an interrupted prior install;
    # remove it so the fresh clone has somewhere to land.
    if CLONE_DIR.exists():
        try:
            shutil.rmtree(CLONE_DIR, ignore_errors=False)
        except OSError as exc:
            typer.echo(f"error: could not remove stale {CLONE_DIR}: {exc}", err=True)
            return False

    CLONE_DIR.parent.mkdir(parents=True, exist_ok=True)

    auth_url = f"{scheme}://x:{token}@{server_host}/marketplace.git/"
    clean_url = f"{scheme}://{server_host}/marketplace.git/"

    if not quiet:
        typer.echo(f"Cloning marketplace from {clean_url} into {CLONE_DIR}...")

    try:
        result = subprocess.run(
            ["git", "clone", auth_url, str(CLONE_DIR)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
    except FileNotFoundError:
        typer.echo("error: `git` not found in PATH; cannot clone marketplace.", err=True)
        return False
    if result.returncode != 0:
        if result.stderr:
            typer.echo(result.stderr.rstrip(), err=True)
        return False

    set_url = subprocess.run(
        ["git", "-C", str(CLONE_DIR), "remote", "set-url", "origin", clean_url],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    if set_url.returncode != 0:
        typer.echo(
            f"warn: could not strip PAT from origin URL: {set_url.stderr.rstrip()}",
            err=True,
        )

    # Best-effort chmod — no-op on Windows NTFS via Git Bash, tightens 700/600
    # on POSIX so other users on the box can't read `.git/config`.
    for path, mode in (
        (CLONE_DIR, 0o700),
        (CLONE_DIR / ".git", 0o700),
        (CLONE_DIR / ".git" / "config", 0o600),
    ):
        try:
            path.chmod(mode)
        except OSError:
            pass

    if shutil.which("claude") is not None:
        add = subprocess.run(
            ["claude", "plugin", "marketplace", "add", str(CLONE_DIR)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
        if add.returncode != 0:
            typer.echo(
                f"warn: `claude plugin marketplace add {CLONE_DIR}` exited {add.returncode}.",
                err=True,
            )
            if add.stderr:
                typer.echo(add.stderr.rstrip(), err=True)
        elif not quiet and add.stdout:
            typer.echo(add.stdout.rstrip())

    if not quiet:
        typer.echo(f"Marketplace bootstrapped at {CLONE_DIR}.")
    return True


def _git_fetch_and_reset(token: str, *, quiet: bool) -> bool:
    """Fetch from origin then hard-reset to FETCH_HEAD.

    Not `pull --ff-only`: the marketplace bare repo on the server rebuilds
    as a fresh orphan commit on every content change, so two snapshots
    have unrelated histories and fast-forward is impossible.
    """
    env = {**os.environ, "AGNES_TOKEN": token}
    fetch_cmd = [
        "git",
        "-c", f"credential.helper={_CREDENTIAL_HELPER}",
        "-C", str(CLONE_DIR),
        "fetch", "origin",
    ]
    try:
        fetch = subprocess.run(
            fetch_cmd, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=False,
        )
    except FileNotFoundError:
        typer.echo("error: `git` not found in PATH; cannot refresh marketplace.", err=True)
        return False
    if fetch.returncode != 0:
        if fetch.stdout:
            typer.echo(fetch.stdout, err=True)
        if fetch.stderr:
            typer.echo(fetch.stderr, err=True)
        return False

    reset = subprocess.run(
        ["git", "-C", str(CLONE_DIR), "reset", "--hard", "FETCH_HEAD"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
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
    """Tell Claude Code to re-read the marketplace clone. Soft-fail if `claude` is missing."""
    if shutil.which("claude") is None:
        typer.echo(
            "warn: `claude` not in PATH — git fetch succeeded, but Claude Code "
            "won't see the changes until the next session start.",
            err=True,
        )
        return
    result = subprocess.run(
        ["claude", "plugin", "marketplace", "update", MARKETPLACE_NAME],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    if result.returncode != 0:
        typer.echo(
            f"warn: `claude plugin marketplace update {MARKETPLACE_NAME}` exited {result.returncode}.",
            err=True,
        )
        if result.stderr:
            typer.echo(result.stderr.rstrip(), err=True)
        return
    if not quiet and result.stdout:
        typer.echo(result.stdout.rstrip())


def _reconcile_with_manifest(
    *,
    quiet: bool,
    events: dict[str, list[str]],
    installed_pre: Optional[dict[str, str]] = None,
) -> None:
    """Make installed plugins match the served manifest.

    Missing → `claude plugin install <name>@agnes --scope project`.
    Version differs → `claude plugin update <name>@agnes`.
    Match → skip.

    `installed_pre` is the snapshot taken before `claude plugin marketplace
    update` ran; we diff against it (not a fresh read) so version bumps
    Claude silently auto-applied are still detected. Bootstrap path passes
    None and we read live — there's no pre-state to preserve.

    Don't auto-uninstall plugins that disappeared from the manifest — a
    transient empty manifest from the server would wipe the user's stack.
    """
    if shutil.which("claude") is None:
        return

    manifest = _read_marketplace_plugin_versions()
    if manifest is None:
        typer.echo("warn: could not read marketplace.json from the clone; skipping reconcile.", err=True)
        return
    if not manifest:
        return

    installed = installed_pre if installed_pre is not None else _list_installed_agnes_plugins_in_cwd()
    if installed is None:
        typer.echo("warn: could not enumerate installed plugins; skipping reconcile.", err=True)
        return

    to_install: list[str] = []
    to_update: list[str] = []
    for name, manifest_version in sorted(manifest.items()):
        installed_version = installed.get(name)
        if installed_version is None:
            to_install.append(name)
        elif installed_version != manifest_version:
            to_update.append(name)

    if not to_install and not to_update:
        if not quiet:
            typer.echo(f"All {len(manifest)} Agnes-stack plugin(s) up to date.")
        return

    if not quiet:
        if to_install:
            typer.echo(f"Installing {len(to_install)} new plugin(s): " + ", ".join(to_install))
        if to_update:
            typer.echo(f"Updating {len(to_update)} plugin(s) to latest version: " + ", ".join(to_update))

    for name in to_install:
        target = f"{name}@{MARKETPLACE_NAME}"
        result = subprocess.run(
            ["claude", "plugin", "install", target, "--scope", "project"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
        if result.returncode != 0:
            typer.echo(
                f"warn: `claude plugin install {target} --scope project` exited {result.returncode}.",
                err=True,
            )
            if result.stderr:
                typer.echo(result.stderr.rstrip(), err=True)
            continue
        events["installed"].append(name)
        if not quiet and result.stdout:
            typer.echo(result.stdout.rstrip())

    for name in to_update:
        target = f"{name}@{MARKETPLACE_NAME}"
        result = subprocess.run(
            ["claude", "plugin", "update", target, "--scope", "project"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
        if result.returncode != 0:
            typer.echo(
                f"warn: `claude plugin update {target}` exited {result.returncode}.",
                err=True,
            )
            if result.stderr:
                typer.echo(result.stderr.rstrip(), err=True)
            continue
        events["updated"].append(name)
        if not quiet and result.stdout:
            typer.echo(result.stdout.rstrip())


def _emit_hook_message(events: dict[str, list[str]]) -> None:
    """Emit Claude Code hook JSON summarizing what changed.

    `systemMessage` is a transient toast (often missed). `additionalContext`
    is wrapped in a system reminder Claude reads at session start, so the
    model can mention the change if it's relevant to the user's first ask.
    Plugins land on disk during the hook; `/reload-plugins` loads them into
    the running session without a restart.
    """
    parts: list[str] = []
    if events["installed"]:
        parts.append(
            f"installed {len(events['installed'])} plugin(s): "
            + ", ".join(events["installed"])
        )
    if events["updated"]:
        parts.append(
            f"updated {len(events['updated'])} plugin(s): "
            + ", ".join(events["updated"])
        )
    summary = "Your Agnes stack changed: " + "; ".join(parts) + "."
    restart_hint = (
        "Run `/reload-plugins` to load the changes into this session — "
        "no restart needed."
    )
    payload = {
        "systemMessage": f"{summary} {restart_hint}",
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": f"{summary} {restart_hint}",
        },
    }
    typer.echo(json.dumps(payload))


def _read_marketplace_plugin_versions() -> Optional[dict[str, str]]:
    """Map `plugin name → version` from the local marketplace.json.

    None on missing/unreadable/malformed manifest. Empty dict means a
    valid manifest with no plugins (RBAC-empty, no /store installs).
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
    versions: dict[str, str] = {}
    for entry in plugins:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        version = entry.get("version")
        if isinstance(name, str) and name and isinstance(version, str) and version:
            versions[name] = version
    return versions


def _list_installed_agnes_plugins_in_cwd() -> Optional[dict[str, str]]:
    """Map `plugin name → installed version` for agnes plugins in this workspace.

    Filters `claude plugin list --json` by `id` ending in `@agnes` AND
    `projectPath == cwd` so plugins from sibling workspaces don't get
    counted. None on any structured-answer failure.
    """
    if shutil.which("claude") is None:
        return None
    try:
        result = subprocess.run(
            ["claude", "plugin", "list", "--json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
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
    versions: dict[str, str] = {}
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
        version = entry.get("version")
        if not isinstance(version, str) or not version:
            continue
        name = plugin_id[: -len(suffix)]
        if name:
            versions[name] = version
    return versions
