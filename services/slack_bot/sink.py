"""Slack ↔ ChatManager pump bridge.

ChatManager.attach() reads frames off the runner subprocess and writes
them to a WebSocket via `ws.send_json({type: ...})`. For Slack DMs there
is no WebSocket — we want assistant_message frames forwarded to
`chat.postMessage` in the originating thread instead.

`SlackSinkBridge` is a duck-typed "WebSocket" that satisfies the manager's
contract (`.send_json`, `.receive_json`, `.close`) but routes frames to
`send_thread_reply`. Token / tool_call / housekeeping frames are dropped
(too chatty for Slack); only assistant_message, error, and cancelled
become visible chat posts.
"""
from __future__ import annotations

import asyncio
import logging

from services.slack_bot.blocks import (
    continue_on_web_block,
    new_session_block,
    stop_button_blocks,
)
from services.slack_bot.sender import (
    post_thread_reply_with_blocks,
    send_ephemeral,
    send_thread_reply,
    update_message,
)

logger = logging.getLogger(__name__)


class SlackSinkBridge:
    """Duck-typed WebSocket adapter for the ChatManager pump.

    Forwards `assistant_message` frames to Slack as a single
    `chat.postMessage` in the originating thread. Discards token / ready /
    runner_ready / tool_call / tool_result frames (too chatty for Slack);
    `error` and `cancelled` post visible thread messages so the user knows
    something happened.
    """

    def __init__(
        self,
        *,
        channel: str,
        thread_ts: str,
        chat_id: str = "",
        owner: str = "",
        web_base: str = "",
    ) -> None:
        self._channel = channel
        self._thread_ts = thread_ts
        self._chat_id = chat_id
        self._owner = owner
        self._web_base = web_base
        self._closed = asyncio.Event()
        # ts of the current turn's button-bearing post, if any. Set on the
        # first assistant_message of a turn; cleared when the button is
        # stripped on cancelled / error / done (turn end).
        self._stop_msg_ts: str | None = None
        self._stop_msg_text: str = ""

    def _turn_blocks(self, content: str) -> list[dict]:
        """Reply section + Stop + Continue-on-web (if web_base) + New-session.

        This is the producer that emits the interactive buttons onto every
        DM bot reply (spec §4 "everywhere a bot reply appears")."""
        blks = stop_button_blocks(text=content, chat_id=self._chat_id, owner=self._owner)
        link = continue_on_web_block(web_base=self._web_base, chat_id=self._chat_id)
        if link is not None:
            blks.append(link)
        blks.append(new_session_block(channel_id=self._channel, owner=self._owner))
        return blks

    async def send_json(self, data: dict) -> None:
        t = data.get("type")
        if t == "assistant_message":
            content = data.get("content", "")
            if not content:
                return
            # With a chat_id we emit the interactive buttons on the streaming
            # reply and strip the Stop button at turn end. Without one, keep
            # the plain path (back-compat for callers that don't wire buttons).
            if self._chat_id and self._stop_msg_ts is None:
                ts = await post_thread_reply_with_blocks(
                    self._channel, self._thread_ts, content, self._turn_blocks(content),
                )
                self._stop_msg_ts = ts
                self._stop_msg_text = content
            else:
                await send_thread_reply(self._channel, self._thread_ts, content)
        elif t == "error":
            kind = data.get("kind", "")
            msg = data.get("message", "")
            parts = [p for p in (kind, msg) if p]
            detail = ": ".join(parts)
            text = f":warning: {detail}" if detail else ":warning:"
            await send_thread_reply(self._channel, self._thread_ts, text)
            await self._strip_stop_button()
        elif t == "cancelled":
            await send_thread_reply(self._channel, self._thread_ts, "_(stopped)_")
            await self._strip_stop_button()
        elif t == "done":
            await self._strip_stop_button()
        # ready, runner_ready, token, tool_call, tool_result: silently ignored

    async def _strip_stop_button(self) -> None:
        """Edit the turn's button-bearing post to remove the Stop button.

        Idempotent: a no-op once already stripped or if no button was posted.
        """
        if self._stop_msg_ts is None:
            return
        ts, text = self._stop_msg_ts, self._stop_msg_text
        self._stop_msg_ts = None
        self._stop_msg_text = ""
        await update_message(self._channel, ts, text, [])

    async def receive_json(self) -> dict:
        """The Slack surface is push-only from the manager's POV.

        Block until `close()` is called; then return a sentinel that lets
        the reader loop exit cleanly. The ChatManager only consumes
        `receive_json` from inside `app/api/chat.py::ws_stream`, not in
        `attach()`, so this is rarely exercised — but we implement it for
        API completeness.
        """
        await self._closed.wait()
        return {"type": "_closed"}

    async def close(self) -> None:
        self._closed.set()


class EphemeralCommandSink:
    """One-shot sink for slash commands.

    Posts the FIRST assistant_message of the turn to the caller's
    response_url, then ignores further frames. error/cancelled are also
    surfaced once so a budget/rate failure is visible. Never stays
    attached — the session's permanent sink (web/DM) keeps streaming.
    """

    def __init__(self, *, response_url: str) -> None:
        self._response_url = response_url
        self._delivered = False
        self._closed = asyncio.Event()

    async def send_json(self, data: dict) -> None:
        if self._delivered:
            return
        t = data.get("type")
        if t == "assistant_message":
            content = data.get("content", "")
            if content:
                self._delivered = True
                await send_ephemeral(self._response_url, content)
        elif t == "error":
            kind = data.get("kind", "")
            msg = data.get("message", "")
            self._delivered = True
            parts = [p for p in (kind, msg) if p]
            detail = ": ".join(parts)
            text = f":warning: {detail}" if detail else ":warning:"
            await send_ephemeral(self._response_url, text)
        elif t == "cancelled":
            self._delivered = True
            await send_ephemeral(self._response_url, "_(stopped)_")
        # ready / runner_ready / token / tool_call / tool_result / done: ignored

    async def receive_json(self) -> dict:
        await self._closed.wait()
        return {"type": "_closed"}

    async def close(self) -> None:
        self._closed.set()
