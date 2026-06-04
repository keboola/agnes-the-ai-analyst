#!/usr/bin/env python3
"""Bundled PreToolUse safety hook.

Reads a JSON payload from stdin per the Claude Code hook spec, returns
a JSON decision object on stdout. Refuses workspace-destructive Bash
commands, hosts outside the Agnes egress allowlist, and prompts for
admin mutations.

Operators with an Initial Workspace Template override take
responsibility for shipping an equivalent hook (admin UI warns at
template upload time if absent).
"""
from __future__ import annotations

import json
import re
import sys

ALLOWED_HOSTS = {
    "127.0.0.1", "localhost",
    "api.anthropic.com",
    "api.github.com",
}

DESTRUCTIVE_PATHS = ("workspace/snapshots/", "workspace/scripts/")
DESTRUCTIVE_PREFIXES = ("rm ", "rm\t", "unlink ", "truncate -s 0", "shred ")

ADMIN_PROMPT_PREFIXES = (
    "agnes admin grant",
    "agnes admin group",
    "agnes admin user",
)


def _decide(payload: dict) -> dict:
    tool = payload.get("tool_name")
    if tool != "Bash":
        return {"permissionDecision": "allow"}
    cmd = (payload.get("tool_input") or {}).get("command", "")
    if not isinstance(cmd, str):
        return {"permissionDecision": "allow"}

    lower = cmd.strip().lower()

    # Destructive ops against persistent workspace dirs
    if any(p in cmd for p in DESTRUCTIVE_PATHS) and any(
        lower.startswith(pref) for pref in DESTRUCTIVE_PREFIXES
    ):
        return {
            "permissionDecision": "deny",
            "permissionDecisionReason":
                "Refusing to delete from persistent workspace/snapshots or workspace/scripts. "
                "Use a fresh path or ask the user explicitly.",
        }

    # Outbound network — block hosts outside allowlist
    for url in re.findall(r"https?://([^/\s'\"]+)", cmd):
        host = url.split(":")[0]
        if host not in ALLOWED_HOSTS:
            return {
                "permissionDecision": "deny",
                "permissionDecisionReason":
                    f"Outbound network to {host!r} is not in the Agnes egress allowlist. "
                    "Allowed: " + ", ".join(sorted(ALLOWED_HOSTS)),
            }

    # Admin mutations need user confirmation
    if any(lower.startswith(p) for p in ADMIN_PROMPT_PREFIXES):
        return {
            "permissionDecision": "ask",
            "permissionDecisionReason":
                "This command mutates the Agnes access-control layer; confirm before running.",
        }

    return {"permissionDecision": "allow"}


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}
    sys.stdout.write(json.dumps(_decide(payload)))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
