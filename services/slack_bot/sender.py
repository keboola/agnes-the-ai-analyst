"""Outbound Slack API calls (chat.postMessage in a thread)."""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)


async def send_ephemeral_to_user(channel: str, slack_user_id: str, text: str) -> None:
    """Post an ephemeral message visible only to ``slack_user_id`` in
    ``channel`` via chat.postEphemeral. Used for all mention denials so we
    never leak channel-enablement or thread ownership into the channel.

    Distinct from the Phase-2 ``send_ephemeral(response_url, ...)`` helper —
    that one POSTs to a slash-command response_url, this one calls the Web API.
    """
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        logger.error("SLACK_BOT_TOKEN missing — cannot post ephemeral")
        return
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            "https://slack.com/api/chat.postEphemeral",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": channel, "user": slack_user_id, "text": text},
        )


async def send_thread_reply(channel: str, thread_ts: str, text: str) -> None:
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        logger.error("SLACK_BOT_TOKEN missing — cannot reply")
        return
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": channel, "thread_ts": thread_ts, "text": text},
        )
