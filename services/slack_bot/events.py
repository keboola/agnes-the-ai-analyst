"""Slack event dispatcher — routes incoming events to handlers."""
from __future__ import annotations

import logging
from typing import Any

from services.slack_bot.binding import lookup_user_email
from services.slack_bot.sender import send_thread_reply

logger = logging.getLogger(__name__)


async def dispatch_event(app, event: dict[str, Any]) -> None:
    etype = event.get("type")
    if etype == "message":
        await _handle_dm(app, event)
    elif etype == "app_mention":
        await _handle_mention(app, event)


async def _handle_dm(app, event: dict) -> None:
    if event.get("channel_type") != "im" or event.get("bot_id"):
        return
    slack_user_id = event.get("user")
    text = event.get("text", "")
    repo = app.state.chat_repo
    user_email = lookup_user_email(repo, slack_user_id)
    if user_email is None:
        await send_thread_reply(
            event["channel"], event["ts"],
            "I don't know who you are yet. Please bind your Slack to Agnes "
            "via the /setup page; you'll get a verification code to paste.",
        )
        return
    mgr = app.state.chat_manager
    from app.chat.types import Surface
    session = await mgr.create_session(
        user_email=user_email, surface=Surface.SLACK_DM,
        slack_channel_id=event["channel"],
    )
    await mgr.send_user_message(session.id, text)


async def _handle_mention(app, event: dict) -> None:
    # MVP: scope = DM only (per spec defaults). Stub for follow-up.
    return
