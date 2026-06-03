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


def stop_button_blocks(*, text: str, chat_id: str, owner: str) -> list[dict[str, Any]]:
    """A reply section + a Stop button that cancels the live turn.

    ``value`` carries chat_id + owner so the handler authorizes the clicker
    against the session owner without a DB round-trip for ownership shape.
    """
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text or " "}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": ACTION_STOP,
                    "text": {"type": "plain_text", "text": "Stop"},
                    "style": "danger",
                    "value": encode_value({"chat_id": chat_id, "owner": owner}),
                }
            ],
        },
    ]


def continue_on_web_block(*, web_base: str, chat_id: str) -> dict[str, Any] | None:
    """A pure link button to the web deep link. No callback — Slack never
    POSTs clicks on buttons that carry a ``url``. Returns None when no
    web_base is configured (so callers simply omit the button)."""
    if not web_base:
        return None
    base = web_base.rstrip("/")
    return {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Continue on web"},
                "url": f"{base}/chat?session={chat_id}",
            }
        ],
    }


def share_to_channel_blocks(*, channel_id: str, token: str) -> list[dict[str, Any]]:
    """Share button for an ephemeral /agnes answer. The answer body is held
    server-side under ``token`` (a long answer can exceed the 2000-char value
    cap), so only the token + channel_id ride in ``value``."""
    return [
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": ACTION_SHARE_CHANNEL,
                    "text": {"type": "plain_text", "text": "Share to channel"},
                    "value": encode_value({"channel_id": channel_id, "token": token}),
                }
            ],
        }
    ]


def new_session_block(*, channel_id: str, owner: str) -> dict[str, Any]:
    """New-session button for a DM thread. Soft-archives the current DM
    session (shared path with /agnes-new)."""
    return {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "action_id": ACTION_NEW_SESSION,
                "text": {"type": "plain_text", "text": "New session"},
                "value": encode_value({"channel_id": channel_id, "owner": owner}),
            }
        ],
    }
