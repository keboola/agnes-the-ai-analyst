"""Workspace-scoped Claude Code hook installer.

Lifted from `cli/commands/analyst.py:_install_claude_hooks` (which gets
deleted in Task 18) so `agnes init` and any future caller can use it
without dragging in the deleted command module.

Design notes:
- Workspace-scoped (`<workspace>/.claude/settings.json`), NOT user-home.
  The hooks fire only when Claude Code opens this workspace.
- Idempotent: second invocation drops prior `agnes self-upgrade` /
  `agnes pull` / `agnes push` / `agnes refresh-marketplace` / `da sync`
  entries (matched by command substring) and appends fresh entries.
  Third-party hooks (mixed entries, foreign commands) are left alone.
- Uses `|| true` in the hook command so the hook never blocks a session on
  a transient sync error.
- SessionStart gets two entries:
    1. Chained `agnes self-upgrade; agnes pull` — self-upgrade runs first
       so any wire-protocol bump lands before pull tries to use the new
       CLI version (and back-fills the `workspace_root` config anchor for
       older clients). Both `|| true`-guarded so an upgrade failure doesn't
       block the pull.
    2. `agnes refresh-marketplace --check` — independent entry. Detector-
       only (since the slash-command split): runs `git fetch` against the
       marketplace clone and emits a Claude Code hook JSON message
       hinting the user at `/update-agnes-plugins` when remote content
       changed. Does NOT install/update plugins itself — the slash
       command does that interactively, with full output visible in the
       Claude Code transcript and under user control. Failure (no clone,
       no token) silently no-ops via the surrounding `|| true`.

  There is no `agnes capture-session` entry any more. `agnes push` now
  scans the workspace's Claude Code session folder directly (anchored on
  the `workspace_root` config key), which is reliable on macOS where the
  hook stdin the old capture step depended on is delivered empty. The
  capture-session markers below stay in `_OUR_COMMAND_MARKERS` only so the
  old entries are stripped from a pre-existing settings.json on the next
  `agnes init` / self-upgrade refresh.

- SessionEnd gets one entry: `agnes push --quiet` wrapped to detach into
  the background.

  The push must detach because Claude Code in `-p` (headless) mode
  terminates SessionEnd hook subprocesses after ~1 second regardless of
  work in progress, so a synchronous `agnes push` (which uploads N session
  JSONLs serially and typically takes 5-30s) gets killed mid-stream and
  most files never reach the server. The `( nohup ... & )` subshell
  orphans the upload child so it survives the Claude shutdown. Errors are
  routed to /dev/null — no worse than the previous `2>/dev/null` form.
  Operators who want visibility into push failures can manually run
  `agnes push --json`. The next session's SessionEnd push re-scans the
  folder, so any transcript a prior push missed (crash, kill, terminal
  close) is picked up then — the upload ledger dedups what already landed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


# Substrings that identify "our" hook commands. Includes legacy `da sync`
# so a workspace bootstrapped by an older CLI gets cleanly upgraded on the
# next `agnes init` run. `agnes init` is listed so the one-time setup hook
# written into Cowork bundles is replaced by the proper pull/push hooks the
# first time `agnes init` calls `install_claude_hooks`.
#
# `agnes capture-session` and the two `capture-session/*.sh` script paths
# stay here purely for MIGRATION cleanup: the capture step is gone, but a
# settings.json seeded by an older CLI (CLI-form `agnes capture-session`) or
# by the template (`bash .claude/hooks/capture-session/capture.sh`) still
# carries those SessionStart/SessionEnd entries — matching them here lets the
# next install / refresh strip them instead of leaving a dead entry that
# shells out to a deleted script.
_OUR_COMMAND_MARKERS = (
    "agnes self-upgrade",
    "agnes update",
    "agnes pull",
    "agnes push",
    "agnes refresh-marketplace",
    "agnes capture-session",
    "capture-session/capture.sh",
    "capture-session/stop-guard.sh",
    "agnes mark-private",
    "agnes statusline",
    "agnes init",
    "da sync",
)


def install_claude_hooks(workspace: Path) -> None:
    """Install SessionStart hooks (`agnes self-upgrade; agnes pull` chained
    + `agnes refresh-marketplace` as a separate entry) and SessionEnd hook
    (`agnes push`).

    Idempotent. Workspace-scoped (writes `<workspace>/.claude/settings.json`).
    Preserves third-party hooks and other event types.

    Override-sentinel handling lives at the call site, not here. The
    init-time caller (`cli/commands/init.py`, gated by `override_active`)
    decides whether to skip this writer for admin-templated workspaces.
    Runtime callers (`maybe_refresh_claude_hooks` from `agnes
    self-upgrade`) invoke us unconditionally so existing override
    workspaces still pick up new Agnes hook layouts.
    """
    settings_path = workspace / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    if settings_path.exists():
        try:
            cfg = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # Self-heal a corrupt settings.json instead of giving up. The old
            # behaviour (`return`) bricked hook repair entirely: a malformed
            # file could never be fixed by `agnes init`/`self-upgrade`/`update`.
            # Back up the unparseable file (so the analyst can recover any
            # third-party keys by hand) and rebuild Agnes's managed entries
            # from an empty config.
            from datetime import datetime, timezone

            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            corrupt = settings_path.with_name(f"{settings_path.name}.corrupt.{ts}")
            try:
                corrupt.write_bytes(settings_path.read_bytes())
                print(
                    f"Warning: {settings_path} was not valid JSON; backed it up to "
                    f"{corrupt.name} and rebuilt the Agnes hook entries.",
                    file=sys.stderr,
                )
            except OSError:
                print(
                    f"Warning: {settings_path} was not valid JSON and could not be "
                    f"backed up; rebuilt the Agnes hook entries from scratch.",
                    file=sys.stderr,
                )
            cfg = {}
    else:
        cfg = {}

    hooks = cfg.setdefault("hooks", {})

    def _replace_or_add(event: str, commands: list[str]) -> None:
        existing = hooks.setdefault(event, [])
        # Remove ALL prior entries that look like ours (every command in
        # the entry matches one of our markers). Third-party entries
        # — which have commands like `echo hi from another tool` — fall
        # through unchanged.
        for entry in list(existing):
            entry_cmds = [h.get("command", "") for h in entry.get("hooks", [])]
            if entry_cmds and all(any(marker in c for marker in _OUR_COMMAND_MARKERS) for c in entry_cmds):
                existing.remove(entry)
        # Append fresh entries — one per command. Independent entries mean
        # a failure in one (e.g. refresh-marketplace on a workspace that
        # never cloned the marketplace) doesn't suppress the other.
        for cmd in commands:
            existing.append({"hooks": [{"type": "command", "command": cmd}]})

    # All entries are wrapped in `bash -c "..."` for Windows compatibility:
    # Claude Code on Windows runs hook commands directly (no shell), so
    # the `; ` chain operator, `2>/dev/null` redirection, and `|| true`
    # short-circuit never get interpreted unless we explicitly invoke
    # bash. (Git Bash on PATH or WSL satisfies this.) The self-upgrade +
    # pull chain previously shipped unwrapped — that pre-dates the
    # Windows fix; ship it wrapped now so every entry uses the same
    # contract. Workspaces still on the older unwrapped form auto-upgrade
    # via `maybe_refresh_claude_hooks` on the next `agnes self-upgrade`.
    #
    # `--check` makes the marketplace entry a detector only: the actual
    # plugin install/update happens in the `/update-agnes-plugins` slash
    # command (installed by `cli.lib.commands.install_claude_commands`).
    # Workspaces still on the older `--quiet` form auto-upgrade here
    # because `_OUR_COMMAND_MARKERS` matches by substring on the
    # `agnes refresh-marketplace` prefix.
    #
    # No `agnes capture-session` entry: `agnes push` scans the workspace's
    # Claude Code session folder directly (anchored on the `workspace_root`
    # config key), so there's nothing to capture at SessionStart. Any
    # leftover capture entry from a CLI- or template-seeded settings.json is
    # stripped because its command matches a `_OUR_COMMAND_MARKERS` substring
    # ("agnes capture-session" or "capture-session/capture.sh").
    # SessionStart kicks off ONE detached `agnes update --quiet`: the unified
    # convergence (CLI self-upgrade -> workspace template -> Agnes-owned
    # hooks/statusLine/commands -> marketplace plugins -> data pull -> report).
    # Detached via `( nohup ... & )` (like the SessionEnd push) so it NEVER
    # blocks session start; a freshly-installed CLI binary, if any, activates
    # next session. `agnes update` sets AGNES_NO_UPDATE_CHECK internally so it
    # doesn't recurse, and holds update.lock so only one runs.
    #
    # This replaces the older two entries (`self-upgrade; pull` chain + a
    # separate `refresh-marketplace --check`). Workspaces still on the old form
    # auto-migrate here: `_replace_or_add` strips entries whose commands match
    # `_OUR_COMMAND_MARKERS` (which covers `agnes self-upgrade` / `agnes pull` /
    # `agnes refresh-marketplace` / `agnes update`) and re-adds just this one.
    _replace_or_add(
        "SessionStart",
        [
            'bash -c "( nohup agnes update --quiet </dev/null >/dev/null 2>&1 & ) ; true"',
        ],
    )
    # SessionEnd runs the detached push. Claude Code in `-p` (headless) mode
    # SIGTERMs hook subprocesses after ~1 second regardless of work in
    # progress; a synchronous `agnes push` (5-30s for a typical workspace)
    # gets killed mid-first-upload and most session JSONLs never reach the
    # server. The subshell `( ... & )` backgrounds the child and exits
    # immediately, orphaning it to init/launchd so it survives the hook
    # subprocess kill. `bash -c` mirrors the refresh-marketplace pattern
    # for Windows compatibility (Claude Code on Windows runs hook commands
    # directly, no shell). `; true` keeps the line exit-0 like the old
    # `|| true` form. push dedups against its upload ledger, so re-scanning
    # the folder every SessionEnd never re-uploads an unchanged transcript.
    _replace_or_add(
        "SessionEnd",
        [
            'bash -c "( nohup agnes push --quiet </dev/null >/dev/null 2>&1 & ) ; true"',
        ],
    )

    _install_statusline(cfg)

    settings_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


# Claude Code's `statusLine` setting tells the editor to invoke a command
# on every status-bar refresh and display the first line of stdout. We
# wire it to `agnes statusline`, which surfaces the `🔒 agnes-private`
# indicator when the current session is marked private.
#
# Politeness: if the user (or another tool) has already set a `statusLine`
# in the workspace `settings.json`, we leave it untouched and emit a
# one-line stderr warning. Customizing the status bar is a personal
# preference and Agnes shouldn't clobber it. Operators who want both
# their custom output and the private indicator can compose `agnes
# statusline` into their own status-line command manually.
_STATUSLINE_MARKER = "agnes statusline"


def _install_statusline(cfg: dict) -> None:
    existing = cfg.get("statusLine")
    # Distinguish "key absent" / "key=null" / "key=empty string" from any
    # real value. A `None` or `""` value is legal JSON but conveys "no
    # statusLine wanted" rather than "default" — overwriting it would
    # silently undo the user's explicit opt-out.
    if existing is None or existing == "":
        cfg["statusLine"] = {"type": "command", "command": "agnes statusline"}
        return
    if isinstance(existing, dict) and _STATUSLINE_MARKER in str(existing.get("command", "")):
        return  # already ours — idempotent re-init
    print(
        "Warning: existing statusLine in .claude/settings.json preserved. "
        "To show the agnes-private indicator alongside your custom status, "
        "add `agnes statusline` to your command.",
        file=sys.stderr,
    )


def workspace_has_agnes_hooks(workspace: Path) -> bool:
    """True iff ``workspace/.claude/settings.json`` already shows signs of a
    prior ``agnes init``: at least one Agnes-managed hook entry, or our
    statusLine command.

    Used as a guard by :func:`maybe_refresh_claude_hooks` so that
    ``agnes self-upgrade`` (which fires from a SessionStart hook in every
    Agnes workspace) does not accidentally install hooks into a directory
    that is not an Agnes workspace — e.g. the user's home dir, if they
    invoke ``agnes self-upgrade`` manually from there.

    Returns False on missing / malformed settings.json — the caller treats
    that as "not an Agnes workspace", so the refresh skips.
    """
    settings_path = workspace / ".claude" / "settings.json"
    if not settings_path.exists():
        return False
    try:
        cfg = json.loads(settings_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(cfg, dict):
        return False
    sl = cfg.get("statusLine")
    if isinstance(sl, dict) and _STATUSLINE_MARKER in str(sl.get("command", "")):
        return True
    hooks = cfg.get("hooks", {})
    if not isinstance(hooks, dict):
        return False
    for entries in hooks.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for h in entry.get("hooks", []) or []:
                cmd = h.get("command", "") if isinstance(h, dict) else ""
                if any(marker in cmd for marker in _OUR_COMMAND_MARKERS):
                    return True
    return False


# Substrings that identify a workspace bootstrapped by the OLD server flow
# (pre-`agnes init`). These workspaces have a SessionEnd/SessionStart hook
# that uploaded sessions via the server's `collect_session` script and
# never install the `agnes self-upgrade` / `agnes pull` SessionStart hooks
# — so the CLI sits on a stale version indefinitely. Matched by substring
# so any variant of the old layout (direct python invocation, a wrapper
# shell script under server/scripts/, …) is caught.
_LEGACY_COMMAND_MARKERS = (
    "collect_session",
    "server/scripts/",
)


def workspace_has_legacy_hooks(workspace: Path) -> bool:
    """True iff ``workspace/.claude/settings.json`` carries a hook from the
    OLD server flow (a SessionStart/SessionEnd command referencing
    ``collect_session`` or ``server/scripts/``) AND the workspace does not
    already have modern Agnes hooks.

    Legacy-flow workspaces never invoke ``agnes self-upgrade`` (it is only
    wired by ``agnes init``), so their CLI drifts stale indefinitely.
    ``agnes pull`` calls this to emit a one-line nudge pointing the analyst
    at ``agnes init``. We do NOT auto-migrate — the analyst owns when their
    hook layout changes.

    The ``not workspace_has_agnes_hooks`` guard ensures a workspace already
    on the modern layout is never flagged, even if a stray legacy entry
    lingers alongside the new hooks — once ``agnes init`` has run, the
    self-upgrade hook is wired and the nudge is moot. Returns False on
    missing / malformed settings.json.
    """
    settings_path = workspace / ".claude" / "settings.json"
    if not settings_path.exists():
        return False
    try:
        cfg = json.loads(settings_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(cfg, dict):
        return False
    hooks = cfg.get("hooks", {})
    if not isinstance(hooks, dict):
        return False
    has_legacy = False
    for event in ("SessionStart", "SessionEnd"):
        entries = hooks.get(event, [])
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for h in entry.get("hooks", []) or []:
                cmd = h.get("command", "") if isinstance(h, dict) else ""
                if any(marker in cmd for marker in _LEGACY_COMMAND_MARKERS):
                    has_legacy = True
    if not has_legacy:
        return False
    # Already on the modern layout → no nudge (self-upgrade is wired).
    return not workspace_has_agnes_hooks(workspace)


def maybe_refresh_claude_hooks(workspace: Path) -> bool:
    """Idempotently re-install Agnes hooks if ``workspace`` already looks like
    an Agnes workspace, otherwise no-op.

    Called from ``agnes self-upgrade`` so that operators on workspaces
    initialized with an older CLI version pick up the current hook layout
    on the next session-start, without needing to re-run ``agnes init``
    manually. This is what migrates an existing workspace off the removed
    ``agnes capture-session`` SessionStart/SessionEnd entries (CLI- or
    template-seeded) and onto the scan-based ``agnes push`` layout —
    otherwise the dead capture entry would keep shelling out to a deleted
    helper on every session.

    The guard (``workspace_has_agnes_hooks``) makes this safe to call from
    any working directory: ``agnes self-upgrade`` invoked from ``~/`` will
    not create ``~/.claude/`` or write hooks there.

    Returns True if hooks were refreshed; False if the workspace looked
    non-Agnes and we skipped.

    Runs regardless of the Initial Workspace Template sentinel
    (`override: true`). Override governs *init-time* skip only —
    runtime hook migration is unconditional so an analyst working in
    an admin-templated workspace still picks up new Agnes hook
    layouts from a stale snapshot. Admin custom hooks are preserved
    because `_replace_or_add` rewrites only entries matching
    `_OUR_COMMAND_MARKERS`; foreign commands fall through unchanged.
    """
    if not workspace_has_agnes_hooks(workspace):
        return False
    install_claude_hooks(workspace)
    return True
