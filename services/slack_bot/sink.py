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

from services.slack_bot.sender import send_thread_reply

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
            await send_thread_reply(
                self._channel, self._thread_ts, f":warning: {kind}: {msg}".strip(": ")
            )
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
