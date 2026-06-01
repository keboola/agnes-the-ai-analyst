"""Slack event dispatcher — routes incoming events to handlers."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from services.slack_bot.binding import issue_verification_code, lookup_user_email
from services.slack_bot.sender import send_thread_reply
from services.slack_bot.sink import SlackSinkBridge

logger = logging.getLogger(__name__)


async def dispatch_event(app, event: dict[str, Any]) -> None:
    etype = event.get("type")
    if etype == "message":
        await _handle_dm(app, event)
    elif etype == "app_mention":
        await _handle_mention(app, event)


def _is_attached(mgr, chat_id: str) -> bool:
    """True iff `chat_id` already has a live attach (sink pumping)."""
    return any(live.chat_id == chat_id for live in mgr.list_live())


async def _handle_dm(app, event: dict) -> None:
    if event.get("channel_type") != "im" or event.get("bot_id"):
        return
    slack_user_id = event.get("user")
    text = event.get("text", "")
    channel = event["channel"]
    thread_ts = event.get("thread_ts") or event["ts"]
    repo = app.state.chat_repo
    user_email = lookup_user_email(repo, slack_user_id)
    if user_email is None:
        # First DM from an unbound user: mint a 6-digit code so the user
        # can paste it at /setup?slack=1 while logged into Agnes.  Without
        # this the bot used to say "go to /setup" with no code to redeem.
        code = issue_verification_code(repo._conn, slack_user_id=slack_user_id)
        public_url = getattr(app.state, "public_url", "")
        setup_link = f"{public_url}/setup?slack=1" if public_url else "/setup?slack=1"
        await send_thread_reply(
            channel, thread_ts,
            (
                "Welcome! To bind your Slack identity to Agnes:\n"
                f"1. Visit {setup_link} while logged in.\n"
                f"2. Paste this 6-digit code: *{code}* (expires in 10 minutes)."
            ),
        )
        return
    # Cloud chat is an RBAC resource (default-deny). A bound Slack user still
    # needs the grant on their group, same as the web surface — check before
    # spawning a session so Slack can't bypass the gate.
    from app.auth.access import can_access
    from app.resource_types import ResourceType
    from src.repositories.users import UserRepository
    _u = UserRepository(repo._conn).get_by_email(user_email)
    if not _u or not can_access(_u["id"], ResourceType.CHAT.value, "chat", repo._conn):
        await send_thread_reply(
            channel, thread_ts,
            "You don't have access to Agnes chat yet — ask an admin to grant "
            "your group access on /admin/access.",
        )
        return
    mgr = app.state.chat_manager
    from app.chat.types import Surface
    session = await mgr.create_session(
        user_email=user_email, surface=Surface.SLACK_DM, slack_channel_id=channel,
    )
    # Attach a SlackSinkBridge if no pump is running for this session yet.
    # The bridge forwards assistant_message frames to send_thread_reply so
    # the user actually sees the answer in Slack.
    if not _is_attached(mgr, session.id):
        sink = SlackSinkBridge(channel=channel, thread_ts=thread_ts)
        asyncio.create_task(mgr.attach(session.id, sink))
        # Give attach() a beat to set up the pump and emit `ready` before
        # we feed the user message into the runner stdin.
        await asyncio.sleep(0.1)
    await mgr.send_user_message(session.id, text)


async def _handle_mention(app, event: dict) -> None:
    # MVP: scope = DM only (per spec defaults). Stub for follow-up — channel
    # @agnes mentions land in a future PR. Operators who install the manifest
    # can still see events arriving at this handler in the logs.
    logger.info(
        "app_mention received but not yet implemented",
        extra={
            "channel": event.get("channel"),
            "thread_ts": event.get("thread_ts"),
            "user": event.get("user"),
        },
    )
    return
