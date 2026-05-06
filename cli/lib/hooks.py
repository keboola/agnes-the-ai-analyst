"""Workspace-scoped Claude Code hook installer.

Lifted from `cli/commands/analyst.py:_install_claude_hooks` (which gets
deleted in Task 18) so `agnes init` and any future caller can use it
without dragging in the deleted command module.

Design notes:
- Workspace-scoped (`<workspace>/.claude/settings.json`), NOT user-home.
  The hooks fire only when Claude Code opens this workspace.
- Idempotent: second invocation drops a prior `agnes self-upgrade` /
  `agnes pull` / `da sync` / `agnes push` entry (matched by command substring)
  and appends fresh entries.
  Third-party hooks (mixed entries, foreign commands) are left alone.
- Uses `|| true` in the hook command so the hook never blocks a session on
  a transient sync error.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


# Substrings that identify "our" hook commands. Includes legacy `da sync`
# so a workspace bootstrapped by an older CLI gets cleanly upgraded on the
# next `agnes init` run.
_OUR_COMMAND_MARKERS = ("agnes self-upgrade", "agnes pull", "agnes push", "da sync")


def install_claude_hooks(workspace: Path) -> None:
    """Install SessionStart->`agnes self-upgrade; agnes pull` and SessionEnd->`agnes push` hooks.

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

    def _replace_or_add(event: str, command: str) -> None:
        existing = hooks.setdefault(event, [])
        for entry in list(existing):
            entry_cmds = [h.get("command", "") for h in entry.get("hooks", [])]
            if entry_cmds and all(
                any(marker in c for marker in _OUR_COMMAND_MARKERS) for c in entry_cmds
            ):
                existing.remove(entry)
        existing.append({"hooks": [{"type": "command", "command": command}]})

    _replace_or_add(
        "SessionStart",
        "agnes self-upgrade --quiet 2>/dev/null || true; "
        "agnes pull --quiet 2>/dev/null || true",
    )
    _replace_or_add("SessionEnd", "agnes push --quiet 2>/dev/null || true")

    settings_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
