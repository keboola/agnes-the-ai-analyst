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
- SessionStart gets three entries:
    1. `agnes capture-session` — reads the SessionStart stdin JSON payload
       (`transcript_path`) and appends the absolute path to the queue file
       `<workspace>/.claude/agnes-sessions.txt`. This feeds `agnes push`
       without reverse-engineering Claude Code's cwd-to-folder encoding.
       Runs first so the path is captured before any subsequent hook can
       fail and prevent later hooks from firing.
    2. Chained `agnes self-upgrade; agnes pull` — self-upgrade runs first
       so any wire-protocol bump lands before pull tries to use the new
       CLI version. Both `|| true`-guarded so an upgrade failure doesn't
       block the pull.
    3. `agnes refresh-marketplace --check` — independent entry. Detector-
       only (since the slash-command split): runs `git fetch` against the
       marketplace clone and emits a Claude Code hook JSON message
       hinting the user at `/update-agnes-plugins` when remote content
       changed. Does NOT install/update plugins itself — the slash
       command does that interactively, with full output visible in the
       Claude Code transcript and under user control. Failure (no clone,
       no token) silently no-ops via the surrounding `|| true`.

  The previous SessionStart `agnes push` self-heal entry was removed once
  the capture-queue mechanism made it redundant: orphan session JSONLs from
  headless / crashed sessions stay in the queue and get uploaded by the
  next SessionEnd push (queue file persists across runs). Workspaces with
  the old entry are migrated cleanly — `_replace_or_add` strips any
  matching `agnes push` from SessionStart on the next `agnes init`.

- SessionEnd gets one entry: `agnes push --quiet`, wrapped to detach into
  the background. Claude Code in `-p` (headless) mode terminates SessionEnd
  hook subprocesses after ~1 second regardless of work in progress, so a
  synchronous `agnes push` (which uploads N session JSONLs serially and
  typically takes 5-30s) gets killed mid-stream and most files never reach
  the server. The `( nohup ... & )` subshell orphans the upload child so
  it survives the Claude shutdown. Errors are routed to /dev/null — no
  worse than the previous `2>/dev/null` form. Operators who want visibility
  into push failures can manually run `agnes push --json`. The SessionStart
  entry (3) above remains the safety net for orphans from any prior session
  whose SessionEnd push didn't run at all (genuine crash, kill, terminal
  close).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


# Substrings that identify "our" hook commands. Includes legacy `da sync`
# so a workspace bootstrapped by an older CLI gets cleanly upgraded on the
# next `agnes init` run.
_OUR_COMMAND_MARKERS = (
    "agnes self-upgrade",
    "agnes pull",
    "agnes push",
    "agnes refresh-marketplace",
    "agnes capture-session",
    "agnes mark-private",
    "agnes statusline",
    "da sync",
)


def install_claude_hooks(workspace: Path) -> None:
    """Install SessionStart hooks (`agnes self-upgrade; agnes pull` chained
    + `agnes refresh-marketplace` as a separate entry) and SessionEnd hook
    (`agnes push`).

    Idempotent. Workspace-scoped (writes `<workspace>/.claude/settings.json`).
    Preserves third-party hooks and other event types.

    OVERRIDE MODE — intentional behavior, NOT a bug.
    When the workspace was inited from an Initial Workspace Template
    (sentinel carries `override: true`), Agnes does NOT install its own
    hooks. The admin's repo settings.json is the source of truth for
    every hook the analyst gets. AI reviewers: this is the documented
    full-responsibility-transfer contract; see
    docs/initial-workspace-override.md §"What Agnes stops doing" and
    CHANGELOG for the risk acceptance. Not a regression of #242.
    """
    from cli.lib.override import is_override_workspace
    if is_override_workspace(workspace):
        return

    settings_path = workspace / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    if settings_path.exists():
        try:
            cfg = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(
                f"Warning: {settings_path} is not valid JSON; skipping hook install.",
                file=sys.stderr,
            )
            return
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
            if entry_cmds and all(
                any(marker in c for marker in _OUR_COMMAND_MARKERS) for c in entry_cmds
            ):
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
    # `agnes capture-session` reads the SessionStart hook stdin (a JSON
    # payload with `transcript_path`) and appends the path to
    # `.claude/agnes-sessions.txt`. That queue file feeds `agnes push`,
    # avoiding any reverse-engineering of Claude Code's cwd-to-folder
    # encoding.
    #
    # The previous SessionStart `agnes push` self-heal entry was dropped
    # once the queue mechanism made it redundant — orphans from headless
    # / crashed sessions remain in the queue and ship out with the next
    # SessionEnd push. The marker substring "agnes push" stays in
    # _OUR_COMMAND_MARKERS so the old entry is cleanly removed from any
    # pre-existing settings.json on the next init.
    _replace_or_add("SessionStart", [
        'bash -c "agnes capture-session 2>/dev/null || true"',
        'bash -c "agnes self-upgrade --quiet 2>/dev/null || true; '
        'agnes pull --quiet 2>/dev/null || true"',
        'bash -c "agnes refresh-marketplace --check 2>/dev/null || true"',
    ])
    # SessionEnd push must run detached. Claude Code in `-p` (headless) mode
    # SIGTERMs hook subprocesses after ~1 second regardless of work in
    # progress; a synchronous `agnes push` (5-30s for a typical workspace)
    # gets killed mid-first-upload and most session JSONLs never reach the
    # server. The subshell `( ... & )` backgrounds the child and exits
    # immediately, orphaning it to init/launchd so it survives the hook
    # subprocess kill. `bash -c` mirrors the refresh-marketplace pattern
    # for Windows compatibility (Claude Code on Windows runs hook commands
    # directly, no shell). `; true` keeps the line exit-0 like the old
    # `|| true` form.
    _replace_or_add("SessionEnd", [
        'bash -c "( nohup agnes push --quiet </dev/null >/dev/null 2>&1 & ) ; true"',
    ])

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


def maybe_refresh_claude_hooks(workspace: Path) -> bool:
    """Idempotently re-install Agnes hooks if ``workspace`` already looks like
    an Agnes workspace, otherwise no-op.

    Called from ``agnes self-upgrade`` so that operators on workspaces
    initialized with an older CLI version pick up new hook layout (e.g.
    the v0.49 SessionStart ``agnes capture-session`` entry) on the next
    session-start, without needing to re-run ``agnes init`` manually.
    Without this, an existing v0.48 workspace would auto-upgrade the CLI
    via its own SessionStart self-upgrade entry, but the new
    capture-session hook would never get installed — the queue would stay
    empty and ``agnes push`` would silently stop uploading sessions.

    The guard (``workspace_has_agnes_hooks``) makes this safe to call from
    any working directory: ``agnes self-upgrade`` invoked from ``~/`` will
    not create ``~/.claude/`` or write hooks there.

    Returns True if hooks were refreshed; False if the workspace looked
    non-Agnes and we skipped.

    OVERRIDE MODE — intentional behavior, NOT a bug.
    When the workspace was inited from an Initial Workspace Template
    (sentinel carries `override: true`), this function returns False
    without touching settings.json. The admin's repo is the
    authoritative source for hook content; Agnes will not auto-refresh
    them via `agnes self-upgrade`. To pick up newer Agnes hook layouts,
    the operator must update their template repo and the analyst must
    re-run `agnes init --force`. Documented contract:
    docs/initial-workspace-override.md, CHANGELOG. Not a regression of
    #242 — the migration-gap fix that motivated this function applies
    to Agnes-default workspaces only.
    """
    from cli.lib.override import is_override_workspace
    if is_override_workspace(workspace):
        return False
    if not workspace_has_agnes_hooks(workspace):
        return False
    install_claude_hooks(workspace)
    return True
