"""Outbound Slack API calls (chat.postMessage in a thread)."""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from services.slack_bot.secrets import slack_secret

logger = logging.getLogger(__name__)


async def send_ephemeral(
    response_url: str,
    text: str,
    blocks: Optional[list] = None,
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
    token = slack_secret("SLACK_BOT_TOKEN")
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
    token = slack_secret("SLACK_BOT_TOKEN")
    if not token:
        logger.error("SLACK_BOT_TOKEN missing — cannot post ephemeral")
        return
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            "https://slack.com/api/chat.postEphemeral",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": channel, "user": slack_user_id, "text": text},
        )


async def post_thread_reply_with_blocks(
    channel: str,
    thread_ts: str,
    text: str,
    blocks: list[dict],
) -> str | None:
    """Post a threaded reply with Block Kit blocks; return the message ts
    (so the caller can later chat.update it to strip the buttons), or None
    on failure."""
    token = slack_secret("SLACK_BOT_TOKEN")
    if not token:
        logger.error("SLACK_BOT_TOKEN missing — cannot reply")
        return None
    # Empty thread_ts (e.g. a SlackSinkBridge re-established by the owner
    # from a forwarded /agnes command, which has no message ts) → post a
    # top-level message; Slack rejects an empty-string thread_ts.
    payload: dict = {"channel": channel, "text": text, "blocks": blocks}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )
    data = resp.json()
    if not data.get("ok"):
        logger.error("chat.postMessage failed: %s", data.get("error"))
        return None
    return data.get("ts")


async def update_message(channel: str, ts: str, text: str, blocks: list[dict]) -> None:
    """Edit an existing message (used to strip the Stop button at turn end)."""
    token = slack_secret("SLACK_BOT_TOKEN")
    if not token:
        logger.error("SLACK_BOT_TOKEN missing — cannot update")
        return
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            "https://slack.com/api/chat.update",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": channel, "ts": ts, "text": text, "blocks": blocks},
        )
    data = resp.json()
    if data.get("ok") is False:
        logger.warning("chat.update failed: %s", data.get("error"))


async def post_channel_message(channel: str, text: str) -> None:
    """Public, non-threaded channel post (Share-to-channel promotion)."""
    token = slack_secret("SLACK_BOT_TOKEN")
    if not token:
        logger.error("SLACK_BOT_TOKEN missing — cannot post")
        return
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": channel, "text": text},
        )


async def respond_via_response_url(response_url: str, body: dict) -> None:
    """POST a raw body to a Slack response_url (clear-ephemeral, ephemeral
    fallback). 30-min / 5-post limited — single-shot use only."""
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(response_url, json=body)


async def add_reaction(channel: str, ts: str, emoji: str) -> None:
    """Best-effort emoji reaction on a message (reactions.add).

    Used as the instant acknowledgement when a mention routes to a bound
    agent profile — the session spawn takes seconds, the reaction must not.
    Failures (missing `reactions:write` scope, already_reacted, deleted
    message) are logged and swallowed: an ack must never block or fail the
    session path it decorates.
    """
    token = slack_secret("SLACK_BOT_TOKEN")
    if not token:
        logger.error("SLACK_BOT_TOKEN missing — cannot react")
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://slack.com/api/reactions.add",
                headers={"Authorization": f"Bearer {token}"},
                json={"channel": channel, "timestamp": ts, "name": emoji},
            )
        body = resp.json()
        if not body.get("ok") and body.get("error") != "already_reacted":
            logger.warning("reactions.add failed for %s/%s: %s", channel, ts, body.get("error"))
    except Exception:
        logger.warning("reactions.add errored for %s/%s", channel, ts, exc_info=True)


async def send_thread_reply(channel: str, thread_ts: str, text: str) -> None:
    token = slack_secret("SLACK_BOT_TOKEN")
    if not token:
        logger.error("SLACK_BOT_TOKEN missing — cannot reply")
        return
    # Empty thread_ts → top-level message (see post_thread_reply_with_blocks).
    payload: dict = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )
