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
out on its own. There is no invalidation hook to forget to call. For a
``scope='per_user'`` source the caller's user id is folded in as well
(``acquire``'s ``key_salt``, supplied by ``client._open_session``): resolved
credentials alone are not proof of identity — two users with no stored
secret, or an identical shared value, would hash to one key and share a
process that can retain per-session state — so one analyst never gets
another's warm process by construction, not by the accident of differing
tokens.

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
already paying a full process spawn, so nobody was concurrent. Serialization
must not mean an unbounded queue: a pooled ``call_tool`` carries a default
ceiling (``CALL_TIMEOUT_S`` — on expiry the call errors and the eviction
path closes the process), and a caller waiting for the session gives up with
a clear error once the call ahead of it must itself have timed out. One
wedged upstream call is one failed call, not a permanent per-source outage.

**An entry is reserved before it is handed out.** The idle sweep and the cap
close only sessions nobody has claimed, and a caller's claim is registered
under the same lock the sweep runs under — otherwise a just-spawned session
looks idle and can be closed out from under the caller that asked for it.

**Starting a process holds no pool-wide lock.** One source's ~6 s startup
must not queue every other source's tool calls behind it, so a spawn is
serialized per key (via an in-flight marker) and the pool-wide guard covers
only the bookkeeping. Teardown likewise runs after the guard is released.
Startup is bounded (``SPAWN_TIMEOUT_S``): an upstream that launches but
never answers ``initialize`` would otherwise park its spawner — and every
same-spec caller queued on the single-flight marker — forever. On expiry the
half-started process is cancelled, the marker is released, and the next
caller spawns fresh.

**A failed or abandoned call evicts, and never retries.** Retrying is the
caller's decision, because a retry of a mutating tool can run it twice. The
pool only guarantees that the *next* acquire does not inherit a broken
process. Cancellation counts as abandoned: a caller whose ``asyncio.wait_for``
fired leaves a request in flight upstream, so that session is evicted too
rather than handed to the next caller in an unverified state. Abandonment
inside the pool's own machinery is covered the same way: a caller cancelled
during startup takes the starting process down with it, and one cancelled
during checkout releases its reservation and its spawn marker instead of
pinning them forever.

**Idle sessions age out on a timer, not only on the next call.** A sweeper
task per loop (started when the first entry lands, gone when the pool
empties) runs the same guarded stale-collection the next acquire would — so
a quiet instance actually releases its warm subprocesses after
``IDLE_TIMEOUT_S`` instead of holding them until the next tool call happens
to arrive. A session whose upstream exited on its own is detached by the
same sweep (``closed`` resolved counts as stale) rather than handed to the
next caller.

The reuse switch is ``mcp_session_pool`` in ``app.switches`` (env
``AGNES_MCP_SESSION_POOL``, config ``mcp.session_pool``); off means a process
per call. ``AGNES_MCP_SESSION_IDLE_S`` / ``AGNES_MCP_SESSION_POOL_MAX`` /
``AGNES_MCP_SESSION_SPAWN_TIMEOUT_S`` / ``AGNES_MCP_SESSION_CALL_TIMEOUT_S``
are process-level tuning read at import — see ``docs/feature-flags.md`` for
why they are not registry switches.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import os
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Set, cast

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger(__name__)


def _env_number(name: str, default: float) -> float:
    """Read a numeric knob, falling back on anything unparseable.

    Deliberately total: these are read at import, and a typo'd value that
    raised here would take the whole connector down — a strictly worse
    outcome than running on the default. That is why non-finite values are
    rejected too: ``float("nan")`` and ``float("inf")`` parse, and then
    ``int()`` at the ``MAX_SESSIONS`` assignment below raises (``ValueError``
    / ``OverflowError``) at import — and a ``nan`` idle timeout makes every
    expiry comparison false, so sessions never age out.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using %s", name, raw, default)
        return default
    if not math.isfinite(value):
        logger.warning("%s=%r is not finite; using %s", name, raw, default)
        return default
    return value


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

#: How long a starting upstream may take to become ready — the subprocess
#: spawn plus its ``initialize`` handshake. Unbounded, a server that starts
#: but never answers ``initialize`` parks its spawner forever, and with it
#: every same-spec caller queued on the single-flight marker. ``0`` disables.
SPAWN_TIMEOUT_S = _env_number("AGNES_MCP_SESSION_SPAWN_TIMEOUT_S", 60.0)

#: Default ceiling on one pooled tool call. Calls on a warm session are
#: serialized, so a call that never returns would hold the session's lock —
#: and with it the whole source, for every caller — forever. Generous,
#: because a legitimately slow tool beats a spurious timeout; on expiry the
#: call errors and the session is evicted so the next call starts fresh.
#: ``0`` disables this bound and the lock-wait bound derived from it.
CALL_TIMEOUT_S = _env_number("AGNES_MCP_SESSION_CALL_TIMEOUT_S", 300.0)


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


def spec_key(params: StdioServerParameters, *, salt: str = "") -> str:
    """Fingerprint of everything that decides what process this would be.

    The resolved secret is part of it *by design* — see the module docstring.
    Hashed rather than stored, so the pool's bookkeeping never holds a
    credential in plain form, and never logs one.

    ``salt`` folds in an identity the launch spec alone cannot carry: a
    ``scope='per_user'`` source passes the calling user's id, so two users
    never share one warm process even when their resolved credentials
    coincide (no stored secret, or an identical value).
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
            "salt": str(salt),
        },
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class _PooledSession:
    """What ``acquire`` hands out: the warm session, with a default bound on
    ``call_tool``.

    Calls on one session are serialized (module docstring), so a call that
    never returns would hold the serialization lock — and with it the whole
    source — forever. The bound turns that into a typed error; the eviction
    path in ``acquire`` then closes the process, so the request left in
    flight is never inherited by the next caller. A caller that passes its
    own ``read_timeout_seconds`` keeps it (the SDK enforces it upstream).
    Everything else proxies through to the underlying session untouched.
    """

    __slots__ = ("_session",)

    def __init__(self, session: ClientSession) -> None:
        self._session = session

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    async def call_tool(
        self,
        name: str,
        arguments: Optional[Dict[str, Any]] = None,
        read_timeout_seconds: Any = None,
        **kwargs: Any,
    ) -> Any:
        if read_timeout_seconds is not None:
            return await self._session.call_tool(name, arguments, read_timeout_seconds=read_timeout_seconds, **kwargs)
        timeout = CALL_TIMEOUT_S
        if timeout <= 0:
            return await self._session.call_tool(name, arguments, **kwargs)
        try:
            return await asyncio.wait_for(self._session.call_tool(name, arguments, **kwargs), timeout=timeout)
        except TimeoutError:
            raise TimeoutError(
                f"MCP tool call {name!r} did not return within {timeout:g}s "
                "(AGNES_MCP_SESSION_CALL_TIMEOUT_S); the warm session is closed, "
                "so the next call starts on a fresh process"
            ) from None


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
    #: across the ~6 s startup. Released only once the entry is REGISTERED
    #: (or the spawn is abandoned), so "no marker and no entry" always means
    #: "no subprocess" — the invariant ``aclose`` shuts down against.
    spawning: Dict[str, asyncio.Future] = field(default_factory=dict)
    #: The idle sweeper for this loop; ``None`` when the pool is empty.
    sweeper: Optional[asyncio.Task] = None


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
            if SPAWN_TIMEOUT_S > 0:
                session = await asyncio.wait_for(ready, timeout=SPAWN_TIMEOUT_S)
            else:
                session = await ready
        except asyncio.CancelledError:
            # The caller was abandoned while the server was still starting —
            # `except Exception` would miss this (CancelledError is a
            # BaseException), leaving the keeper parked with a live subprocess
            # nothing could ever reach: the entry was never registered, so no
            # sweep and no `close_all` finds it. We ARE the cancelled task, so
            # the unwind cannot be awaited here; cancelling the keeper is what
            # unwinds the transport context managers and kills the subprocess
            # (it also covers an upstream HUNG in startup, which `close` alone
            # would not). Tracked like a reaper so it is not a stray task.
            close.set()
            task.cancel()
            self._reapers.add(task)
            task.add_done_callback(self._reapers.discard)
            raise
        except TimeoutError:
            # The upstream started but never became ready (a wedged
            # `initialize`). The keeper is parked inside the handshake, so
            # only cancelling it unwinds the transport and kills the
            # subprocess — same reasoning as the cancellation branch above,
            # and tracked the same way. The caller gets a typed error;
            # `_checkout` releases the spawn marker on it, so the callers
            # queued behind this spawn retry fresh instead of inheriting a
            # process that never answered.
            close.set()
            task.cancel()
            self._reapers.add(task)
            task.add_done_callback(self._reapers.discard)
            raise TimeoutError(
                f"MCP server did not finish initializing within {SPAWN_TIMEOUT_S:g}s "
                "(AGNES_MCP_SESSION_SPAWN_TIMEOUT_S); its process was cancelled and "
                "the next call will spawn fresh"
            ) from None
        except BaseException:
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

        Used on the cancellation paths only: the caller's task is already
        cancelled, so awaiting teardown inline would either re-raise or stall
        the unwind. `_shutdown` sets `close` itself, so an entry handed here
        is on its way out regardless of when this task gets to run.
        """
        task = asyncio.ensure_future(self._shutdown(entry))
        self._reapers.add(task)
        task.add_done_callback(self._reapers.discard)

    # -- the idle sweeper -----------------------------------------------------

    def _ensure_sweeper(self, state: _LoopState) -> None:
        """Keep one sweeper task per non-empty loop-state. Called under the
        guard wherever an entry is registered or reused, so the invariant is
        simply: entries present ⇒ a sweeper is running."""
        if state.sweeper is None or state.sweeper.done():
            state.sweeper = asyncio.ensure_future(self._sweep_loop(state))

    async def _sweep_loop(self, state: _LoopState) -> None:
        """Age idle sessions out on a timer, not only on the next call.

        Without this, `IDLE_TIMEOUT_S` fired only from `_checkout` — so an
        instance whose last tool call had passed kept up to `MAX_SESSIONS`
        warm subprocesses alive until the next call or process exit, the
        opposite of what the switch text and the operator docs promise. The
        task exits when the pool empties (the next insert starts a new one)
        and is cancelled by `aclose` and by its loop closing.
        """
        try:
            while True:
                await asyncio.sleep(max(0.05, min(IDLE_TIMEOUT_S, 60.0)))
                async with state.guard:
                    victims = self._collect_stale(state)
                    done = not state.entries and not state.spawning
                    if done:
                        state.sweeper = None
                try:
                    await self._shutdown_all(victims)
                except BaseException:
                    for victim in victims:
                        self._reap(victim)
                    raise
                if done:
                    return
        finally:
            if state.sweeper is asyncio.current_task():
                state.sweeper = None

    # -- sweeps (called under the guard; they DETACH, they never close) ------

    def _collect_stale(self, state: _LoopState) -> List[_Entry]:
        now = time.monotonic()
        victims: List[_Entry] = []
        for key, entry in list(state.entries.items()):
            if self._in_use(entry):
                continue
            # An entry whose keeper already exited (`closed` resolved — the
            # upstream died or shut down on its own) is stale immediately:
            # handing it out would fail the next call on a dead transport.
            if entry.closed.done() or now - entry.last_used > IDLE_TIMEOUT_S:
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

    @staticmethod
    def _release_spawn_marker(state: _LoopState, key: str, pending: Optional[asyncio.Future]) -> None:
        """Resolve and drop this caller's in-flight spawn marker.

        Deliberately awaits nothing — it runs on the cancellation paths too,
        and a marker left behind there would wedge every later caller for
        this spec on a spawn that nobody is doing.
        """
        if pending is None:
            return
        if state.spawning.get(key) is pending:
            del state.spawning[key]
        if not pending.done():
            pending.set_result(None)

    async def _checkout(self, state: _LoopState, key: str, params: StdioServerParameters) -> _Entry:
        """Return a reserved entry for ``key``, starting one if needed.

        Every await between taking something under the guard (a reservation,
        the spawn marker, a fresh entry) and returning is wrapped so that
        cancellation — a caller's ``asyncio.wait_for`` firing while we pay for
        somebody else's teardown — gives the claim back instead of pinning it
        forever. Only ``acquire``'s finally releases a reservation otherwise,
        and it is unreachable until this method returns; a permanently
        reserved entry is permanently in-use, which no sweep can ever close,
        and once enough pile up the cap stops bounding the pool at all.
        """
        while True:
            async with state.guard:
                victims = self._collect_stale(state)
                entry = state.entries.get(key)
                if entry is not None:
                    self._reserve(entry)
                    self._ensure_sweeper(state)
                    pending: Optional[asyncio.Future] = None
                    mine = False
                else:
                    pending = state.spawning.get(key)
                    mine = pending is None
                    if mine:
                        pending = state.loop.create_future()
                        state.spawning[key] = pending
            try:
                await self._shutdown_all(victims)
            except BaseException:
                if entry is not None:
                    entry.reserved -= 1
                if mine:
                    self._release_spawn_marker(state, key, pending)
                for victim in victims:
                    # The gather may have been cancelled before a victim's
                    # shutdown set `close`; reap so each still goes down.
                    self._reap(victim)
                raise
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
            except BaseException:
                # On failure the waiters simply take another turn, and one of
                # them tries the spawn itself.
                self._release_spawn_marker(state, key, pending)
                raise
            entry.reserved = 1
            try:
                async with state.guard:
                    state.entries[key] = entry
                    self._ensure_sweeper(state)
                    over_cap = self._collect_over_cap(state)
                    # Released only now, with the entry registered: a waiter
                    # woken by the marker always finds the entry (no window in
                    # which it would start a duplicate process), and `aclose`
                    # can never observe "no marker, no entry" while the
                    # subprocess lives.
                    self._release_spawn_marker(state, key, pending)
            except BaseException:
                # Cancelled before the entry was published: nothing else can
                # ever reach it, so close it ourselves or it outlives every
                # sweep.
                self._release_spawn_marker(state, key, pending)
                entry.close.set()
                self._reap(entry)
                raise
            try:
                await self._shutdown_all(over_cap)
            except BaseException:
                entry.reserved -= 1
                for victim in over_cap:
                    self._reap(victim)
                raise
            return entry

    @staticmethod
    async def _take_entry_lock(entry: _Entry) -> None:
        """Take the entry's serialization lock, bounded when it is held.

        Unbounded, one wedged call held every later caller of the source in
        a silent queue forever. An uncontended lock is taken on the fast
        path — no suspension, which also preserves the no-await window
        between checkout and the staleness re-check that ``aclose``'s final
        sweep relies on. A held one is waited for with a budget: what the
        call ahead can possibly spend before the lock must come free — its
        own call ceiling plus the teardown the eviction path pays under the
        lock — so a healthy slow call is never preempted, and behind a
        wedged one the wait turns into a clear error. ``CALL_TIMEOUT_S <= 0``
        (the operator opting out of call bounds) leaves this wait unbounded
        too.
        """
        if CALL_TIMEOUT_S <= 0 or not entry.lock.locked():
            await entry.lock.acquire()
            return
        budget = CALL_TIMEOUT_S + 2 * CLOSE_TIMEOUT_S
        try:
            await asyncio.wait_for(entry.lock.acquire(), timeout=budget)
        except TimeoutError:
            raise TimeoutError(
                f"pooled MCP session is busy: the call ahead of this one has held it "
                f"for over {budget:g}s (AGNES_MCP_SESSION_CALL_TIMEOUT_S); giving up "
                "instead of queueing forever"
            ) from None

    @asynccontextmanager
    async def acquire(self, params: StdioServerParameters, *, key_salt: str = "") -> AsyncIterator[ClientSession]:
        """Reserve the warm session for ``params`` (spawning it if needed).

        ``key_salt`` — see :func:`spec_key`: per-user sources pass the
        caller's identity so two users never share one process.
        """
        key = spec_key(params, salt=key_salt)
        state = self._state()
        while True:
            entry = await self._checkout(state, key, params)
            try:
                await self._take_entry_lock(entry)
                try:
                    if state.entries.get(key) is not entry or entry.closed.done():
                        # The caller ahead of us in the queue hit a transport
                        # error and evicted this session while we waited for
                        # its lock — or the upstream died on its own in that
                        # window (`closed` resolved). Either way its process
                        # is gone, so start over rather than run this call on
                        # it; the next checkout's sweep detaches the corpse.
                        continue
                    entry.last_used = time.monotonic()
                    try:
                        yield cast(ClientSession, _PooledSession(entry.session))
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
                    entry.lock.release()
            finally:
                entry.reserved -= 1
            return

    async def aclose(self) -> None:
        """Close every session this loop owns, waiting out in-flight spawns.

        A spawn racing shutdown would otherwise re-register its entry after
        the sweep and the subprocess would outlive the "graceful" stop. The
        marker is only released once the entry is registered (see
        `_LoopState.spawning`), so sweeping again after each round of markers
        is guaranteed to see whatever those spawns produced.
        """
        state = self._state()
        while True:
            async with state.guard:
                victims = list(state.entries.values())
                state.entries.clear()
                spawning = [f for f in state.spawning.values() if not f.done()]
                if not spawning and state.sweeper is not None:
                    state.sweeper.cancel()
                    state.sweeper = None
            await self._shutdown_all(victims)
            if not spawning:
                break
            for marker in spawning:
                with contextlib.suppress(Exception):
                    await asyncio.shield(marker)
        reapers = [t for t in self._reapers if not t.done() and t.get_loop() is state.loop]
        if reapers:
            await asyncio.gather(*reapers, return_exceptions=True)
        with self._states_guard:
            if self._states.get(id(state.loop)) is state and not state.entries and not state.spawning:
                del self._states[id(state.loop)]


_pool = _Pool()


def get_pool() -> _Pool:
    return _pool


async def close_all() -> None:
    """Close every pooled session (process shutdown, and test teardown)."""
    await _pool.aclose()
