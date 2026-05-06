"""Workspace-scoped Claude Code hook installer.

Lifted from `cli/commands/analyst.py:_install_claude_hooks` (which gets
deleted in Task 18) so `agnes init` and any future caller can use it
without dragging in the deleted command module.

Design notes:
- Workspace-scoped (`<workspace>/.claude/settings.json`), NOT user-home.
  The hooks fire only when Claude Code opens this workspace.
- Idempotent: second invocation drops prior `agnes pull` / `agnes push` /
  `agnes refresh-marketplace` / `da sync` entries (matched by command
  substring) and appends fresh entries. Third-party hooks (mixed entries,
  foreign commands) are left alone.
- Uses `|| true` in the hook command so the hook never blocks a session on
  a transient sync error.
- SessionStart gets two entries (data pull + marketplace refresh) as
  *separate* hook entries rather than a single chained command. Claude
  Code runs them independently, so a failure in one (e.g. marketplace
  not yet cloned for a fresh workspace) does not skip the other.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


# Substrings that identify "our" hook commands. Includes legacy `da sync`
# so a workspace bootstrapped by an older CLI gets cleanly upgraded on the
# next `agnes init` run. New marker `agnes refresh-marketplace` is added so
# the idempotent-replace logic recognizes it as ours on re-install.
_OUR_COMMAND_MARKERS = (
    "agnes pull",
    "agnes push",
    "agnes refresh-marketplace",
    "da sync",
)


def install_claude_hooks(workspace: Path) -> None:
    """Install SessionStart->`agnes pull` + `agnes refresh-marketplace`
    and SessionEnd->`agnes push` hooks.

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
    # Other hooks don't strictly need it (the redirection is fluff for them
    # too, but they pre-date this fix and aren't worth churning).
    _replace_or_add("SessionStart", [
        "agnes pull --quiet 2>/dev/null || true",
        'bash -c "agnes refresh-marketplace --quiet 2>/dev/null || true"',
    ])
    _replace_or_add("SessionEnd", [
        "agnes push --quiet 2>/dev/null || true",
    ])

    settings_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
