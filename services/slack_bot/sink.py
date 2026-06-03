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

from services.slack_bot.sender import send_ephemeral, send_thread_reply

logger = logging.getLogger(__name__)


class SlackSinkBridge:
    """Duck-typed WebSocket adapter for the ChatManager pump.

    Forwards `assistant_message` frames to Slack as a single
    `chat.postMessage` in the originating thread. Discards token / ready /
    runner_ready / tool_call / tool_result frames (too chatty for Slack);
    `error` and `cancelled` post visible thread messages so the user knows
    something happened.
    """

    def __init__(self, *, channel: str, thread_ts: str, chat_id: str | None = None) -> None:
        self._channel = channel
        self._thread_ts = thread_ts
        self._chat_id = chat_id
        self._closed = asyncio.Event()

    async def send_json(self, data: dict) -> None:
        t = data.get("type")
        if t == "assistant_message":
            content = data.get("content", "")
            if content:
                await send_thread_reply(self._channel, self._thread_ts, content)
        elif t == "error":
            kind = data.get("kind", "")
            msg = data.get("message", "")
            parts = [p for p in (kind, msg) if p]
            detail = ": ".join(parts)
            text = f":warning: {detail}" if detail else ":warning:"
            await send_thread_reply(self._channel, self._thread_ts, text)
        elif t == "cancelled":
            await send_thread_reply(self._channel, self._thread_ts, "_(stopped)_")
        # ready, runner_ready, token, tool_call, tool_result, done: silently ignored

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
