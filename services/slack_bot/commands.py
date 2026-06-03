"""Slack slash-command dispatcher — routes /agnes* commands to handlers.

Each handler delivers its answer asynchronously via the command's
response_url (30-min / 5-post limited → single-shot). /agnes help is the
only synchronous path (its body rides the 3 s ack).

This module owns its own _schedule + _run_logged (Phase 0's copies live
in events.py but are not depended upon here — verified absent at authoring
time; keeping them local makes this phase self-contained).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from services.slack_bot.binding import issue_verification_code, lookup_user_email
from services.slack_bot.sender import open_im, send_ephemeral
from services.slack_bot.sink import EphemeralCommandSink

logger = logging.getLogger(__name__)

_BG_TASKS: set = set()


def _schedule(coro) -> None:
    """Fire-and-forget a coroutine, keeping a strong ref so the GC can't
    cancel an in-flight dispatch."""
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


async def _run_logged(coro, *, response_url: Optional[str] = None) -> None:
    """Run a dispatch coroutine, swallowing + logging any unhandled
    exception. Because the endpoint acks before dispatch, an exception
    here never triggers a Slack retry — this is the only recovery path,
    so on failure post a best-effort ephemeral to the caller's
    response_url (if one was supplied)."""
    try:
        await coro
    except Exception:
        logger.exception("unhandled exception in slash-command dispatch")
        if response_url:
            try:
                await send_ephemeral(
                    response_url,
                    ":warning: Something went wrong handling that command. "
                    "Please try again.",
                )
            except Exception:
                logger.exception("failed to post error ephemeral")


def _help_body() -> str:
    return (
        "*Agnes slash commands*\n"
        "• `/agnes <question>` — ask Agnes; the answer also appears on web /chat.\n"
        "• `/agnes-new` — archive your current Agnes DM session and start fresh.\n"
        "• `/agnes-status` — show your active session count and cap.\n"
        "• `/agnes help` — show this message."
    )


async def dispatch_command(app, cmd: dict[str, Any]) -> None:
    command = (cmd.get("command") or "").strip()
    if command == "/agnes":
        await _cmd_agnes(app, cmd)
    elif command == "/agnes-new":
        await _cmd_new(app, cmd)
    elif command == "/agnes-status":
        await _cmd_status(app, cmd)
    else:
        logger.info("unknown slash command: %s", command)


def _is_attached(mgr, chat_id: str) -> bool:
    return any(live.chat_id == chat_id for live in mgr.list_live())


async def _cmd_agnes(app, cmd: dict) -> None:
    from app.auth.access import can_access
    from app.chat.manager import ConcurrencyCapHit
    from app.chat.types import Surface
    from app.resource_types import ResourceType
    from src.repositories.users import UserRepository

    repo = app.state.chat_repo
    mgr = app.state.chat_manager
    slack_user_id = cmd.get("user_id", "")
    text = (cmd.get("text") or "").strip()
    response_url = cmd.get("response_url", "")

    user_email = lookup_user_email(repo, slack_user_id)
    if user_email is None:
        code = issue_verification_code(repo._conn, slack_user_id=slack_user_id)
        public_url = getattr(app.state, "public_url", "")
        setup_link = f"{public_url}/setup?slack=1" if public_url else "/setup?slack=1"
        await send_ephemeral(
            response_url,
            "To use Agnes from Slack, bind your identity first:\n"
            f"1. Visit {setup_link} while logged in.\n"
            f"2. Paste this 6-digit code: *{code}* (expires in 10 minutes).",
        )
        return

    _u = UserRepository(repo._conn).get_by_email(user_email)
    if not _u or not can_access(_u["id"], ResourceType.CHAT.value, "chat", repo._conn):
        await send_ephemeral(
            response_url,
            "You don't have access to Agnes chat yet — ask an admin to grant "
            "your group access on /admin/access.",
        )
        return

    im_channel = await open_im(slack_user_id)
    if im_channel is None:
        await send_ephemeral(response_url, ":warning: Couldn't open a DM channel. Try again.")
        return

    try:
        session = await mgr.create_session(
            user_email=user_email, surface=Surface.SLACK_DM, slack_channel_id=im_channel,
        )
    except ConcurrencyCapHit:
        cap = mgr._config.concurrency_per_user
        await send_ephemeral(
            response_url,
            f"You're at your session limit ({cap}); run `/agnes-new` to free one.",
        )
        return

    # Attach a one-shot ephemeral sink only if no permanent sink (web/DM)
    # is already pumping — response_url is single-shot and the persistent
    # sink keeps streaming on web/DM.
    if not _is_attached(mgr, session.id):
        sink = EphemeralCommandSink(response_url=response_url)
        asyncio.create_task(mgr.attach(session.id, sink))
        await asyncio.sleep(0.1)  # let attach() set up the pump + emit ready
    await mgr.send_user_message(session.id, text)


async def _soft_archive_dm(app, slack_user_id: str) -> bool:
    """Resolve the caller's IM channel, kill + archive any live DM session.

    Returns True if a session was archived, False if none existed. Shared
    by /agnes-new and (Phase 3) the New-session button.
    """
    repo = app.state.chat_repo
    mgr = app.state.chat_manager
    im_channel = await open_im(slack_user_id)
    if im_channel is None:
        return False
    existing = repo.get_slack_dm_session(im_channel)
    if existing is None:
        return False
    try:
        await mgr.kill(existing.id, reason="agnes_new")
    except Exception:
        logger.exception("kill failed for %s during /agnes-new", existing.id)
    repo.archive_session(existing.id)
    return True


async def _cmd_new(app, cmd: dict) -> None:
    slack_user_id = cmd.get("user_id", "")
    response_url = cmd.get("response_url", "")
    # Binding/grant are enforced on the next /agnes; /agnes-new is a no-op
    # for unbound users (no DM session can exist), so we skip the gate here.
    archived = await _soft_archive_dm(app, slack_user_id)
    if archived:
        await send_ephemeral(response_url, "Archived your Agnes session — your next `/agnes` starts fresh.")
    else:
        await send_ephemeral(response_url, "No active Agnes session to archive — your next `/agnes` starts fresh.")


async def _cmd_status(app, cmd: dict) -> None:
    repo = app.state.chat_repo
    mgr = app.state.chat_manager
    slack_user_id = cmd.get("user_id", "")
    response_url = cmd.get("response_url", "")

    user_email = lookup_user_email(repo, slack_user_id)
    if user_email is None:
        code = issue_verification_code(repo._conn, slack_user_id=slack_user_id)
        public_url = getattr(app.state, "public_url", "")
        setup_link = f"{public_url}/setup?slack=1" if public_url else "/setup?slack=1"
        await send_ephemeral(
            response_url,
            "Bind your Slack identity to Agnes first:\n"
            f"1. Visit {setup_link} while logged in.\n"
            f"2. Paste this 6-digit code: *{code}* (expires in 10 minutes).",
        )
        return

    active = mgr.active_count_for_user(user_email)
    cap = mgr._config.concurrency_per_user
    public_url = getattr(app.state, "public_url", "")
    chat_link = f"{public_url}/chat" if public_url else "/chat"
    await send_ephemeral(
        response_url,
        f"*Agnes status* — active sessions: *{active}* / {cap}\n"
        f"Open the full chat UI: {chat_link}",
    )
