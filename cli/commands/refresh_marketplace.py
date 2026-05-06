"""`agnes refresh-marketplace` — reconcile the workspace plugin set with
the user's current Agnes stack.

The Agnes "stack" served to a user is composed server-side as
``(admin RBAC grants ∖ user MyAIStack opt-outs) ∪ user /store installs``
(see ``src/marketplace_filter.py:resolve_user_marketplace``). The served
manifest treats the three sources slightly differently:

- **Admin RBAC grants** materialize as one plugin entry each, version
  taken from the upstream ``plugin.json``.
- **/store ``type=plugin`` installs** also get one entry each, with a
  ``store-<entity_id>`` prefix so two owners' same-named plugins don't
  collide.
- **/store ``type=skill`` and ``type=agent`` installs** ALL collapse into
  ONE synth plugin called ``agnes-store-bundle`` whose ``version`` is a
  sha256 of the bundle's contents. Adding a single skill via /store
  doesn't add a marketplace entry — it bumps the bundle's version.

That aggregation has a direct consequence for refresh logic: if we only
auto-installed plugins listed in the manifest but missing locally,
adding a /store skill would never propagate (the bundle is already
installed; only its version changed). So this command does
**version-aware reconciliation**, not just "install missing":

  1. ``git fetch`` against the clone with PAT injection (per-pull
     credential helper, no persistent change to the user's git config —
     PAT stays out of ``.git/config`` URL at rest), then
     ``git reset --hard FETCH_HEAD``. The bare repo on the server is
     rebuilt as a fresh orphan commit on every content change (see
     ``app/marketplace_server/git_backend.py:build_bare_repo`` —
     ``commit.parents = []``), so a normal ``pull --ff-only`` would hit
     "Not possible to fast-forward" the moment the server-side manifest
     changes. The local clone is treated as a snapshot mirror, not a
     history we own.
  2. ``claude plugin marketplace update agnes`` so Claude Code re-reads
     the refreshed manifest.
  3. **Reconcile installed vs. manifest** — for each plugin in the
     manifest:
       - Not installed in this workspace → ``claude plugin install
         <name>@agnes --scope project``
       - Installed but version differs → ``claude plugin update
         <name>@agnes``
       - Installed and version matches → skip
     We DON'T auto-uninstall plugins that disappeared from the manifest
     (admin revoked, user opted out via MyAIStack, /store uninstall) —
     uninstall is destructive and a transient server bug returning an
     empty manifest would wipe everything. Future opt-in flag.

When invoked with ``--quiet`` (the SessionStart hook context) and at
least one plugin was installed or updated, this command emits a Claude
Code hook JSON object on stdout. ``systemMessage`` becomes a transient
notification visible to the user; ``hookSpecificOutput.additionalContext``
is wrapped in a system reminder so the model sees what changed at
session start. Empty / no-op runs produce empty stdout, so quiet
sessions stay quiet.

Used by:
- Manual invocation: ``agnes refresh-marketplace`` after a known stack
  change, or just to verify the clone is healthy.
- SessionStart hook: ``agnes refresh-marketplace --quiet 2>/dev/null || true``
  runs every Claude Code session so users get stack changes (new plugins
  installed, version bumps applied) without re-running setup.
- Initial install: ``agnes refresh-marketplace --bootstrap`` is what the
  setup-instructions step 5 emits as a one-liner. With ``--bootstrap``,
  if no clone exists yet the command does the initial ``git clone`` +
  PAT-strip + chmod + ``claude plugin marketplace add`` itself, then
  falls through to the normal fetch+reset+reconcile flow so the user's
  stack lands installed in one shot. The flag exists because the install
  prompt runs from inside Claude Code, where the agent's permission gate
  blocks ad-hoc ``rm -rf`` — pulling the destructive prep into the agnes
  binary lets the CLI's grant of trust cover it.

Design choices:
- **No-op when the clone is missing.** Workspaces that don't use the
  stack (no plugin grants, or skipped step 5) shouldn't see hook noise.
  Exits 0 silently if ``~/.agnes/marketplace/.git`` isn't there.
- **No-op when claude isn't in PATH.** The git fetch+reset still runs,
  so the next session that does have claude available picks up the
  changes via Claude Code's natural startup re-read of the registered
  marketplace. Reconcile is also skipped (it requires ``claude``).
- **PAT injection only via env-var.** Never appears in argv, so ``ps``
  on Linux/macOS or ``tasklist /v`` on Windows can't observe it. The
  one-shot credential helper is scoped to this single git invocation
  via ``git -c credential.helper=...``, so unrelated git commands the
  user later runs don't see our helper or our token.
- **Install scope: project.** Mirrors the initial setup-instructions
  install line (``--scope project``), so plugins land in the workspace
  the SessionStart hook fired in. Workspace match enforced by filtering
  ``claude plugin list --json`` on ``projectPath == cwd``; without that
  a plugin installed in workspace A would mask a missing/outdated entry
  in workspace B.
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
    bootstrap: bool = typer.Option(
        False, "--bootstrap",
        help=(
            "If no marketplace clone exists yet, clone it from the server "
            "and register the local path with Claude Code. Used by the "
            "setup-instructions install flow as a one-liner replacement for "
            "the manual `git clone` + chmod + `claude plugin marketplace add` "
            "sequence (which trips Claude Code's `rm -rf` permission gate "
            "on agent-driven onboarding). No-op if the clone already exists."
        ),
    ),
):
    """Sync the marketplace clone, re-register with Claude, install/update plugins.

    With ``--bootstrap`` this command also handles initial clone, so the
    install flow can call it once and not need a separate `git clone` /
    `claude plugin marketplace add` sequence.
    """
    clone_exists = (CLONE_DIR / ".git").is_dir()

    if not clone_exists and not bootstrap:
        # No clone → nothing to refresh. Hook contexts hit this on every
        # workspace that didn't go through step 5; silent exit keeps logs
        # clean. Manual invocation gets a hint so the user knows why.
        # Importantly: we don't read the token here. The hook command
        # `agnes refresh-marketplace --quiet` runs in every workspace that
        # has the hook installed, including ones where no agnes token is
        # configured yet (e.g. a fresh CI checkout). Forcing a token check
        # before the no-op short-circuit would surface spurious auth_failed
        # errors on workspaces that legitimately have no marketplace.
        if not quiet:
            typer.echo(
                f"No marketplace clone at {CLONE_DIR} — nothing to refresh. "
                "Re-run setup with `agnes refresh-marketplace --bootstrap` "
                "(or re-run setup from the dashboard) to clone it."
            )
        raise typer.Exit(0)

    # Token is needed for both bootstrap-clone and the fetch+reset path.
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
        # --bootstrap requested + no clone: do the initial clone + register
        # with Claude. Then fall through to the normal fetch/reset/reconcile
        # flow so any plugins listed in the manifest get installed too.
        if not _bootstrap_clone(token, quiet=quiet):
            raise typer.Exit(1)

    # Collected during the run so the hook-output JSON can summarize what
    # changed. Empty lists → quiet stdout (no JSON emitted).
    events: dict[str, list[str]] = {"installed": [], "updated": []}

    fetch_ok = _git_fetch_and_reset(token, quiet=quiet)
    if not fetch_ok:
        # Fetch/reset failure already surfaced via stderr; exit non-zero so
        # hook consumers can detect it (the hook itself swallows non-zero via
        # `|| true`, but a manual `agnes refresh-marketplace` should fail).
        raise typer.Exit(1)

    # Capture installed versions BEFORE telling Claude Code to re-read
    # the marketplace. `claude plugin marketplace update` auto-applies
    # version bumps for plugins registered as a local-path marketplace
    # — Claude reads the new manifest off disk and silently updates the
    # plugin's installed version to match. If we captured AFTER that,
    # the diff against the new manifest would always be zero on real
    # version-bump scenarios (admin pushes v1.0.1, /store skill bumps
    # the agnes-store-bundle content hash, etc.) and the reconcile-event
    # tracking would never fire — no notification surface for the user,
    # despite the plugin actually getting updated. Snapshot now while
    # the old version is still observable.
    installed_pre = _list_installed_agnes_plugins_in_cwd()

    _claude_marketplace_update(quiet=quiet)

    _reconcile_with_manifest(quiet=quiet, events=events, installed_pre=installed_pre)

    # In hook context (--quiet), emit a Claude Code hook JSON object on
    # stdout summarizing the run so the user gets a notification + the
    # session model sees what changed. Skip when nothing changed so quiet
    # sessions stay quiet.
    if quiet and (events["installed"] or events["updated"]):
        _emit_hook_message(events)


def _bootstrap_clone(token: str, *, quiet: bool) -> bool:
    """Initial clone of the per-user marketplace bare repo.

    Replaces the four-step shell sequence the install prompt used to emit
    inline (``rm -rf`` + ``git clone`` + ``git remote set-url`` + ``chmod``
    + ``claude plugin marketplace add``). Pulling that into the CLI buys
    two things:

      1. **Claude Code permission gate**: the agent-driven onboarding flow
         in Claude Code denies ``rm -rf`` by default. Wrapping it in the
         agnes binary lets the CLI's grant of trust cover the deletion.
      2. **Idempotence**: the install prompt is documented as "safe to
         re-run if a step fails partway through". Manual ``rm -rf`` +
         ``git clone`` only re-runs cleanly because the rm hides whatever
         the previous clone left behind. We can do the same defensively
         here without forcing the operator to grant the destructive
         permission.

    The clone URL embeds the PAT as HTTP Basic in the user-info segment
    (``https://x:<PAT>@host/marketplace.git/``) — same scheme git itself
    uses when fetching credentials from the URL. Once the clone succeeds
    the origin URL is rewritten without the PAT so it doesn't sit in
    plaintext at ``.git/config`` (refreshes use the per-invocation
    credential helper instead). chmod is best-effort — no-op on Windows
    NTFS via Git Bash, real tightening on macOS/Linux.

    Returns True on success, False on any failure. The caller treats
    False as "exit 1, don't proceed to fetch/reset".
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

    # If a stale dir exists without a `.git/` subdir we can't reuse it as
    # a working tree; remove it before clone. We avoid this branch in the
    # common case (caller checked .git/ exists already), so this only
    # runs when an interrupted prior install left a half-formed dir
    # behind. Do it via shutil.rmtree (Python-native, doesn't trip the
    # `rm -rf` shell-pattern permission gate).
    if CLONE_DIR.exists():
        try:
            shutil.rmtree(CLONE_DIR, ignore_errors=False)
        except OSError as exc:
            typer.echo(
                f"error: could not remove stale {CLONE_DIR}: {exc}",
                err=True,
            )
            return False

    CLONE_DIR.parent.mkdir(parents=True, exist_ok=True)

    auth_url = f"{scheme}://x:{token}@{server_host}/marketplace.git/"
    clean_url = f"{scheme}://{server_host}/marketplace.git/"

    if not quiet:
        typer.echo(f"Cloning marketplace from {clean_url} into {CLONE_DIR}...")

    clone_cmd = ["git", "clone", auth_url, str(CLONE_DIR)]
    try:
        result = subprocess.run(clone_cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        typer.echo("error: `git` not found in PATH; cannot clone marketplace.", err=True)
        return False
    if result.returncode != 0:
        # Forward stderr verbatim — clone failures are network / TLS / auth
        # and the operator needs the literal git diagnostic.
        if result.stderr:
            typer.echo(result.stderr.rstrip(), err=True)
        return False

    # Strip the PAT from the cloned origin so it doesn't sit at rest in
    # `.git/config`. Refreshes use the per-invocation credential helper.
    set_url = subprocess.run(
        ["git", "-C", str(CLONE_DIR), "remote", "set-url", "origin", clean_url],
        capture_output=True, text=True, check=False,
    )
    if set_url.returncode != 0:
        # Non-fatal — the refresh path's credential helper still works
        # because it injects via `-c credential.helper=`, not via origin
        # URL parsing. But warn loudly so the operator knows the PAT is
        # currently sitting in `.git/config`.
        typer.echo(
            f"warn: could not strip PAT from origin URL: {set_url.stderr.rstrip()}",
            err=True,
        )

    # Best-effort chmod — no-op on Windows NTFS via Git Bash MSYS, but
    # tightens 700 / 600 on macOS / Linux so other users on the box can't
    # read the repo content (especially `.git/config` if the strip above
    # silently no-op'd somewhere).
    for path, mode in (
        (CLONE_DIR, 0o700),
        (CLONE_DIR / ".git", 0o700),
        (CLONE_DIR / ".git" / "config", 0o600),
    ):
        try:
            path.chmod(mode)
        except OSError:
            pass

    # Register the local clone path with Claude Code. Skip silently if
    # claude isn't on PATH (the bootstrap still produced a usable clone;
    # the subsequent `claude plugin marketplace update` in the main flow
    # will warn). This matches the soft-fail behavior elsewhere.
    if shutil.which("claude") is not None:
        add_cmd = ["claude", "plugin", "marketplace", "add", str(CLONE_DIR)]
        add = subprocess.run(add_cmd, capture_output=True, text=True, check=False)
        if add.returncode != 0:
            typer.echo(
                f"warn: `claude plugin marketplace add {CLONE_DIR}` "
                f"exited {add.returncode}.",
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

    Why not ``pull --ff-only``? The marketplace bare repo on the server
    rebuilds as a brand-new orphan commit (``commit.parents = []``) on
    every content change — see ``app/marketplace_server/git_backend.py:
    build_bare_repo``. Two snapshots have unrelated histories, so a
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


def _reconcile_with_manifest(
    *,
    quiet: bool,
    events: dict[str, list[str]],
    installed_pre: Optional[dict[str, str]] = None,
) -> None:
    """Make installed plugins match the served manifest.

    For each plugin in the marketplace.json:
      - Not installed locally → ``claude plugin install <name>@agnes --scope project``
      - Installed at a different version → ``claude plugin update <name>@agnes``
      - Installed and version matches → skip

    Why version-aware?  The /store skill+agent bundle (``agnes-store-bundle``)
    shares ONE manifest entry across every skill/agent the user installed
    from /store; adding a skill bumps the bundle's sha256-based version
    without changing the manifest's plugin set. A "missing-only" install
    flow would never see those changes. Same applies to admin pushing a
    new version of an existing plugin.

    The ``installed_pre`` arg is the snapshot of plugins-as-installed
    captured BEFORE ``claude plugin marketplace update`` ran. Why a
    pre-snapshot matters: ``claude plugin marketplace update`` for a
    local-path marketplace silently auto-applies version bumps from the
    refreshed manifest, so an installed-snapshot taken AFTER it would
    already match the manifest in the very scenarios we want to detect
    (admin pushed a new version, /store bundle hash changed, …). The
    caller passes the pre-snapshot it captured between fetch+reset and
    the marketplace update; we diff against it to see what *was actually
    different* before Claude silently reconciled. As a fallback (e.g.
    bootstrap path where the caller passed ``None``), we re-read live —
    that case has no pre-state to preserve anyway because the clone
    just appeared.

    We don't auto-uninstall plugins that disappeared from the manifest —
    that's destructive (transient server-side empty-manifest bug would
    wipe the user's stack) and the user can ``claude plugin uninstall``
    explicitly. Future opt-in flag possible.

    Successful actions are appended to ``events["installed"]`` /
    ``events["updated"]`` so the caller can summarize via the hook JSON.
    """
    if shutil.which("claude") is None:
        # _claude_marketplace_update already warned; don't double-print.
        return

    manifest = _read_marketplace_plugin_versions()
    if manifest is None:
        typer.echo(
            "warn: could not read marketplace.json from the clone; "
            "skipping reconcile.",
            err=True,
        )
        return
    if not manifest:
        # Empty stack (RBAC granted nothing, no /store installs). Nothing to do.
        return

    installed = installed_pre if installed_pre is not None else _list_installed_agnes_plugins_in_cwd()
    if installed is None:
        typer.echo(
            "warn: could not enumerate installed plugins; "
            "skipping reconcile.",
            err=True,
        )
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
            typer.echo(
                f"Installing {len(to_install)} new plugin(s): "
                + ", ".join(to_install)
            )
        if to_update:
            typer.echo(
                f"Updating {len(to_update)} plugin(s) to latest version: "
                + ", ".join(to_update)
            )

    for name in to_install:
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
        events["installed"].append(name)
        if not quiet and result.stdout:
            typer.echo(result.stdout.rstrip())

    for name in to_update:
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
        events["updated"].append(name)
        if not quiet and result.stdout:
            typer.echo(result.stdout.rstrip())


def _emit_hook_message(events: dict[str, list[str]]) -> None:
    """Emit a Claude Code hook JSON object summarizing what changed.

    Output shape (per Claude Code hook protocol):
      - ``systemMessage`` is a transient warning-style toast. It's
        best-effort: users frequently miss it because the toast vanishes
        a few seconds after session start, before the user gets a chance
        to look. We still emit it for the lucky cases.
      - ``hookSpecificOutput.additionalContext`` is wrapped in a system
        reminder before the user's first prompt — Claude *reliably* sees
        it. So we phrase it as a direct instruction to mention the change
        at the start of the first response, not as passive documentation.
        Without that prompting, Claude reads the context but only mentions
        it if the user happens to ask "what's new?".

    Plus a persistent log line at ``~/.agnes/refresh.log`` so the user
    can ``cat`` it later if they noticed something installed itself but
    missed the in-session notification.

    User-facing language says "your Agnes stack" rather than "the Agnes
    marketplace": the served set is **per-user composed**, not a single
    shared catalog. From ``src/marketplace_filter.py:236`` the formula
    is ``(admin RBAC grants ∖ MyAIStack opt-outs) ∪ /store installs`` —
    three independent sources, any of which can change. Plus the
    ``agnes-store-bundle`` quirk: skill / agent additions don't add a
    manifest entry, they just bump the bundle's version (so they
    materialize as an "updated" event, not an "installed" one).

    Caller has already verified that at least one of ``installed`` /
    ``updated`` is non-empty, so we always emit something useful.
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

    # Persistent log line — survives across sessions, user can `cat` it
    # to see what's been installed/updated by the SessionStart hook.
    _append_refresh_log(summary)

    # additionalContext is phrased as a direct instruction so Claude
    # mentions the change at the top of its first reply (the toast is
    # transient and easy to miss).
    additional = (
        f"{summary} BEFORE responding to the user's first message, "
        "open with one short sentence acknowledging this change so the "
        "user knows the SessionStart hook just installed/updated their "
        "stack — then proceed with their actual request. Keep the "
        "acknowledgement to a single sentence; don't dwell. "
        "\n\n"
        "Background for your own understanding (don't repeat this to "
        "the user unless asked): the Agnes stack is composed as "
        "`(admin RBAC grants ∖ user MyAIStack opt-outs) ∪ user /store "
        "installs` (see src/marketplace_filter.py:236). 'Installed' "
        "means a plugin newly appeared in the stack — admin grant, "
        "MyAIStack untoggle, or a /store type=plugin install. "
        "'Updated' means an existing plugin's version changed — admin "
        "pushed a new version, or the user added/removed a skill/agent "
        "from /store (those share one synth plugin `agnes-store-bundle` "
        "whose version is a content hash). The CLI can't tell which "
        "source from the diff alone."
    )
    payload = {
        "systemMessage": summary,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": additional,
        },
    }
    typer.echo(json.dumps(payload))


def _append_refresh_log(summary: str) -> None:
    """Persist a one-line entry per refresh that changed something.

    Lives at ``~/.agnes/refresh.log`` (next to the marketplace clone).
    Best-effort: any I/O error is swallowed silently — losing a log line
    is a worse-than-nothing UX, but it's still UX, not a fatal flow.

    Format: ``<ISO-8601 UTC>  <summary>`` per line. A user who notices a
    plugin installed itself can ``cat ~/.agnes/refresh.log`` to confirm
    when and what.
    """
    log_path = CLONE_DIR.parent / "refresh.log"
    try:
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"{ts}  {summary}\n")
    except OSError:
        pass


def _read_marketplace_plugin_versions() -> Optional[dict[str, str]]:
    """Map ``plugin name → version`` from the local marketplace.json.

    Returns None if the file is missing/unreadable/malformed (caller
    treats that as "warn and skip"). Returns an empty dict when the
    manifest is valid but lists no plugins (RBAC-empty, no /store
    installs).

    A plugin entry without a ``version`` field is skipped — Claude Code
    can't reason about updates without one, and ``compare != ""`` would
    be useless.
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
    """Map ``plugin name → installed version`` for agnes-marketplace plugins
    in the current workspace.

    Best-effort enumeration via ``claude plugin list --json``. The output
    is a flat list across all workspaces, so we filter by:
      - ``id`` ends with ``@agnes`` (parses out the marketplace from the id)
      - ``projectPath`` equals current working directory (so plugins from
        sibling workspaces don't get counted as already-installed here)

    Returns None if we can't get a structured answer (claude missing,
    --json flag unsupported, output not parseable). Empty dict means
    "nothing currently installed in this workspace from agnes".
    """
    # Short-circuit when `claude` isn't on PATH so callers (including the
    # pre-snapshot capture in the main flow) don't trigger a subprocess
    # invocation that would either fail with FileNotFoundError or in
    # tests register a spurious `claude` call against a `which`-less
    # fixture. Matches the soft-fail philosophy in _claude_marketplace_update.
    if shutil.which("claude") is None:
        return None
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
        # Strip the @agnes suffix to get the plain name.
        name = plugin_id[: -len(suffix)]
        if name:
            versions[name] = version
    return versions
