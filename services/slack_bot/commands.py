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

from services.slack_bot.sender import send_ephemeral

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


async def _cmd_agnes(app, cmd: dict) -> None:  # implemented in Task 6
    raise NotImplementedError


async def _cmd_new(app, cmd: dict) -> None:  # implemented in Task 7
    raise NotImplementedError


async def _cmd_status(app, cmd: dict) -> None:  # implemented in Task 8
    raise NotImplementedError
