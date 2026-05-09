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
    1. Chained `agnes self-upgrade; agnes pull` — self-upgrade runs first
       so any wire-protocol bump lands before pull tries to use the new
       CLI version. Both `|| true`-guarded so an upgrade failure doesn't
       block the pull.
    2. `agnes refresh-marketplace --check` — independent entry. Detector-
       only (since the slash-command split): runs `git fetch` against the
       marketplace clone and emits a Claude Code hook JSON message
       hinting the user at `/update-agnes-plugins` when remote content
       changed. Does NOT install/update plugins itself — the slash
       command does that interactively, with full output visible in the
       Claude Code transcript and under user control. Failure (no clone,
       no token) silently no-ops via the surrounding `|| true`.
    3. `agnes push` — uploads any session JSONLs that haven't reached the
       server yet (orphans from `claude -p` headless mode where Claude Code
       does NOT fire SessionEnd, or from abnormal session exits). Symmetric
       with `agnes pull` so the workspace heals on the next interactive
       session start.

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
    "da sync",
)


def install_claude_hooks(workspace: Path) -> None:
    """Install SessionStart hooks (`agnes self-upgrade; agnes pull` chained
    + `agnes refresh-marketplace` as a separate entry) and SessionEnd hook
    (`agnes push`).

    Idempotent. Workspace-scoped (writes `<workspace>/.claude/settings.json`).
    Preserves third-party hooks and other event types.
    """
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

    # `refresh-marketplace` is wrapped in `bash -c` because Claude Code on
    # Windows runs hook commands directly (no shell), so the `2>/dev/null
    # || true` redirection + short-circuit syntax never gets interpreted.
    # The self-upgrade+pull chained entry pre-dates the Windows fix and
    # isn't churned for parity (the same redirection fluff applies but
    # changing the existing wire would force every workspace to re-write
    # its settings.json on the next `agnes init` for no behaviour gain).
    #
    # `--check` makes the marketplace entry a detector only: the actual
    # plugin install/update happens in the `/update-agnes-plugins` slash
    # command (installed by `cli.lib.commands.install_claude_commands`).
    # Workspaces still on the older `--quiet` form auto-upgrade here
    # because `_OUR_COMMAND_MARKERS` matches by substring on the
    # `agnes refresh-marketplace` prefix.
    _replace_or_add("SessionStart", [
        "agnes self-upgrade --quiet 2>/dev/null || true; "
        "agnes pull --quiet 2>/dev/null || true",
        'bash -c "agnes refresh-marketplace --check 2>/dev/null || true"',
        'bash -c "agnes push --quiet 2>/dev/null || true"',
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

    settings_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
