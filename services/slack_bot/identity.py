"""Resolve the bot's own Slack user id once at startup via auth.test.

Stashed on app.state.slack_bot_user_id so the mention loop-guard and
_strip_bot_mention can recognise (and ignore) the bot's own posts.
"""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)


async def resolve_bot_user_id() -> str | None:
    """Return the bot's Slack user id (``user_id`` from auth.test), or None
    if the token is missing or Slack returns ``ok=false``. Never raises —
    a failure just leaves loop-guard/strip in their None-safe fallback."""
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        logger.warning("SLACK_BOT_TOKEN missing — cannot resolve bot user id")
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://slack.com/api/auth.test",
                headers={"Authorization": f"Bearer {token}"},
            )
        data = resp.json()
    except Exception:
        logger.exception("auth.test failed — bot user id unresolved")
        return None
    if not data.get("ok"):
        logger.warning("auth.test returned ok=false: %s", data.get("error"))
        return None
    return data.get("user_id")
