"""Slack event dispatcher — routes incoming events to handlers."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Awaitable, Callable, Coroutine, Optional

from services.slack_bot.binding import (
    issue_verification_code,
    is_channel_allowlisted,
    lookup_user_email,
)
from services.slack_bot.sender import send_ephemeral_to_user, send_thread_reply
from services.slack_bot.sink import SlackSinkBridge

logger = logging.getLogger(__name__)

# Strong references to every scheduled dispatch task. asyncio only keeps a
# weak ref to a bare create_task() result, so a fire-and-forget task can be
# GC-collected (and cancelled) mid-flight. Holding it here until the
# done-callback discards it guarantees the dispatch runs to completion.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _schedule(coro: "Coroutine[Any, Any, Any]") -> asyncio.Task:
    """Schedule a coroutine on the running loop, retaining a strong ref.

    Used at every transport's dispatch call site (HTTP endpoint + Socket
    Mode) so the slow body runs *after* the 3s Slack ack has been sent.
    """
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


async def _run_logged(
    coro: "Coroutine[Any, Any, Any]",
    *,
    on_failure: Optional[Callable[[BaseException], Awaitable[None]]] = None,
) -> None:
    """Wrap a scheduled dispatch coroutine — the ONLY recovery path.

    Because we ack Slack *before* processing (ack-then-async), a failure
    here does NOT trigger a Slack retry. So this wrapper must (a) never let
    the exception escape — an escaped exception surfaces as an asyncio
    "Task exception was never retrieved" and silently drops the work — and
    (b) drive the best-effort user-visible recovery notice.

    ``on_failure`` is that recovery seam: an awaitable the caller supplies
    to post a user-visible ephemeral with the failure. It is itself
    best-effort — a notifier that raises is caught and logged, never
    propagated. Phase 0 call sites pass ``on_failure=None`` (the HTTP DM
    handler emits its own binding/error replies inline, and the context-free
    dispatch here carries no channel/response_url to post to, and the
    ``send_ephemeral`` helper does not exist until Phase 2). Later phases
    (mentions/slash/interactivity), which have channel/response_url context,
    pass an ``on_failure`` that posts the ephemeral. The seam is wired and
    tested now; only the concrete ephemeral payload is deferred.
    """
    try:
        await coro
    except Exception as exc:  # noqa: BLE001 — last line of defence for a detached task
        logger.exception("scheduled Slack dispatch failed")
        if on_failure is not None:
            try:
                await on_failure(exc)
            except Exception:  # noqa: BLE001 — recovery notice is best-effort
                logger.exception("best-effort Slack failure notice failed")


async def dispatch_event(app, event: dict[str, Any]) -> None:
    etype = event.get("type")
    if etype == "message":
        await _handle_dm(app, event)
    elif etype == "app_mention":
        await _handle_mention(app, event)


def _strip_bot_mention(text: str, bot_user_id: str | None) -> str:
    """Remove the bot's own ``<@ID>`` / ``<@ID|label>`` mention token(s) from
    an app_mention text body and return the trimmed remainder.

    ``bot_user_id`` None (not yet resolved) → just trim — never echo the raw
    ``<@…>`` token into the runner.
    """
    if not text:
        return ""
    if bot_user_id:
        text = re.sub(rf"<@{re.escape(bot_user_id)}(?:\|[^>]*)?>", "", text)
    return text.strip()


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
        _schedule(mgr.attach(session.id, sink))
        # Give attach() a beat to set up the pump and emit `ready` before
        # we feed the user message into the runner stdin.
        await asyncio.sleep(0.1)
    await mgr.send_user_message(session.id, text)


async def _handle_mention(app, event: dict) -> None:
    """Channel @agnes mention → public in-thread reply on a persistent
    SLACK_THREAD session owned by the mention starter. Gated by the
    per-channel allowlist (default-deny). All denials are ephemeral.
    """
    # 2. Bot loop-guard: ignore our own / any bot's posts.
    bot_user_id = getattr(app.state, "slack_bot_user_id", None)
    if event.get("bot_id") or (bot_user_id and event.get("user") == bot_user_id):
        return

    channel = event["channel"]
    thread_ts = event.get("thread_ts") or event["ts"]
    slack_user_id = event.get("user")
    text = event.get("text", "")
    repo = app.state.chat_repo
    conn = repo._conn

    # 3. Allowlist (direct Everyone grant — never can_access).
    if not is_channel_allowlisted(conn, channel):
        await send_ephemeral_to_user(
            channel, slack_user_id, "Agnes isn't enabled in this channel."
        )
        return

    # 4. Identity binding.
    user_email = lookup_user_email(repo, slack_user_id)
    if user_email is None:
        code = issue_verification_code(conn, slack_user_id=slack_user_id)
        public_url = getattr(app.state, "public_url", "")
        setup_link = f"{public_url}/setup?slack=1" if public_url else "/setup?slack=1"
        await send_ephemeral_to_user(
            channel, slack_user_id,
            (
                "To use Agnes here, bind your Slack identity:\n"
                f"1. Visit {setup_link} while logged in.\n"
                f"2. Paste this 6-digit code: *{code}* (expires in 10 minutes)."
            ),
        )
        return

    # 5. CHAT grant.
    from app.auth.access import can_access
    from app.resource_types import ResourceType
    from src.repositories.users import UserRepository
    _u = UserRepository(conn).get_by_email(user_email)
    if not _u or not can_access(_u["id"], ResourceType.CHAT.value, "chat", conn):
        await send_ephemeral_to_user(
            channel, slack_user_id,
            "You don't have access to Agnes chat yet — ask an admin to grant "
            "your group access on /admin/access.",
        )
        return

    # 6. Thread session: reuse or create; reject if owned by someone else.
    mgr = app.state.chat_manager
    from app.chat.types import Surface
    existing = repo.get_slack_thread_session(channel, thread_ts)
    if existing is not None and existing.user_email != user_email:
        owner_row = conn.execute(
            "SELECT slack_user_id FROM users WHERE email = ?",
            [existing.user_email],
        ).fetchone()
        owner_ref = f"<@{owner_row[0]}>" if owner_row and owner_row[0] else "another user"
        await send_ephemeral_to_user(
            channel, slack_user_id, f"This thread belongs to {owner_ref}."
        )
        return
    session = await mgr.create_session(
        user_email=user_email,
        surface=Surface.SLACK_THREAD,
        slack_channel_id=channel,
        slack_thread_ts=thread_ts,
    )

    # 7. Strip our own mention token.
    clean = _strip_bot_mention(text, bot_user_id)

    # 8. Attach (NOT awaited — keep the 3s ack budget).
    if not _is_attached(mgr, session.id):
        sink = SlackSinkBridge(channel=channel, thread_ts=thread_ts, chat_id=session.id)
        asyncio.create_task(mgr.attach(session.id, sink))
        await asyncio.sleep(0.1)

    # 9. Inject the user turn. send_user_message(chat_id, text) — no sender_email
    #    (per-sender attribution arrives with Phase 5a's multi-sink refactor).
    await mgr.send_user_message(session.id, clean)
