"""Keep a stdio MCP subprocess alive between calls.

Measured on a live instance (2026-08-12), against a connected project's MCP
server run through ``uv``: **~6 s per tool call**, of which ``uv`` itself is
0.13 s. The cost is the upstream's own import tree — ``import fastmcp`` alone
is 2.4 s, its server module 5.6 s. Nothing about how we launch it can move
that number; the only thing that can is not paying it on every call. An agent
that reaches for five tools in one answer was spending half a minute in
process startup.

So a stdio session is kept warm and reused. The properties that make that
safe, each pinned by a test in ``tests/test_mcp_session_pool.py``:

**The key is the whole launch spec, secret included.** Not the source id. A
rotated token, an edited ``env``, a bumped runner version — each produces a
different key, so the next call builds a fresh process and the stale one ages
out on its own. There is no invalidation hook to forget to call. It also means
a ``scope='per_user'`` source cannot hand one analyst's warm process to
another: their resolved credentials differ, so their keys differ.

**A session is only ever reused on the event loop that created it.** The
session, its keeper task, and the ``Event``/``Future`` it parks on all belong
to that loop; the sync wrappers (``list_tools``, ``call_tool``,
``_materialize_one_tool``) run one ``asyncio.run`` per call, and closing that
loop cancels the keeper — which tears the subprocess down. Bookkeeping is
therefore per running loop, and a loop that is closed takes its entries with
it. Same shape as ``_get_oauth_refresh_lock`` in ``client.py``, for the same
reason.

**Calls on one session are serialized.** The MCP session multiplexes by
request id, but the streams underneath are not documented as safe for
concurrent writers, and serializing costs nothing today — every caller was
already paying a full process spawn, so nobody was concurrent.

**An entry is reserved before it is handed out.** The idle sweep and the cap
close only sessions nobody has claimed, and a caller's claim is registered
under the same lock the sweep runs under — otherwise a just-spawned session
looks idle and can be closed out from under the caller that asked for it.

**Starting a process holds no pool-wide lock.** One source's ~6 s startup
must not queue every other source's tool calls behind it, so a spawn is
serialized per key (via an in-flight marker) and the pool-wide guard covers
only the bookkeeping. Teardown likewise runs after the guard is released.

**A failed or abandoned call evicts, and never retries.** Retrying is the
caller's decision, because a retry of a mutating tool can run it twice. The
pool only guarantees that the *next* acquire does not inherit a broken
process. Cancellation counts as abandoned: a caller whose ``asyncio.wait_for``
fired leaves a request in flight upstream, so that session is evicted too
rather than handed to the next caller in an unverified state.

The reuse switch is ``mcp_session_pool`` in ``app.switches`` (env
``AGNES_MCP_SESSION_POOL``, config ``mcp.session_pool``); off means a process
per call. ``AGNES_MCP_SESSION_IDLE_S`` / ``AGNES_MCP_SESSION_POOL_MAX`` are
process-level tuning read at import — see ``docs/feature-flags.md`` for why
they are not registry switches.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator, Dict, List, Optional, Set

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger(__name__)


def _env_number(name: str, default: float) -> float:
    """Read a numeric knob, falling back on anything unparseable.

    Deliberately total: these are read at import, and a typo'd value that
    raised here would take the whole connector down — a strictly worse
    outcome than running on the default.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using %s", name, raw, default)
        return default


#: How long an unused session is kept before it is closed. Long enough to
#: cover one agent turn's worth of tool calls, short enough that an idle
#: instance is not holding subprocesses (and their memory) indefinitely.
IDLE_TIMEOUT_S = _env_number("AGNES_MCP_SESSION_IDLE_S", 180.0)

#: Upper bound on live pooled sessions *per event loop*. Each one is a Python
#: process with the upstream's whole import tree resident, so this is a memory
#: bound as much as a tidiness one. Beyond it, the least recently used session
#: nobody is holding is closed.
MAX_SESSIONS = int(_env_number("AGNES_MCP_SESSION_POOL_MAX", 8.0))

#: How long teardown waits for a keeper task to unwind before giving up on it.
CLOSE_TIMEOUT_S = 10.0


def pool_enabled() -> bool:
    """Whether stdio sessions are reused (registry switch ``mcp_session_pool``)."""
    try:
        from app.switches import switch_value

        return bool(switch_value("mcp_session_pool"))
    except Exception:  # noqa: BLE001 — see below
        # The connector must stay usable without the app package around
        # (headless scripts, connector-only fixtures) and reading a switch
        # must never be the reason a tool call fails. Same token set as
        # `app.instance_config.coerce_flag_value`, which is the rule the
        # registry applies on the path above.
        raw = os.environ.get("AGNES_MCP_SESSION_POOL")
        if raw is None:
            return True
        return raw.strip().lower() not in ("0", "false", "no", "off", "")


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
    #: Callers that have claimed this entry. Incremented under the pool guard
    #: BEFORE `lock` is taken, so neither the idle sweep nor the cap can close
    #: a session out from under a caller that is still on its way to it.
    reserved: int = 0


@dataclass
class _LoopState:
    """One loop's worth of pool. Everything in here is bound to `loop`."""

    loop: asyncio.AbstractEventLoop
    guard: asyncio.Lock
    entries: Dict[str, _Entry] = field(default_factory=dict)
    #: key -> future completed when the in-flight spawn for that key finishes
    #: (successfully or not). The per-key single-flight: it keeps two callers
    #: for one spec from starting two processes, without a pool-wide lock held
    #: across the ~6 s startup.
    spawning: Dict[str, asyncio.Future] = field(default_factory=dict)


class _Pool:
    def __init__(self) -> None:
        self._states: Dict[int, _LoopState] = {}
        # A plain threading lock: several event loops (a test's asyncio.run,
        # a thread running its own loop) can reach this map concurrently, and
        # the map itself is loop-agnostic.
        self._states_guard = threading.Lock()
        self._reapers: Set[asyncio.Task] = set()

    # -- per-loop state -----------------------------------------------------

    def _state(self) -> _LoopState:
        """Bookkeeping for the RUNNING loop, dropping loops that are gone.

        An entry from a closed loop is not merely unusable — its keeper task
        was cancelled when the loop closed, which unwound `stdio_client` and
        killed the subprocess. There is nothing left to close, so those states
        are discarded rather than drained.
        """
        loop = asyncio.get_running_loop()
        with self._states_guard:
            for ident, state in list(self._states.items()):
                if state.loop.is_closed():
                    del self._states[ident]
            state = self._states.get(id(loop))
            if state is None or state.loop is not loop:
                # `id()` is reused after a loop is garbage-collected, so the
                # identity check is what makes the key trustworthy.
                state = _LoopState(loop=loop, guard=asyncio.Lock())
                self._states[id(loop)] = state
            return state

    @property
    def _entries(self) -> Dict[str, _Entry]:
        """The running loop's entries. Read by tests; requires a live loop."""
        return self._state().entries

    # -- lifecycle ----------------------------------------------------------

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
            # `suppress`, so a keeper that will not unwind cannot replace the
            # startup error the caller needs to see with a bare timeout.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(closed), timeout=CLOSE_TIMEOUT_S)
            task.cancel()
            raise
        return _Entry(session=session, close=close, closed=closed)

    @staticmethod
    def _in_use(entry: _Entry) -> bool:
        return entry.reserved > 0 or entry.lock.locked()

    @staticmethod
    def _detach(state: _LoopState, key: str, entry: _Entry) -> bool:
        """Remove ``entry`` from the pool if it is still the one under ``key``.

        Synchronous on purpose: a dict swap cannot be interleaved by another
        task, so it needs no guard — and taking one here would mean awaiting
        on the cancellation path, where the caller has already been cancelled.
        """
        if state.entries.get(key) is entry:
            del state.entries[key]
            return True
        return False

    async def _shutdown(self, entry: _Entry) -> None:
        """Stop a DETACHED entry. Never called with the pool guard held: the
        wait is up to ``CLOSE_TIMEOUT_S``, and a teardown that slow must not
        block every other source's tool calls."""
        entry.close.set()
        try:
            await asyncio.wait_for(asyncio.shield(entry.closed), timeout=CLOSE_TIMEOUT_S)
        except Exception:  # noqa: BLE001 — teardown is best-effort
            logger.debug("pooled MCP session did not close cleanly", exc_info=True)

    async def _shutdown_all(self, entries: List[_Entry]) -> None:
        if entries:
            await asyncio.gather(*(self._shutdown(e) for e in entries))

    def _reap(self, entry: _Entry) -> None:
        """Close a detached entry in the background.

        Used on the cancellation path only: the caller's task is already
        cancelled, so awaiting teardown inline would either re-raise or stall
        the unwind. `close.set()` has already been called by then, so the
        keeper is on its way out regardless of when this task gets to run.
        """
        task = asyncio.ensure_future(self._shutdown(entry))
        self._reapers.add(task)
        task.add_done_callback(self._reapers.discard)

    # -- sweeps (called under the guard; they DETACH, they never close) ------

    def _collect_stale(self, state: _LoopState) -> List[_Entry]:
        now = time.monotonic()
        victims: List[_Entry] = []
        for key, entry in list(state.entries.items()):
            if self._in_use(entry):
                continue
            if now - entry.last_used > IDLE_TIMEOUT_S:
                del state.entries[key]
                victims.append(entry)
        return victims

    def _collect_over_cap(self, state: _LoopState) -> List[_Entry]:
        victims: List[_Entry] = []
        while len(state.entries) > MAX_SESSIONS:
            idle = [(k, e) for k, e in state.entries.items() if not self._in_use(e)]
            if not idle:
                # Everything live is claimed. Going over the cap for as long
                # as that lasts beats handing a caller a dead session.
                break
            idle.sort(key=lambda kv: kv[1].last_used)
            key, entry = idle[0]
            del state.entries[key]
            victims.append(entry)
        return victims

    # -- acquire ------------------------------------------------------------

    def _reserve(self, entry: _Entry) -> None:
        entry.reserved += 1
        entry.last_used = time.monotonic()

    async def _checkout(self, state: _LoopState, key: str, params: StdioServerParameters) -> _Entry:
        """Return a reserved entry for ``key``, starting one if needed."""
        while True:
            async with state.guard:
                victims = self._collect_stale(state)
                entry = state.entries.get(key)
                if entry is not None:
                    self._reserve(entry)
                    pending: Optional[asyncio.Future] = None
                    mine = False
                else:
                    pending = state.spawning.get(key)
                    mine = pending is None
                    if mine:
                        pending = state.loop.create_future()
                        state.spawning[key] = pending
            await self._shutdown_all(victims)
            if entry is not None:
                return entry
            if not mine:
                # Somebody is already starting this exact process. Wait for
                # them (shielded, so our own cancellation doesn't cancel their
                # marker) and re-check rather than starting a second one.
                assert pending is not None
                await asyncio.shield(pending)
                continue
            try:
                entry = await self._spawn(params)
            finally:
                # Resolved either way: on failure the waiters simply take
                # another turn, and one of them tries the spawn itself.
                # Deliberately awaits nothing — this runs on the cancellation
                # path too, and a marker left behind there would wedge every
                # later caller for this spec on a spawn that never finishes.
                if state.spawning.get(key) is pending:
                    del state.spawning[key]
                assert pending is not None
                if not pending.done():
                    pending.set_result(None)
            entry.reserved = 1
            async with state.guard:
                state.entries[key] = entry
                over_cap = self._collect_over_cap(state)
            await self._shutdown_all(over_cap)
            return entry

    @asynccontextmanager
    async def acquire(self, params: StdioServerParameters) -> AsyncIterator[ClientSession]:
        key = spec_key(params)
        state = self._state()
        while True:
            entry = await self._checkout(state, key, params)
            try:
                async with entry.lock:
                    if state.entries.get(key) is not entry:
                        # The caller ahead of us in the queue hit a transport
                        # error and evicted this session while we waited for
                        # its lock. Its process is gone, so start over rather
                        # than run this call on it.
                        continue
                    entry.last_used = time.monotonic()
                    try:
                        yield entry.session
                    except BaseException as exc:
                        # Do NOT retry here: re-running a mutating tool because
                        # its transport hiccuped is worse than the error. Just
                        # make sure the next caller does not inherit a broken
                        # process — and `BaseException`, not `Exception`,
                        # because a cancelled (timed-out) call leaves a request
                        # in flight upstream, which is exactly the state nobody
                        # should inherit.
                        if self._detach(state, key, entry):
                            entry.close.set()
                            if isinstance(exc, asyncio.CancelledError):
                                self._reap(entry)
                            else:
                                await self._shutdown(entry)
                        raise
                    finally:
                        entry.last_used = time.monotonic()
            finally:
                entry.reserved -= 1
            return

    async def aclose(self) -> None:
        """Close every session this loop owns."""
        state = self._state()
        async with state.guard:
            victims = list(state.entries.values())
            state.entries.clear()
        await self._shutdown_all(victims)
        with self._states_guard:
            if self._states.get(id(state.loop)) is state and not state.entries and not state.spawning:
                del self._states[id(state.loop)]


_pool = _Pool()


def get_pool() -> _Pool:
    return _pool


async def close_all() -> None:
    """Close every pooled session (process shutdown, and test teardown)."""
    await _pool.aclose()
