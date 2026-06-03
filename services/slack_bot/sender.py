"""Outbound Slack API calls (chat.postMessage in a thread)."""
from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


async def send_ephemeral(
    response_url: str, text: str, blocks: Optional[list] = None,
) -> None:
    """Deliver an ephemeral message to a slash command's response_url.

    response_url is limited to ~30 min / 5 posts — single-shot use only.
    No bot token needed: the URL itself authorizes the post.
    """
    payload: dict = {"response_type": "ephemeral", "text": text}
    if blocks is not None:
        payload["blocks"] = blocks
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(response_url, json=payload)


async def open_im(slack_user_id: str) -> Optional[str]:
    """Resolve a user's DM channel id via conversations.open.

    A slash command fired in a public channel carries that channel's id,
    not the DM channel — keying a SLACK_DM session on it would break
    dedup. Returns the IM channel id, or None on missing token / error.
    """
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        logger.error("SLACK_BOT_TOKEN missing — cannot open IM")
        return None
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            "https://slack.com/api/conversations.open",
            headers={"Authorization": f"Bearer {token}"},
            json={"users": slack_user_id},
        )
    try:
        data = resp.json()
    except Exception:
        logger.exception("conversations.open returned non-JSON")
        return None
    if not data.get("ok"):
        logger.error("conversations.open failed: %s", data.get("error"))
        return None
    return data.get("channel", {}).get("id")


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
