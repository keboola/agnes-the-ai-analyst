"""Pure Block Kit builders for Slack interactivity (Phase 3).

Leaf module: imports nothing from the other slack_bot modules. Every
interactive element carries a structured JSON ``value`` so handlers in
interactivity.py never re-parse free text. Slack caps a button ``value``
at 2000 chars — keep payloads tiny (ids + emails, never message bodies).
"""
from __future__ import annotations

import json
from typing import Any

# Single action_id namespace; the dispatcher routes on these.
ACTION_STOP = "agnes_stop"
ACTION_CONTINUE_WEB = "agnes_continue_web"
ACTION_SHARE_CHANNEL = "agnes_share_channel"
ACTION_NEW_SESSION = "agnes_new_session"


def encode_value(data: dict[str, Any]) -> str:
    """Serialize a structured button value to a compact JSON string."""
    return json.dumps(data, separators=(",", ":"))


def decode_value(raw: str) -> dict[str, Any]:
    """Parse a button value; return {} on any malformed input (fail-soft)."""
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
