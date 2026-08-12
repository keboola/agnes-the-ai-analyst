"""Keep a stdio MCP subprocess alive between calls.

Measured on a live instance (2026-08-12), against Keboola's MCP server run
through ``uv``: **~6 s per tool call**, of which ``uv`` itself is 0.13 s. The
cost is the upstream's own import tree — ``import fastmcp`` alone is 2.4 s,
``keboola_mcp_server.server`` 5.6 s. Nothing about how we launch it can move
that number; the only thing that can is not paying it on every call. An agent
that reaches for five tools in one answer was spending half a minute in
process startup.

So a stdio session is kept warm and reused. Three properties make that safe:

**The key is the whole launch spec, secret included.** Not the source id. A
rotated token, an edited ``env``, a bumped runner version — each produces a
different key, so the next call builds a fresh process and the stale one ages
out on its own. There is no invalidation hook to forget to call. It also means
a ``scope='per_user'`` source cannot hand one analyst's warm process to
another: their resolved credentials differ, so their keys differ.

**Calls on one session are serialized.** The MCP session multiplexes by
request id, but the streams underneath are not documented as safe for
concurrent writers, and serializing costs nothing today — every caller was
already paying a full process spawn, so nobody was concurrent.

**A failed call evicts, and never retries.** Retrying is the caller's
decision, because a retry of a mutating tool can run it twice. The pool only
guarantees that the *next* acquire does not inherit a broken process.

Set ``AGNES_MCP_SESSION_POOL=0`` to turn reuse off and go back to a process
per call.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator, Dict

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger(__name__)

#: How long an unused session is kept before it is closed. Long enough to
#: cover one agent turn's worth of tool calls, short enough that an idle
#: instance is not holding subprocesses (and their memory) indefinitely.
IDLE_TIMEOUT_S = float(os.environ.get("AGNES_MCP_SESSION_IDLE_S", "180"))

#: Upper bound on live pooled sessions. Each one is a Python process with the
#: upstream's whole import tree resident, so this is a memory bound as much as
#: a tidiness one. Beyond it, the least recently used is closed.
MAX_SESSIONS = int(os.environ.get("AGNES_MCP_SESSION_POOL_MAX", "8"))


def pool_enabled() -> bool:
    return (os.environ.get("AGNES_MCP_SESSION_POOL", "1") or "").strip().lower() not in ("0", "false", "no")


def spec_key(params: StdioServerParameters) -> str:
    """Fingerprint of everything that decides what process this would be.

    The resolved secret is part of it *by design* — see the module docstring.
    Hashed rather than stored, so the pool's bookkeeping never holds a
    credential in plain form, and never logs one.
    """
    material = json.dumps(
        {
            "command": str(params.command),
            "args": [str(a) for a in (params.args or [])],
            # Coerced rather than dumped as-is: this must never raise. A key
            # that cannot be computed would take down a tool call over a value
            # `json` happens not to like, and the only thing being asked of
            # these values is "did they change".
            "env": {str(k): str(v) for k, v in (params.env or {}).items()},
        },
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass
class _Entry:
    session: ClientSession
    close: asyncio.Event
    closed: asyncio.Future
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = field(default_factory=time.monotonic)


class _Pool:
    def __init__(self) -> None:
        self._entries: Dict[str, _Entry] = {}
        self._guard = asyncio.Lock()

    async def _spawn(self, params: StdioServerParameters) -> _Entry:
        """Start a session owned by its own task.

        ``stdio_client`` and ``ClientSession`` are async context managers with
        anyio task groups inside, so they must be entered and exited by the
        same task. A keeper task owns both and parks on an event; everyone
        else just uses the session it publishes.
        """
        loop = asyncio.get_running_loop()
        ready: asyncio.Future = loop.create_future()
        close = asyncio.Event()
        closed: asyncio.Future = loop.create_future()

        async def _keeper() -> None:
            try:
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        ready.set_result(session)
                        await close.wait()
            except Exception as exc:  # noqa: BLE001 — surfaced through `ready`/`closed`
                if not ready.done():
                    ready.set_exception(exc)
                else:
                    logger.warning("pooled MCP session ended: %s", exc, exc_info=True)
            finally:
                if not closed.done():
                    closed.set_result(None)

        task = asyncio.ensure_future(_keeper())
        try:
            session = await ready
        except Exception:
            close.set()
            await asyncio.wait_for(asyncio.shield(closed), timeout=10)
            task.cancel()
            raise
        return _Entry(session=session, close=close, closed=closed)

    async def _close_entry(self, key: str, entry: _Entry) -> None:
        self._entries.pop(key, None)
        entry.close.set()
        try:
            await asyncio.wait_for(asyncio.shield(entry.closed), timeout=10)
        except (asyncio.TimeoutError, Exception):  # noqa: B014 — teardown is best-effort
            logger.debug("pooled MCP session did not close cleanly", exc_info=True)

    async def _evict_stale(self) -> None:
        now = time.monotonic()
        for key, entry in list(self._entries.items()):
            if entry.lock.locked():
                continue
            if now - entry.last_used > IDLE_TIMEOUT_S:
                await self._close_entry(key, entry)

    async def _enforce_cap(self) -> None:
        idle = [(k, e) for k, e in self._entries.items() if not e.lock.locked()]
        while len(self._entries) > MAX_SESSIONS and idle:
            idle.sort(key=lambda kv: kv[1].last_used)
            key, entry = idle.pop(0)
            await self._close_entry(key, entry)

    @asynccontextmanager
    async def acquire(self, params: StdioServerParameters) -> AsyncIterator[ClientSession]:
        key = spec_key(params)
        async with self._guard:
            await self._evict_stale()
            entry = self._entries.get(key)
            if entry is None:
                entry = await self._spawn(params)
                self._entries[key] = entry
                await self._enforce_cap()
        async with entry.lock:
            entry.last_used = time.monotonic()
            try:
                yield entry.session
            except Exception:
                # Do NOT retry here: re-running a mutating tool because its
                # transport hiccuped is worse than the error. Just make sure
                # the next caller does not inherit a broken process.
                async with self._guard:
                    if self._entries.get(key) is entry:
                        await self._close_entry(key, entry)
                raise
            finally:
                entry.last_used = time.monotonic()

    async def aclose(self) -> None:
        async with self._guard:
            for key, entry in list(self._entries.items()):
                await self._close_entry(key, entry)


_pool = _Pool()


def get_pool() -> _Pool:
    return _pool


async def close_all() -> None:
    """Close every pooled session (process shutdown, and test teardown)."""
    await _pool.aclose()
