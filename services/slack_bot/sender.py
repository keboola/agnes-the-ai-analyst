"""Outbound Slack API calls (chat.postMessage in a thread)."""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)


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
