"""`agnes refresh-marketplace` — reconcile this workspace's plugins with
the user's current Agnes stack.

Three call paths share the same code:
  - `agnes refresh-marketplace --bootstrap` — first-time setup; clones the
    per-user marketplace bare repo, registers it with Claude Code, then
    falls through to fetch+reset+reconcile so plugins land installed.
  - `agnes refresh-marketplace` — manual re-sync after a known stack
    change. This is what the `/update-agnes-plugins` slash command runs
    inside Claude Code so the user sees install/update progress in the
    transcript.
  - `agnes refresh-marketplace --check` — SessionStart hook context.
    Lightweight detector: `git ls-remote origin HEAD` only (no fetch,
    no reset, no plugin install/update side effects), compares the
    remote HEAD SHA against the local `HEAD` SHA, emits a Claude Code
    hook JSON message pointing the user at `/update-agnes-plugins`
    when they differ. Silent otherwise. ls-remote is ~0.5–1 s vs ~8 s
    for fetch — matters because every Claude Code session start in
    every workspace fires this hook.

Reconcile (default + --bootstrap paths) is version-aware (install
missing / update on version diff / skip on match). Server-side stack
composition lives in `src/marketplace_filter.py:resolve_user_marketplace`.
Plugin installs use `--scope project` so they land in the workspace the
caller invoked from.
"""

from __future__ import annotations

import json
import os
import re
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
    check: bool = typer.Option(
        False, "--check",
        help=(
            "Detect-only mode for the SessionStart hook. Runs "
            "`git ls-remote origin HEAD` and compares the returned SHA "
            "with local HEAD. When they differ, emits a Claude Code "
            "hook JSON message hinting the user at "
            "`/update-agnes-plugins`. No `git fetch`, no `git reset`, "
            "no plugin install/update side effects — fast, invisible "
            "when nothing changed, fully recoverable interactively "
            "via the slash command."
        ),
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
    if check and bootstrap:
        typer.echo(
            "error: --check and --bootstrap are mutually exclusive.",
            err=True,
        )
        raise typer.Exit(2)

    clone_exists = (CLONE_DIR / ".git").is_dir()

    # Hook contexts hit the no-clone path on every workspace that didn't
    # bootstrap; silent exit keeps logs clean. Don't read the token here —
    # workspaces with the hook installed but no agnes token configured
    # (fresh CI checkout, etc.) must silent-noop, not surface auth_failed.
    if not clone_exists and not bootstrap:
        if not check:
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
        if not _bootstrap_clone(token):
            raise typer.Exit(1)
    elif bootstrap:
        # Clone survived but Claude Code's registry may not list `agnes`
        # — fresh Claude Code install on the same box, manual
        # `claude plugin marketplace remove`, or an earlier interrupted
        # bootstrap that warn-and-continued past the add step. The
        # `--bootstrap` contract is "after this returns, plugins work";
        # ensure the registration is current before we fall through to
        # `claude plugin marketplace update agnes`, which would otherwise
        # fail with "Marketplace 'agnes' not found".
        if not _ensure_marketplace_registered():
            raise typer.Exit(1)

    # --check: lightweight detector. Don't fetch+reset, don't reconcile
    # plugins — that's the slash command's job. Just check whether the
    # remote has new content and tell the user if so. `git ls-remote`
    # fetches one line of text (the remote HEAD ref) instead of all
    # git objects — ~0.5–1 s vs ~8 s for `git fetch`.
    if check:
        remote_sha = _remote_head_sha(token)
        if remote_sha is None:
            raise typer.Exit(1)
        local_sha = _local_head_sha()
        if local_sha is not None and local_sha != remote_sha:
            _emit_check_hook_message()
        raise typer.Exit(0)

    events: dict[str, list[str]] = {"installed": [], "updated": [], "enabled": []}

    if not _git_fetch_and_reset(token):
        raise typer.Exit(1)

    # Snapshot installed versions BEFORE `claude plugin marketplace update`.
    # On local-path marketplaces Claude silently auto-applies version bumps
    # (re-reads the manifest off disk and updates the installed cache), so
    # an after-snapshot would always match the manifest on real version-bump
    # scenarios — `events["updated"]` would stay empty and no notification
    # would fire despite the plugin having actually changed.
    installed_pre = _list_installed_agnes_plugins_in_cwd()

    _claude_marketplace_update()

    _reconcile_with_manifest(events=events, installed_pre=installed_pre)

    if events["installed"] or events["updated"] or events["enabled"]:
        typer.echo(
            "\nRun `/reload-plugins` in Claude Code to load the "
            "new/updated plugins into the running session — no restart needed."
        )


def _bootstrap_clone(token: str) -> bool:
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

    if not _register_clone_with_claude(CLONE_DIR):
        return False

    typer.echo(f"Marketplace bootstrapped at {CLONE_DIR}.")
    return True


def _register_clone_with_claude(clone_dir: Path) -> bool:
    """Call `claude plugin marketplace add <clone_dir>` and treat failures as fatal.

    Soft-passes when `claude` is not on PATH so workspaces without Claude
    Code installed (CI, sandbox) still complete the clone step. When
    `claude` IS available, a non-zero exit from `add` is fatal: continuing
    silently is the bug captured in David's 2026-05-10 init report — the
    subsequent `claude plugin marketplace update agnes` (and every plugin
    install) blew up with "Marketplace 'agnes' not found" because the add
    step had silently warned-and-continued. Returning False here lets the
    caller exit non-zero with the actual `add` stderr, which is the signal
    the operator needs to fix their machine state.
    """
    if shutil.which("claude") is None:
        return True
    add = subprocess.run(
        ["claude", "plugin", "marketplace", "add", str(clone_dir)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    if add.returncode != 0:
        typer.echo(
            f"error: `claude plugin marketplace add {clone_dir}` exited {add.returncode}.",
            err=True,
        )
        if add.stderr:
            typer.echo(add.stderr.rstrip(), err=True)
        return False
    if add.stdout:
        typer.echo(add.stdout.rstrip())
    return True


def _claude_marketplace_is_registered() -> bool:
    """Return True iff Claude Code already has MARKETPLACE_NAME in its registry.

    Parses `claude plugin marketplace list` text output. The CLI doesn't
    expose a --json flag for that subcommand at time of writing, so we
    match the marketplace name as a whole word in stdout. Returns False
    when `claude` is missing or the command itself fails — callers treat
    that as "not registered" and run the add path, which is the correct
    fail-safe (worst case: a redundant add that itself errors out cleanly).
    """
    if shutil.which("claude") is None:
        return False
    try:
        result = subprocess.run(
            ["claude", "plugin", "marketplace", "list"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
    except FileNotFoundError:
        return False
    if result.returncode != 0:
        return False
    # Filter out `Source: …` lines before matching: the CLI prints the
    # local clone path there (e.g. `Source: Local path (~/.agnes/marketplace)`),
    # so a naive `\bagnes\b` over the full stdout false-positives whenever
    # ANY registered marketplace happens to live under a path containing
    # the marketplace name. We only care about the registry headers.
    relevant = "\n".join(
        line for line in (result.stdout or "").splitlines()
        if not line.lstrip().startswith("Source:")
    )
    pattern = re.compile(rf"\b{re.escape(MARKETPLACE_NAME)}\b")
    return bool(pattern.search(relevant))


def _ensure_marketplace_registered() -> bool:
    """Make sure Claude Code has the cloned marketplace registered.

    Used by the `--bootstrap` recovery path when CLONE_DIR already exists
    but the Claude Code marketplace registry doesn't list `agnes` (fresh
    Claude Code install on the same machine, manual `claude plugin
    marketplace remove agnes`, or any other state where the clone survived
    but the registration didn't). Idempotent — re-registering an already-
    registered marketplace short-circuits in `_claude_marketplace_is_registered`.

    Returns False only when registration was needed and failed; True when
    registration was already in place OR `claude` is not on PATH (the
    latter matches `_register_clone_with_claude`'s soft-pass behavior).
    """
    if shutil.which("claude") is None:
        return True
    if _claude_marketplace_is_registered():
        return True
    typer.echo(
        f"Claude Code does not have `{MARKETPLACE_NAME}` registered; "
        f"running `claude plugin marketplace add {CLONE_DIR}`..."
    )
    return _register_clone_with_claude(CLONE_DIR)


def _git_fetch_only(token: str) -> bool:
    """Fetch from origin without resetting the working tree.

    Used by `--check` to learn whether the remote has new content without
    actually applying it. The bare repo on the server rebuilds as a fresh
    orphan commit on every content change, so FETCH_HEAD is always the
    full new tree — comparing local HEAD to FETCH_HEAD is sufficient to
    detect remote-side changes.
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
        typer.echo("error: `git` not found in PATH; cannot check marketplace.", err=True)
        return False
    if fetch.returncode != 0:
        if fetch.stdout:
            typer.echo(fetch.stdout, err=True)
        if fetch.stderr:
            typer.echo(fetch.stderr, err=True)
        return False
    return True


def _remote_head_sha(token: str) -> Optional[str]:
    """Return the remote `HEAD` SHA via `git ls-remote`, or None on failure.

    `ls-remote` returns one line of text per ref (`<sha>\\tHEAD`); no git
    objects are transferred — orders of magnitude cheaper than a full
    `git fetch` for the SessionStart-hook detector path. Same PAT wiring
    as `_git_fetch_only`: token in env, never on argv. Surfaces stderr
    on failure so auth/network errors aren't swallowed silently — the
    `--check` caller turns failure into exit 1.
    """
    env = {**os.environ, "AGNES_TOKEN": token}
    cmd = [
        "git",
        "-c", f"credential.helper={_CREDENTIAL_HELPER}",
        "-C", str(CLONE_DIR),
        "ls-remote", "origin", "HEAD",
    ]
    try:
        result = subprocess.run(
            cmd, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=False,
        )
    except FileNotFoundError:
        typer.echo("error: `git` not found in PATH; cannot check marketplace.", err=True)
        return None
    if result.returncode != 0:
        if result.stdout:
            typer.echo(result.stdout, err=True)
        if result.stderr:
            typer.echo(result.stderr, err=True)
        return None
    first_line = result.stdout.strip().splitlines()[:1]
    if not first_line:
        return None
    sha = first_line[0].split()[0].strip()
    return sha or None


def _local_head_sha() -> Optional[str]:
    """Return the local `HEAD` SHA, or None on any rev-parse failure.

    None means "can't determine local state" — the `--check` caller
    treats that as "stay silent" rather than emitting a misleading
    updates-available hint built on a missing left-hand side.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(CLONE_DIR), "rev-parse", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def _git_fetch_and_reset(token: str) -> bool:
    """Fetch from origin then hard-reset to FETCH_HEAD.

    Not `pull --ff-only`: the marketplace bare repo on the server rebuilds
    as a fresh orphan commit on every content change, so two snapshots
    have unrelated histories and fast-forward is impossible.
    """
    if not _git_fetch_only(token):
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

    if reset.stdout:
        typer.echo(reset.stdout.rstrip())
    return True


def _claude_marketplace_update() -> None:
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
    if result.stdout:
        typer.echo(result.stdout.rstrip())


def _reconcile_with_manifest(
    *,
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
        if result.stdout:
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
        if result.stdout:
            typer.echo(result.stdout.rstrip())

    # Whether anything was installed or updated above, the workspace
    # settings.json must end up with `enabledPlugins["<name>@agnes"]: true`
    # for every plugin in the stack — `claude plugin install` does not do
    # this on its own, and a fresh refresh on a workspace where the user
    # manually `claude plugin disable`-d a stack plugin must re-enable it.
    _enable_plugins_in_workspace_settings(manifest, events=events)

    if not to_install and not to_update and not events["enabled"]:
        typer.echo(f"All {len(manifest)} Agnes-stack plugin(s) up to date.")


def _emit_check_hook_message() -> None:
    """Emit Claude Code hook JSON pointing the user at `/update-agnes-plugins`.

    `systemMessage` is a transient toast; `additionalContext` is wrapped in
    a system reminder Claude reads at session start, so the model can
    proactively mention the available update if the user's first ask is
    plugin-related. The hook itself does NOT install anything — running
    the slash command is the user's choice.
    """
    summary = (
        "Agnes marketplace has updates available. "
        "Run /update-agnes-plugins to install them."
    )
    payload = {
        "systemMessage": summary,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": summary,
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


def _enable_plugins_in_workspace_settings(
    manifest: dict[str, str],
    *,
    events: dict[str, list[str]],
) -> None:
    """Ensure workspace `.claude/settings.json` has `enabledPlugins` entries
    for every plugin in the user's stack manifest.

    `claude plugin install --scope project` only writes the global plugin
    registry (`~/.claude/plugins/installed_plugins.json`); it does NOT add
    the plugin to the workspace `enabledPlugins` map, so Claude Code treats
    every stack plugin as disabled until something explicitly enables it.
    This helper closes that gap: after install/update, we write
    `"<name>@agnes": true` for each manifest entry directly into the
    workspace settings.

    Stack-as-source-of-truth: a locally `claude plugin disable`-d plugin
    that still appears in the user's stack gets re-enabled. To permanently
    exclude a plugin, remove it from the stack (`agnes marketplace remove`)
    rather than relying on local disable, which is ephemeral between
    refreshes.

    Runs unconditionally — `refresh-marketplace` is a runtime command, so
    the Initial Workspace Template sentinel (`override: true`) does not
    apply here. The sentinel governs init-time skip only; runtime CLI
    keeps workspaces in sync with the user's current stack regardless of
    how the workspace was originally seeded.

    Idempotent: writes only when at least one plugin actually changed
    state (missing/false → true). No write when everything is already
    enabled, so this is safe to call on every refresh without churning
    mtime or polluting git diffs in workspace repos.
    """
    workspace = Path.cwd()
    settings_path = workspace / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    if settings_path.exists():
        try:
            cfg = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            typer.echo(
                f"warn: {settings_path} is not valid JSON; skipping plugin enable.",
                err=True,
            )
            return
        if not isinstance(cfg, dict):
            typer.echo(
                f"warn: {settings_path} top-level is not an object; skipping plugin enable.",
                err=True,
            )
            return
    else:
        cfg = {}

    enabled = cfg.setdefault("enabledPlugins", {})
    if not isinstance(enabled, dict):
        typer.echo(
            f"warn: {settings_path} `enabledPlugins` is not an object; skipping plugin enable.",
            err=True,
        )
        return

    changed: list[str] = []
    for name in manifest:
        key = f"{name}@{MARKETPLACE_NAME}"
        if enabled.get(key) is not True:
            enabled[key] = True
            changed.append(name)

    if not changed:
        return

    settings_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    events["enabled"].extend(sorted(changed))
    typer.echo(
        f"Enabled {len(changed)} plugin(s) in workspace settings: "
        + ", ".join(sorted(changed))
    )
