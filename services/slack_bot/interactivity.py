"""Slack interactivity (Block Kit button clicks) parsing + routing.

Signature verification lives in app/api/slack.py; by the time a payload
reaches parse_interaction it is trusted. Handlers deliver async via the
Slack Web API / response_url and never raise (each dispatch runs under
events._run_logged, so an exception becomes a logged best-effort failure,
never a Slack retry).
"""
from __future__ import annotations

import logging
import secrets
import time as _time
from dataclasses import dataclass, field
from typing import Any

from app.chat.audit import write_audit
from services.slack_bot import blocks, sender
from services.slack_bot.binding import is_channel_allowlisted, lookup_user_email
from services.slack_bot.commands import _soft_archive_dm_for_button as _soft_archive_dm

logger = logging.getLogger(__name__)

# Share-to-channel answer store. A /agnes answer can exceed the 2000-char
# Slack button `value` cap, so only a token rides in the button; the body
# lives here keyed by token with a short TTL. In-memory + single-worker
# (chat is disabled under multiple uvicorn workers — see app/main.py).
_SHARE_TTL_SECONDS = 30 * 60
_SHARE_ANSWERS: dict[str, tuple[float, str]] = {}


def store_share_answer(text: str) -> str:
    """Stash a shareable answer body, returning its lookup token."""
    token = secrets.token_urlsafe(12)
    _SHARE_ANSWERS[token] = (_time.monotonic(), text)
    return token


def get_share_answer(token: str) -> str | None:
    """Return the stored body, or None if missing/expired (and evict it)."""
    entry = _SHARE_ANSWERS.get(token)
    if entry is None:
        return None
    stored_at, text = entry
    if (_time.monotonic() - stored_at) > _SHARE_TTL_SECONDS:
        _SHARE_ANSWERS.pop(token, None)
        return None
    return text


@dataclass(frozen=True)
class Interaction:
    action_id: str
    slack_user_id: str
    channel_id: str
    response_url: str
    value: dict[str, Any] = field(default_factory=dict)


def parse_interaction(payload: dict[str, Any]) -> Interaction:
    """Normalize a Slack block_actions payload into an Interaction.

    Only the first clicked action is considered (Agnes never bundles two
    interactive elements into one block that fire together)."""
    actions = payload.get("actions") or []
    first = actions[0] if actions else {}
    return Interaction(
        action_id=first.get("action_id", ""),
        slack_user_id=(payload.get("user") or {}).get("id", ""),
        channel_id=(payload.get("channel") or {}).get("id", ""),
        response_url=payload.get("response_url", ""),
        value=blocks.decode_value(first.get("value", "")),
    )


async def dispatch_interaction(app, interaction: Interaction) -> None:
    if interaction.action_id == blocks.ACTION_STOP:
        await _on_stop(app, interaction)
    elif interaction.action_id == blocks.ACTION_SHARE_CHANNEL:
        await _on_share(app, interaction)
    elif interaction.action_id == blocks.ACTION_NEW_SESSION:
        await _on_new_session(app, interaction)
    else:
        # Link buttons (Continue-on-web) never POST; anything else is unknown.
        logger.info("ignoring unrouted interaction action_id=%s", interaction.action_id)


async def _on_stop(app, it: Interaction) -> None:
    repo = app.state.chat_repo
    mgr = app.state.chat_manager
    clicker_email = lookup_user_email(repo, it.slack_user_id)
    chat_id = it.value.get("chat_id", "")
    owner = it.value.get("owner", "")
    if not clicker_email:
        await sender.send_ephemeral(
            it.response_url, "Bind your Slack identity first (DM Agnes to start)."
        )
        return
    if clicker_email != owner:
        await sender.send_ephemeral(
            it.response_url,
            f"This session belongs to <@{it.slack_user_id}>'s owner; only they can stop it.",
        )
        return
    await mgr.cancel(chat_id)  # idempotent; sink strips the button on `cancelled`


async def _on_share(app, it: Interaction) -> None:
    repo = app.state.chat_repo
    conn = repo._conn
    clicker_email = lookup_user_email(repo, it.slack_user_id)
    if not clicker_email:
        await sender.send_ephemeral(
            it.response_url, "Bind your Slack identity first (DM Agnes to start)."
        )
        return
    channel_id = it.value.get("channel_id", "")
    # SECURITY: re-check the allowlist at click time against the
    # signature-verified channel — never trust a stale grant or the payload's
    # display channel. is_channel_allowlisted does a direct Everyone-scoped
    # grant lookup (no admin short-circuit).
    if not is_channel_allowlisted(conn, channel_id):
        await sender.send_ephemeral(it.response_url, "Agnes can't post in this channel.")
        return
    body = get_share_answer(it.value.get("token", ""))
    if body is None:
        await sender.send_ephemeral(
            it.response_url, "That answer expired — re-run /agnes to share again."
        )
        return
    await sender.post_channel_message(channel_id, body)
    # Clear the ephemeral; the public post already landed, so a response_url
    # expiry here is non-fatal.
    try:
        await sender.respond_via_response_url(it.response_url, {"delete_original": True})
    except Exception:
        logger.warning("response_url clear failed after share (post already public)")
    write_audit(
        conn, user_email=clicker_email, action="slack_share",
        details={"channel_id": channel_id},
    )


async def _on_new_session(app, it: Interaction) -> None:
    repo = app.state.chat_repo
    clicker_email = lookup_user_email(repo, it.slack_user_id)
    owner = it.value.get("owner", "")
    channel_id = it.value.get("channel_id", "")
    if not clicker_email or clicker_email != owner:
        await sender.send_ephemeral(
            it.response_url, "This session belongs to someone else; only its owner can reset it."
        )
        return
    await _soft_archive_dm(app, owner, channel_id)
    await sender.send_ephemeral(
        it.response_url, "Started a fresh session — your next message begins anew."
    )
