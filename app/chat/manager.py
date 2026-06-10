"""ChatManager: session state machine, lifecycle, WS attachment."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from app.chat.audit import hash_args, write_audit
from app.chat.config import ChatConfig
from app.chat.persistence import ChatRepository
from app.chat.provider import SandboxHandle, SandboxProvider
from app.chat.types import ChatSession, SessionState, Surface
from app.chat.workdir import WorkdirManager

logger = logging.getLogger(__name__)

# Sonnet pricing constants (USD per million tokens)
_PRICE_IN_PER_MTOK = 3.0
_PRICE_OUT_PER_MTOK = 15.0

# TTL for the per-user daily-token-sum cache inside ChatManager.
# The budget check runs on every send_user_message; hitting the DB on
# every keystroke is wasteful — a 60-second stale window is acceptable
# given the daily-cap semantics.
_DAILY_CACHE_TTL_SEC = 60


class ConcurrencyCapHit(Exception):
    """Raised when a user already has the maximum allowed active sessions."""


class SessionNotFound(Exception):
    pass


@dataclass
class SinkEntry:
    """One output target for a live session's frames. Duck-typed sink:
    a web WebSocket or a SlackSinkBridge — both expose ``send_json`` and
    ``close``. ``participant_email`` attributes the sink to a principal so
    leave/teardown can drop exactly one sink (used by co-drive in 5b)."""

    participant_email: str
    sink: object


@dataclass
class LiveSession:
    chat_id: str
    user_email: str
    state: SessionState
    handle: Optional[SandboxHandle]
    started_at: datetime
    last_activity: datetime
    crash_count: int = 0
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    tasks: list[asyncio.Task] = field(default_factory=list)
    # Latest pump-subprocess-to-ws task. Each crash respawn replaces this
    # (and removes the previous one from `tasks`) so the per-session task
    # list does not grow unboundedly across crashes.
    current_pump: Optional[asyncio.Task] = None
    # Latest crash-respawn wait task (_wait_for_exit_and_respawn). A co-session
    # leave (_respawn_co_runner) cancels this BEFORE killing the old handle and
    # starts a fresh one bound to the new session_dir — otherwise the running
    # wait task would observe the intentional kill as a crash and respawn a
    # second time (double-respawn race).
    current_wait: Optional[asyncio.Task] = None
    # Set to True once an auto-title task has been scheduled for this
    # session — guarantees we only fire Haiku once per live session
    # even if the user sends a second turn while the first one is
    # still in-flight.
    auto_title_started: bool = False
    # Output sinks the runner's frames fan out to. One SinkEntry per
    # attached principal (web WS or SlackSinkBridge). The primary sink is
    # seated by attach(); add_sink() appends late joiners (co-drive, 5b).
    sinks: list["SinkEntry"] = field(default_factory=list)
    # Frames of the in-progress turn (token/tool_call/...), replayed to
    # late-seated sinks and persisted as an interrupted message on forced
    # death. Cleared when the turn's assistant_message lands.
    turn_buffer: list[dict] = field(default_factory=list)
    turn_in_flight: bool = False
    # Linger task: fires _linger_then_pause after the last sink detaches.
    linger_task: Optional[asyncio.Task] = None
    # Session workdir; set at spawn/resume so helpers can access it.
    session_dir: Optional[Path] = None
    # Active-time accounting for max_session_seconds (Task 9).
    # active_since: monotonic timestamp when this spawn/resume made the session
    # ACTIVE. Pause folds (now - active_since) into active_seconds_accum and
    # resets active_since. Resume/spawn resets active_since to now.
    active_seconds_accum: float = 0.0
    active_since: float = field(default_factory=time.monotonic)
    # Serializes the stdin write+drain pair so two participants' concurrent
    # turns can never interleave partial JSON lines on the shared stdin
    # (spec §6.2).
    _stdin_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Live participant emails for co-sessions. Populated by attach() from
    # chat_session_participants WHERE left_at IS NULL; updated by leave_session()
    # when a participant leaves. Empty for non-co sessions.
    participant_emails: list[str] = field(default_factory=list)


class ChatManager:
    def __init__(
        self,
        *,
        provider: SandboxProvider,
        workdir_mgr: WorkdirManager,
        repo: ChatRepository,
        config: ChatConfig,
    ) -> None:
        self._provider = provider
        self._workdir_mgr = workdir_mgr
        self._repo = repo
        self._config = config
        self._live: dict[str, LiveSession] = {}
        self._idle_task: Optional[asyncio.Task] = None
        # Per-user sliding-window message timestamps for the rate-limit knob.
        # Each entry is a deque of monotonic timestamps in the last hour.
        # Trimmed on each send; entries older than 3600 s evicted.
        from collections import deque

        self._user_msg_window: dict[str, "deque[float]"] = {}
        self._deque_cls = deque
        # TTL cache: user_email → (monotonic_timestamp, (tokens_in, tokens_out))
        self._daily_tokens_cache: dict[str, tuple[float, tuple[int, int]]] = {}

    def _cached_daily_tokens(self, user_email: str) -> tuple[int, int]:
        """Return (tokens_in, tokens_out) for today, with a 60-second TTL cache.

        Avoids hitting the DB on every send_user_message call.
        """
        now = time.monotonic()
        cached = self._daily_tokens_cache.get(user_email)
        if cached and now - cached[0] < _DAILY_CACHE_TTL_SEC:
            return cached[1]
        val = self._repo.daily_anthropic_tokens(user_email)
        self._daily_tokens_cache[user_email] = (now, val)
        return val

    # --- public API used by app/api/chat.py and services/slack_bot/ -------

    async def create_session(
        self,
        *,
        user_email: str,
        surface: Surface,
        slack_channel_id: Optional[str] = None,
        slack_thread_ts: Optional[str] = None,
        title: Optional[str] = None,
    ) -> ChatSession:
        if not self._config.enabled:
            raise RuntimeError("chat.enabled is false")
        active = self._active_count_for_user(user_email)
        if active >= self._config.concurrency_per_user:
            raise ConcurrencyCapHit(
                f"user {user_email} has {active} active sessions; cap = {self._config.concurrency_per_user}"
            )
        # De-dupe Slack DM / thread to existing live session.
        # intentional: no await between SELECT and INSERT — Slack uniqueness without DB partial unique index
        if surface == Surface.SLACK_DM and slack_channel_id:
            existing = self._repo.get_slack_dm_session(slack_channel_id)
            if existing is not None:
                return existing
        if surface == Surface.SLACK_THREAD and slack_channel_id and slack_thread_ts:
            existing = self._repo.get_slack_thread_session(slack_channel_id, slack_thread_ts)
            if existing is not None:
                return existing
        created = self._repo.create_session(
            user_email=user_email,
            surface=surface,
            slack_channel_id=slack_channel_id,
            slack_thread_ts=slack_thread_ts,
            title=title,
        )
        # Garbage-collect orphan empty sessions for this user on every
        # web-surface create. Clicking "+ New chat" repeatedly was
        # accumulating ten-plus 'Untitled chat' rows in the sidebar
        # because each click POSTs a new session but the previous
        # empty one was never archived. Soft-archive prior empties
        # only — never touch sessions with real messages. Slack
        # surfaces de-dupe upstream, so only run this on WEB.
        if surface == Surface.WEB:
            try:
                self._repo.archive_empty_user_sessions(
                    user_email,
                    surface=Surface.WEB,
                    exclude_id=created.id,
                )
            except Exception:
                logger.exception(
                    "archive_empty_user_sessions failed for %s; not fatal",
                    user_email,
                )
        return created

    def _active_count_for_user(self, user_email: str) -> int:
        n = 0
        for s in self._live.values():
            if s.state not in (SessionState.NEW, SessionState.ACTIVE, SessionState.IDLE):
                continue
            # Count the session against both the owner and every live participant
            # in co-sessions, so the concurrency cap applies to all co-drivers.
            co_emails = getattr(s, "participant_emails", [])
            if s.user_email == user_email or user_email in co_emails:
                n += 1
        return n

    def active_count_for_user(self, user_email: str) -> int:
        """Public wrapper over the private cap predicate so callers
        (e.g. /agnes-status) report exactly what create_session enforces."""
        return self._active_count_for_user(user_email)

    def list_live(self) -> list[LiveSession]:
        return list(self._live.values())

    async def wait_until_live(self, chat_id: str, *, timeout: float = 30.0) -> bool:
        """Block until ``chat_id`` is registered with a live, usable handle.

        ``attach()`` does not return for the lifetime of a session — it awaits
        the session's pump/wait tasks (see the ``asyncio.gather`` at the end of
        ``attach``). So request-less callers (the Slack bot) cannot ``await``
        attach() to learn when the session is ready: they schedule it
        fire-and-forget and then await *this*. Spawning the sandbox inside
        attach() takes several seconds — far longer than the fixed 0.1s sleep
        the callers used to rely on — so without this the immediately-following
        ``send_user_message`` races attach() and raises ``SessionNotFound``,
        silently dropping the user's first message.

        Returns True once the live session exists with a non-dead handle, or
        False if ``timeout`` elapses first. Polls the in-process registry; no
        external I/O.
        """
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            live = self._live.get(chat_id)
            if live is not None and live.handle is not None and live.state != SessionState.DEAD:
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.1)

    async def shutdown(self) -> None:
        """Gracefully shut down all live sessions.

        When on_detach='pause', ACTIVE sessions are paused so they survive the
        restart (sandboxes preserve memory + running processes). When
        on_detach='kill' (or pause fails), the session is killed as before.
        """
        chat_ids = list(self._live.keys())
        for chat_id in chat_ids:
            live = self._live.get(chat_id)
            if live is None:
                continue
            if live.state == SessionState.ACTIVE and self._config.on_detach == "pause":
                try:
                    await self._pause_live(live)
                    continue
                except Exception:
                    logger.exception("shutdown pause failed for %s — killing instead", chat_id)
            try:
                await self.kill(chat_id, reason="server_shutdown")
            except Exception:
                logger.exception("error killing session %s on shutdown", chat_id)

    # --- attach + runtime methods (Task 5.2 / Task 8) -----------------------

    async def attach(self, chat_id: str, ws, *, is_primary: bool = True) -> None:
        """Ensure the session is running and seat ws as a sink.

        Decision tree (Task 8):
        1. Live ACTIVE  → cancel any linger task, seat sink.
        2. Live PAUSED  → resume provider, restart tasks, seat sink.
        3. No live entry but repo row has sandbox refs → _resume_from_row (post-restart).
        4. Otherwise    → _spawn_live (today's spawn body).

        attach() is now fast: it returns after seating the sink. The pump/wait
        tasks run independently — attach no longer awaits them. The caller is
        responsible for keeping ws reading until it wants to disconnect, then
        calling detach_sink().
        """
        live = self._live.get(chat_id)
        if live is not None and live.state == SessionState.ACTIVE:
            self._cancel_linger(live)
            await self._seat_sink(live, ws, is_primary=is_primary)
            return
        if live is not None and live.state == SessionState.PAUSED:
            await self._resume_live(live)
            await self._seat_sink(live, ws, is_primary=is_primary)
            return
        session = self._repo.get_session(chat_id)
        if session is None:
            raise SessionNotFound(chat_id)
        if session.sandbox_id is not None and session.runner_pid is not None:
            live = await self._resume_from_row(session)
            if live is not None:
                await self._seat_sink(live, ws, is_primary=is_primary)
                return
            # resume failed → refs cleared by _resume_from_row, fall through
            session = self._repo.get_session(chat_id)
        live = await self._spawn_live(session)
        await self._seat_sink(live, ws, is_primary=is_primary)

    async def _seat_sink(self, live: "LiveSession", ws, *, is_primary: bool) -> None:
        """Replay history + turn buffer to ws, append to sinks, send ready."""
        for msg in self._repo.list_messages(live.chat_id):
            await ws.send_json(
                {
                    "type": "assistant_message" if msg.role == "assistant" else "user_msg",
                    "content": msg.content,
                    "sender_email": msg.sender_email,
                }
            )
        for frame in list(live.turn_buffer):
            await ws.send_json(frame)
        if is_primary:
            live.sinks.insert(0, SinkEntry(participant_email=live.user_email, sink=ws))
        else:
            live.sinks.append(SinkEntry(participant_email=live.user_email, sink=ws))
        await ws.send_json({"type": "ready"})

    async def _spawn_live(self, session: "ChatSession") -> "LiveSession":
        """Spawn a fresh sandbox, register refs, start pump/wait tasks.

        Returns the new LiveSession registered in self._live. Does NOT await
        the pump/wait tasks — they run independently (Task 8 contract).
        """
        chat_id = session.id
        if session.is_co_session:
            parts = self._repo.get_session_participants(chat_id)
            emails = [p.user_email for p in parts if p.left_at is None]
            from src.grant_intersection import compute_grant_intersection

            inter = compute_grant_intersection(emails, self._repo._conn)
            session_dir = self._workdir_mgr.prepare_ephemeral_session_dir(chat_id, emails, inter)
        else:
            emails = []  # participant_emails is empty for single-user sessions
            self._workdir_mgr.ensure_user_workdir(session.user_email)
            session_dir = self._workdir_mgr.prepare_session_dir(session.user_email, chat_id)

        handle = await self._spawn_runner(session, session_dir)
        import time as _t

        live = LiveSession(
            chat_id=chat_id,
            user_email=session.user_email,
            state=SessionState.ACTIVE,
            handle=handle,
            started_at=datetime.now(timezone.utc),
            last_activity=datetime.now(timezone.utc),
            sinks=[],
            participant_emails=emails,
            session_dir=session_dir,
            active_since=_t.monotonic(),
        )
        self._live[chat_id] = live
        self._repo.set_sandbox_ref(chat_id, sandbox_id=handle.sandbox_id, runner_pid=handle.pid)
        pump_task = asyncio.create_task(self._pump_subprocess_to_ws(live))
        wait_task = asyncio.create_task(self._wait_for_exit_and_respawn(live, session_dir))
        live.tasks = [pump_task, wait_task]
        live.current_pump = pump_task
        live.current_wait = wait_task
        return live

    # --- detach / linger / pause --------------------------------------------

    async def detach_sink(self, chat_id: str, ws) -> None:
        """Remove ws from the session's sink list. When the last sink leaves,
        trigger the on_detach policy (linger→pause or kill)."""
        live = self._live.get(chat_id)
        if live is None:
            return
        live.sinks = [e for e in live.sinks if e.sink is not ws]
        if not live.sinks:
            self._on_all_sinks_gone(live)

    def _cancel_linger(self, live: "LiveSession") -> None:
        if live.linger_task is not None and not live.linger_task.done():
            live.linger_task.cancel()
        live.linger_task = None

    def _on_all_sinks_gone(self, live: "LiveSession") -> None:
        if self._config.on_detach == "kill":
            asyncio.create_task(self.kill(live.chat_id, reason="ws_disconnect"))
            return
        self._cancel_linger(live)
        live.linger_task = asyncio.create_task(self._linger_then_pause(live))

    async def _linger_then_pause(self, live: "LiveSession") -> None:
        # Wait for any in-flight turn to complete first.
        while live.turn_in_flight:
            await asyncio.sleep(0.05)
        await asyncio.sleep(self._config.detach_linger_seconds)
        if live.sinks or live.state != SessionState.ACTIVE:
            return  # a sink came back, or state already changed
        await self._pause_live(live)

    async def _pause_live(self, live: "LiveSession") -> None:
        """Snapshot the sandbox and mark the session PAUSED.

        Sets state=PAUSED FIRST so _wait_for_exit_and_respawn (which holds
        the crash-respawn loop) sees the state change and treats the
        subsequent EOF as intentional rather than a crash.
        Folds the current active segment into active_seconds_accum so the
        max_session_seconds cap counts only real active time.
        """
        # Fold active-time segment before changing state.
        if live.state == SessionState.ACTIVE:
            live.active_seconds_accum += time.monotonic() - live.active_since
        live.state = SessionState.PAUSED
        for t in live.tasks:
            t.cancel()
        live.tasks = []
        live.current_pump = None
        live.current_wait = None
        try:
            if live.handle is not None:
                await self._provider.pause(live.handle)
        except Exception:
            logger.exception("pause failed for %s — falling back to kill", live.chat_id)
            live.state = SessionState.ACTIVE  # let kill() handle teardown + partial-save
            await self.kill(live.chat_id, reason="pause_failed")
            return
        live.handle = None
        self._repo.set_sandbox_paused_at(live.chat_id, datetime.now(timezone.utc))

    async def _resume_live(self, live: "LiveSession") -> None:
        """Resume a PAUSED in-memory session by reconnecting the sandbox."""
        session = self._repo.get_session(live.chat_id)
        if session is None or session.sandbox_id is None:
            await self._respawn_fresh(live)
            return
        import time as _t

        try:
            handle = await self._provider.resume(
                sandbox_id=session.sandbox_id,
                runner_pid=session.runner_pid,
                env={},
            )
        except Exception:
            logger.warning("resume failed for %s — fresh spawn fallback", live.chat_id)
            self._repo.clear_sandbox_ref(live.chat_id)
            await self._respawn_fresh(live)
            return
        live.handle = handle
        live.state = SessionState.ACTIVE
        live.active_since = _t.monotonic()
        pump_task = asyncio.create_task(self._pump_subprocess_to_ws(live))
        wait_task = asyncio.create_task(self._wait_for_exit_and_respawn(live, live.session_dir or Path("/tmp")))
        live.tasks = [pump_task, wait_task]
        live.current_pump = pump_task
        live.current_wait = wait_task
        self._repo.set_sandbox_paused_at(live.chat_id, None)

    async def _resume_from_row(self, session: "ChatSession") -> Optional["LiveSession"]:
        """Post-restart resume: no LiveSession in memory, but repo row has refs.

        Returns a new LiveSession on success, None on failure (refs cleared).
        """
        import time as _t

        try:
            handle = await self._provider.resume(
                sandbox_id=session.sandbox_id,
                runner_pid=session.runner_pid,
                env={},
            )
        except Exception:
            logger.warning(
                "_resume_from_row failed for %s — clearing refs for fresh spawn",
                session.id,
            )
            self._repo.clear_sandbox_ref(session.id)
            return None
        session_dir = self._workdir_mgr.prepare_session_dir(session.user_email, session.id)
        live = LiveSession(
            chat_id=session.id,
            user_email=session.user_email,
            state=SessionState.ACTIVE,
            handle=handle,
            started_at=datetime.now(timezone.utc),
            last_activity=datetime.now(timezone.utc),
            sinks=[],
            session_dir=session_dir,
            active_since=_t.monotonic(),
        )
        self._live[session.id] = live
        self._repo.set_sandbox_paused_at(session.id, None)
        pump_task = asyncio.create_task(self._pump_subprocess_to_ws(live))
        wait_task = asyncio.create_task(self._wait_for_exit_and_respawn(live, session_dir))
        live.tasks = [pump_task, wait_task]
        live.current_pump = pump_task
        live.current_wait = wait_task
        return live

    async def _respawn_fresh(self, live: "LiveSession") -> None:
        """Spawn a new sandbox for an existing LiveSession and replay history.

        Factored from _wait_for_exit_and_respawn's crash-respawn block so
        resume-failure and reaper-kill fallbacks can reuse it.
        """
        session = self._repo.get_session(live.chat_id)
        if session is None:
            return
        import time as _t

        session_dir = live.session_dir or self._workdir_mgr.prepare_session_dir(session.user_email, live.chat_id)
        new_handle = await self._spawn_runner(session, session_dir)
        live.handle = new_handle
        live.state = SessionState.ACTIVE
        live.active_since = _t.monotonic()
        self._repo.set_sandbox_ref(live.chat_id, sandbox_id=new_handle.sandbox_id, runner_pid=new_handle.pid)
        await self._broadcast(live, {"type": "ready"})
        # Replay last 3 user turns.
        history = self._repo.list_messages(live.chat_id)[-3:]
        live_emails = set(live.participant_emails) or {live.user_email}
        for msg in history:
            if msg.role != "user":
                continue
            author = getattr(msg, "sender_email", None) or live.user_email
            if live.participant_emails and author not in live_emails:
                continue
            payload = json.dumps({"type": "user_msg", "text": msg.content}) + "\n"
            async with live._stdin_lock:
                new_handle.stdin.write(payload.encode("utf-8"))
                await new_handle.stdin.drain()
        old_pump = live.current_pump
        if old_pump is not None and not old_pump.done():
            old_pump.cancel()
            try:
                await old_pump
            except (asyncio.CancelledError, Exception):
                pass
        if old_pump is not None and old_pump in live.tasks:
            live.tasks.remove(old_pump)
        new_pump = asyncio.create_task(self._pump_subprocess_to_ws(live))
        live.current_pump = new_pump
        live.tasks.append(new_pump)
        new_wait = asyncio.create_task(self._wait_for_exit_and_respawn(live, session_dir))
        live.current_wait = new_wait
        live.tasks.append(new_wait)

    async def add_sink(self, chat_id: str, sink, participant_email: str) -> None:
        """Attach an additional output sink to an already-live session.

        SR-9: re-verifies that the participant is still a live (left_at IS NULL)
        member before appending; raises PermissionError otherwise so a
        post-leave join attempt is rejected at the door.

        Replays persisted history to the new sink BEFORE appending it to the
        broadcast list, so a late joiner never misses in-flight frames and
        never double-receives one (replay + append are serialized here; the
        pump only ever sees the sink once it's in live.sinks). Sends ``ready``
        last. Used by single-principal Slack cross-surface attach and (5b)
        co-drive join."""
        live = self._live.get(chat_id)
        if live is None or live.state == SessionState.DEAD:
            raise SessionNotFound(chat_id)
        # SR-9: for co-sessions, membership re-verify — only live participants
        # may join. Non-co-session add_sink (e.g. Slack cross-surface) bypasses
        # this check because participant rows don't exist for single-user sessions.
        if live.participant_emails:  # truthy only for co-sessions
            parts = self._repo.get_session_participants(chat_id)
            if not any(p.user_email == participant_email and p.left_at is None for p in parts):
                raise PermissionError(f"{participant_email} is not a live participant of {chat_id}")
        for msg in self._repo.list_messages(chat_id):
            await sink.send_json(
                {
                    "type": "assistant_message" if msg.role == "assistant" else "user_msg",
                    "content": msg.content,
                    "sender_email": msg.sender_email,
                }
            )
        # Replay the in-progress turn buffer so a mid-turn reconnect/join
        # picks up exactly the frames the runner has already emitted.
        # Snapshot first to avoid racing the pump task.
        for frame in list(live.turn_buffer):
            await sink.send_json(frame)
        live.sinks.append(SinkEntry(participant_email=participant_email, sink=sink))
        await sink.send_json({"type": "ready"})

    async def _spawn_runner(self, session: ChatSession, session_dir: Path):
        from app.auth.access import mint_session_jwt, mint_co_session_jwt

        if session.is_co_session:
            # SR-5: NO seed fallback for co-sessions. A mint failure re-raises
            # and aborts the spawn — never inject a seed token (which carries no
            # co claims and could resolve to admin via the normal user path).
            token = mint_co_session_jwt(session.id)
        else:
            try:
                token = mint_session_jwt(session.user_email, session.id)
            except ValueError:
                # User not found in DB (e.g. deleted mid-session) — fall back to
                # the env-seed so the runner at least starts; it will fail auth
                # on its first API call and surface a clear error to the user.
                logger.warning(
                    "_spawn_runner: mint_session_jwt failed for %s; using AGNES_SESSION_JWT_SEED fallback",
                    session.user_email,
                )
                token = os.environ.get("AGNES_SESSION_JWT_SEED", "")
        env = {
            "AGNES_TOKEN": token,
            # The agnes CLI inside the sandbox reads its server URL from
            # AGNES_SERVER (cli/config.py) — the previous AGNES_API had no
            # consumer, so `agnes catalog`/`query`/… silently fell back to
            # http://localhost:8000 and could never reach the server. The
            # sandbox is a remote microVM, so this MUST be a publicly
            # reachable URL: prefer SERVER_URL (the deployment's public URL,
            # same value WorkdirManager seeds into the workspace), falling
            # back to AGNES_INTERNAL_URL then loopback. Operators running
            # cloud chat must set SERVER_URL for the data rails to work.
            "AGNES_SERVER": (
                os.environ.get("SERVER_URL") or os.environ.get("AGNES_INTERNAL_URL") or "http://127.0.0.1:8000"
            ),
            "AGNES_SESSION_ID": session.id,
            "AGNES_USER_EMAIL": session.user_email,
            "AGNES_DAILY_BUDGET_USD": str(self._config.daily_anthropic_spend_usd),
            "AGNES_PER_TOOL_CALL_SECONDS": str(self._config.per_tool_call_seconds),
            "AGNES_TOOL_CALLS_PER_TURN": str(self._config.tool_calls_per_turn_budget),
            # Opt-in: bootstrap the user's marketplace plugins into the sandbox
            # at spawn and load them via setting_sources. Off by default (adds
            # per-spawn latency; only useful once the marketplace ships real
            # skill content). See ChatConfig.bootstrap_marketplace.
            "AGNES_BOOTSTRAP_MARKETPLACE": "1" if self._config.bootstrap_marketplace else "",
            # claude-agent-sdk inside the sandbox spawns the `claude` CLI
            # binary, which authenticates against Anthropic via this env.
            # Without it the MCP initialize handshake hangs and the runner
            # fires "Control request timeout: initialize" within ~60 s.
            # We forward the host-process key (already startup-gated via
            # _chat_anthropic_key_ok in app/main.py) — if it's empty here
            # the gate would have blocked startup, so this is just a
            # pass-through.
            "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            # ``session_dir`` is an Agnes-host-side path; it doesn't exist
            # inside the E2B sandbox. claude-agent-sdk's inner ``claude``
            # CLI needs a writable HOME for ``~/.claude/`` config — using
            # the host path here makes the CLI hang on first config write,
            # which surfaces as ``Control request timeout: initialize``.
            # ``/home/user`` is created by the e2b template's base image
            # and is writable by the in-sandbox ``user`` account.
            "HOME": "/home/user",
            "TERM": "dumb",
            "LANG": "C.UTF-8",
            "PYTHONUNBUFFERED": "1",
        }
        # Under E2B the in-sandbox runner is uploaded as a single file
        # (provider does ``files.write("/work/runner.py", ...)`` at spawn
        # time per the agnes-chat template tradeoff), so we invoke it
        # directly as a script. The legacy ``python -m app.chat.runner``
        # form relied on the host's installed package — there is no
        # ``app.chat.runner`` module inside the sandbox.
        argv = ["python3", "/work/runner.py", "--session-id", session.id]
        handle = await self._provider.spawn(workdir=session_dir, env=env, argv=argv)
        # The provider may declare ``syncs_workspace = True`` (workspace
        # is mounted, no sync needed). For E2B we hold the workspace
        # locally and push it after spawn — Q1's full-push strategy.
        if not getattr(self._provider, "syncs_workspace", False):
            from app.chat.e2b_workspace_sync import (
                WorkspaceTooLarge,
                upload_agnes_wheel,
                upload_workspace,
            )

            max_bytes = getattr(self._config, "e2b_workspace_max_bytes", 100 * 1024 * 1024)
            sandbox = getattr(handle, "_sandbox", None)
            if sandbox is not None:
                try:
                    await upload_workspace(sandbox, session_dir, max_bytes=max_bytes)
                except WorkspaceTooLarge as e:
                    logger.error("workspace upload refused: %s", e)
                    # Tear down the sandbox; surfacing the failure to the
                    # caller lets attach() emit a user-facing error
                    # frame.
                    try:
                        await handle.kill(grace_sec=1.0)
                    except Exception:
                        logger.exception("kill after upload-refusal failed")
                    raise
                # Ship the agnes CLI wheel so the runner can pip-install it at
                # boot — this is what makes `agnes catalog/query/...` resolve
                # inside the sandbox. Best-effort: a missing/oversized wheel
                # leaves the CLI absent but never blocks the session, so unlike
                # the workspace push it does not tear the sandbox down.
                try:
                    await upload_agnes_wheel(sandbox)
                except Exception:
                    logger.exception(
                        "agnes wheel upload failed; `agnes` CLI will be absent in sandbox for session %s",
                        session.id,
                    )
        return handle

    async def _pump_subprocess_to_ws(self, live: LiveSession) -> None:
        assert live.handle is not None
        while True:
            line = await live.handle.stdout.readline()
            if not line:
                return
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            live.last_activity = datetime.now(timezone.utc)
            await self._broadcast(live, frame)
            ftype = frame.get("type")
            # Accumulate in-flight turn frames for mid-turn replay and partial save.
            if ftype in ("token", "tool_call"):
                live.turn_buffer.append(frame)
            if ftype == "assistant_message":
                self._repo.append_message(
                    session_id=live.chat_id,
                    role="assistant",
                    content=frame.get("content", ""),
                    tool_calls=frame.get("tool_calls"),
                    tokens_in=frame.get("tokens_in"),
                    tokens_out=frame.get("tokens_out"),
                    model=frame.get("model"),
                )
                live.turn_buffer.clear()
                live.turn_in_flight = False
                # Auto-title: the first assistant_message in a session
                # is the trigger to ask Haiku for a short title. We
                # check the per-session flag (not just the persisted
                # title) so two rapid-fire assistant frames during
                # crash-respawn replay don't both fire the call.
                if not live.auto_title_started:
                    self._maybe_start_auto_title(live)
            elif ftype == "done":
                live.turn_buffer.clear()
                live.turn_in_flight = False
            if ftype == "tool_call":
                write_audit(
                    self._repo._conn,
                    user_email=live.user_email,
                    action="chat.tool_call",
                    details={
                        "session_id": live.chat_id,
                        "tool": frame.get("tool"),
                        "args_hash": hash_args(frame.get("args", {})),
                    },
                )

    async def _broadcast(self, live: LiveSession, frame: dict) -> None:
        """Send a frame to every sink, snapshotting the list first so a
        concurrent add/remove can't mutate it mid-iteration. Dead sinks are
        removed and closed after the loop. A failing sink never aborts the
        broadcast to the others."""
        dead: list[SinkEntry] = []
        for entry in list(live.sinks):
            try:
                await entry.sink.send_json(frame)
            except Exception:
                logger.warning("sink send failed for %s", live.chat_id)
                dead.append(entry)
        for entry in dead:
            if entry in live.sinks:
                live.sinks.remove(entry)
            asyncio.create_task(self._safe_close(entry.sink))

    @staticmethod
    async def _safe_close(sink) -> None:
        try:
            await sink.close()
        except Exception:
            pass

    async def _wait_for_exit_and_respawn(self, live: LiveSession, session_dir: Path) -> None:
        while True:
            assert live.handle is not None
            rc = await live.handle.wait()
            # Return for intentional terminations: clean exit, kill(), or pause.
            if rc == 0 or live.state in (SessionState.DEAD, SessionState.PAUSED):
                return
            # Crash path
            live.crash_count += 1
            await self._broadcast(
                live,
                {
                    "type": "error",
                    "kind": "subprocess_crashed",
                    "auto_respawn": live.crash_count < 3,
                },
            )
            if live.crash_count >= 3:
                live.state = SessionState.DEAD
                return
            session = self._repo.get_session(live.chat_id)
            if session is None:
                return
            new_handle = await self._spawn_runner(session, session_dir)
            live.handle = new_handle
            live.state = SessionState.ACTIVE
            await self._broadcast(live, {"type": "ready"})
            # Replay last 3 user turns into the new subprocess.
            # SR-11: for co-sessions, skip turns authored by a departed
            # participant and carry sender_email so the runner sees
            # who sent each message.
            history = self._repo.list_messages(live.chat_id)[-3:]
            live_emails = set(live.participant_emails) or {live.user_email}
            for msg in history:
                if msg.role != "user":
                    continue
                author = getattr(msg, "sender_email", None) or live.user_email
                # SR-11: do not replay a departed participant's turn
                if live.participant_emails and author not in live_emails:
                    continue
                payload = json.dumps({"type": "user_msg", "text": msg.content}) + "\n"
                async with live._stdin_lock:
                    new_handle.stdin.write(payload.encode("utf-8"))
                    await new_handle.stdin.drain()
            # Replace (not append) the per-session pump task so the task
            # list does not grow unboundedly across crash respawns.  The old
            # pump returned on EOF; cancel it for hygiene, then drop it from
            # `tasks` before spawning the new one.
            old_pump = live.current_pump
            if old_pump is not None and not old_pump.done():
                old_pump.cancel()
                try:
                    await old_pump
                except (asyncio.CancelledError, Exception):
                    pass
            if old_pump is not None and old_pump in live.tasks:
                live.tasks.remove(old_pump)
            new_pump = asyncio.create_task(self._pump_subprocess_to_ws(live))
            live.current_pump = new_pump
            live.tasks.append(new_pump)
            # Loop back to wait on the new handle.

    async def send_user_message(self, chat_id: str, text: str, *, sender_email: Optional[str] = None) -> None:
        live = self._live.get(chat_id)
        # Resume on-demand: PAUSED live session (Slack DM after hours, web race).
        if live is not None and live.state == SessionState.PAUSED:
            await self._resume_live(live)
        elif live is None:
            # Post-restart: no LiveSession in memory, but repo row may have sandbox refs.
            session = self._repo.get_session(chat_id)
            if session is not None and session.sandbox_id is not None:
                live = await self._resume_from_row(session)
                if live is None:
                    # _resume_from_row cleared refs; try a fresh spawn
                    session = self._repo.get_session(chat_id)
                    if session is not None:
                        live = await self._spawn_live(session)
            # After recovery attempt, re-fetch from _live
            live = self._live.get(chat_id)
        if live is None or live.handle is None or live.state == SessionState.DEAD:
            raise SessionNotFound(chat_id)
        # SR-10: key all per-user budget/rate checks on the actual SENDER,
        # not the session owner — each co-driver has their own daily/rate window.
        sender = sender_email or live.user_email
        # Enforce daily Anthropic spend cap (result is TTL-cached — see _cached_daily_tokens)
        tokens_in, tokens_out = self._cached_daily_tokens(sender)
        spent_usd = tokens_in * _PRICE_IN_PER_MTOK / 1_000_000 + tokens_out * _PRICE_OUT_PER_MTOK / 1_000_000
        if spent_usd >= self._config.daily_anthropic_spend_usd:
            await self._broadcast(
                live,
                {
                    "type": "error",
                    "kind": "daily_budget",
                    "message": (
                        f"Daily spend cap of ${self._config.daily_anthropic_spend_usd:.2f} reached. Try again tomorrow."
                    ),
                },
            )
            raise RuntimeError("daily_budget_exhausted")
        # Per-session token cap — operators set max_session_tokens in
        # instance.yaml; previously the knob was dead config. Tokens already
        # spent in this session are summed from chat_messages on every send;
        # the session row itself is never UPDATEd (DuckDB 1.5.3 FK+index bug
        # documented in persistence.py).
        session_tokens = self._repo.session_total_tokens(chat_id)
        if session_tokens >= self._config.max_session_tokens:
            await self._broadcast(
                live,
                {
                    "type": "error",
                    "kind": "max_session_tokens",
                    "message": (
                        f"Per-session token cap of {self._config.max_session_tokens} reached "
                        f"(used {session_tokens}). Start a new chat session."
                    ),
                },
            )
            raise RuntimeError("max_session_tokens_exhausted")
        # Per-user sliding-window message-rate cap keyed on the SENDER (SR-10).
        # Trim entries older than one hour, then check the count.
        import time as _time

        now_mono = _time.monotonic()
        window = self._user_msg_window.setdefault(sender, self._deque_cls())
        while window and (now_mono - window[0]) > 3600:
            window.popleft()
        if len(window) >= self._config.rate_messages_per_hour:
            await self._broadcast(
                live,
                {
                    "type": "error",
                    "kind": "rate_limit",
                    "message": (
                        f"Rate limit hit: {self._config.rate_messages_per_hour} messages/hour. "
                        "Slow down or wait an hour."
                    ),
                },
            )
            raise RuntimeError("rate_limit_exceeded")
        window.append(now_mono)
        self._repo.append_message(
            session_id=chat_id,
            role="user",
            content=text,
            sender_email=sender_email or live.user_email,
        )
        payload = json.dumps({"type": "user_msg", "text": text}) + "\n"
        async with live._stdin_lock:
            live.handle.stdin.write(payload.encode("utf-8"))
            await live.handle.stdin.drain()
        live.turn_buffer.clear()
        live.turn_in_flight = True
        live.last_activity = datetime.now(timezone.utc)
        live.state = SessionState.ACTIVE

    async def leave_session(self, chat_id: str, participant_email: str) -> None:
        """SR-9: atomically stamp left_at, remove+close the leaver's sink,
        refresh live.participant_emails, then respawn under the narrowed
        intersection. After this method returns, zero frames will reach the
        removed sink — the sink is removed from live.sinks BEFORE _broadcast
        is called again, and we await its close() before returning."""
        live = self._live.get(chat_id)
        if live is None:
            return
        self._repo.remove_participant(chat_id, participant_email)  # stamps left_at
        leaving = [s for s in live.sinks if s.participant_email == participant_email]
        live.sinks = [s for s in live.sinks if s.participant_email != participant_email]
        for s in leaving:
            try:
                await s.sink.close()
            except Exception:
                logger.exception("close leaver sink failed for %s", chat_id)
        parts = self._repo.get_session_participants(chat_id)
        live.participant_emails = [p.user_email for p in parts if p.left_at is None]
        await self._respawn_co_runner(live)

    async def _respawn_co_runner(self, live: LiveSession) -> None:
        """Recompute intersection for remaining participants and re-spawn runner.

        Called after a participant leaves (SR-7). Kills the current handle,
        rebuilds the ephemeral workspace under the new (narrower) intersection,
        spawns a fresh runner, and replaces live.current_pump. If no participants
        remain, kills the session entirely."""
        if not live.participant_emails:
            await self.kill(live.chat_id, reason="all_participants_left")
            return
        session = self._repo.get_session(live.chat_id)
        if session is None:
            return
        from src.grant_intersection import compute_grant_intersection

        inter = compute_grant_intersection(live.participant_emails, self._repo._conn)
        session_dir = self._workdir_mgr.prepare_ephemeral_session_dir(
            live.chat_id,
            live.participant_emails,
            inter,
        )
        # Cancel the crash-respawn wait task BEFORE killing the handle: this
        # respawn is intentional, and a running _wait_for_exit_and_respawn
        # would otherwise see the kill's non-zero exit as a crash and respawn
        # a second time (double-respawn race → multiple concurrent runners).
        # A fresh wait task bound to the new session_dir is started below.
        old_wait = live.current_wait
        if old_wait is not None and not old_wait.done():
            old_wait.cancel()
            try:
                await old_wait
            except (asyncio.CancelledError, Exception):
                pass
        if old_wait is not None and old_wait in live.tasks:
            live.tasks.remove(old_wait)
        live.current_wait = None
        if live.handle is not None:
            try:
                await live.handle.kill()
            except Exception:
                logger.exception("_respawn_co_runner: kill old handle failed")
        new_handle = await self._spawn_runner(session, session_dir)
        live.handle = new_handle
        live.state = SessionState.ACTIVE
        await self._broadcast(live, {"type": "ready"})
        # Replay last 3 user turns skipping departed participants (SR-11).
        history = self._repo.list_messages(live.chat_id)[-3:]
        live_emails = set(live.participant_emails) or {live.user_email}
        for msg in history:
            if msg.role != "user":
                continue
            author = getattr(msg, "sender_email", None) or live.user_email
            if author not in live_emails:
                continue  # SR-11: do not replay a departed participant's turn
            payload = json.dumps({"type": "user_msg", "text": msg.content}) + "\n"
            async with live._stdin_lock:
                new_handle.stdin.write(payload.encode("utf-8"))
                await new_handle.stdin.drain()
        old_pump = live.current_pump
        if old_pump is not None and not old_pump.done():
            old_pump.cancel()
            try:
                await old_pump
            except (asyncio.CancelledError, Exception):
                pass
        if old_pump is not None and old_pump in live.tasks:
            live.tasks.remove(old_pump)
        new_pump = asyncio.create_task(self._pump_subprocess_to_ws(live))
        live.current_pump = new_pump
        live.tasks.append(new_pump)
        # Start a fresh crash-respawn watcher bound to the NEW handle and the
        # NEW (narrowed-intersection) session_dir, so a genuine later crash
        # respawns with the correct workspace — not the pre-leave wider one.
        new_wait = asyncio.create_task(self._wait_for_exit_and_respawn(live, session_dir))
        live.current_wait = new_wait
        live.tasks.append(new_wait)

    async def cancel(self, chat_id: str) -> None:
        live = self._live.get(chat_id)
        if live is None or live.handle is None:
            return
        payload = json.dumps({"type": "cancel"}) + "\n"
        async with live._stdin_lock:
            live.handle.stdin.write(payload.encode("utf-8"))
            await live.handle.stdin.drain()
        # Synthetic tool_result so the agent's conversation history reflects
        # the cancellation (per spec § Lifecycle "On cancellation").  Without
        # this, the next user_msg lands in a dangling tool_call context and
        # the model can hallucinate a result.  Persisted to chat_messages so
        # crash-respawn replay sees it too.
        synthetic = {
            "type": "tool_result",
            "tool": "_cancel",
            "result": {"cancelled": True},
        }
        await self._broadcast(live, synthetic)
        self._repo.append_message(
            session_id=chat_id,
            role="assistant",
            content="",
            tool_calls=[{"cancelled": True}],
        )
        await self._broadcast(live, {"type": "cancelled"})

    async def kill(self, chat_id: str, *, reason: str) -> None:
        live = self._live.pop(chat_id, None)
        if live is None:
            return
        live.state = SessionState.DEAD
        # Partial-save: if a turn was in flight, persist the accumulated token
        # text as an interrupted assistant message so it's not lost.
        if live.turn_buffer:
            partial = "".join(f.get("text", "") for f in live.turn_buffer if f.get("type") == "token").strip()
            if partial:
                self._repo.append_message(
                    session_id=chat_id,
                    role="assistant",
                    content=partial,
                    tool_calls=[{"interrupted": True, "reason": reason}],
                    tokens_in=None,
                    tokens_out=None,
                    model=None,
                )
        if live.handle is not None:
            await live.handle.kill()
        for t in live.tasks:
            t.cancel()
        self._repo.clear_sandbox_ref(chat_id)
        write_audit(
            self._repo._conn,
            user_email=live.user_email,
            action="chat.session_killed",
            details={"session_id": chat_id, "reason": reason},
        )

    # --- auto-title ---------------------------------------------------------

    def _maybe_start_auto_title(self, live: LiveSession) -> None:
        """Schedule a Haiku call to generate a session title if it
        doesn't have one yet. Idempotent per live session — sets
        ``auto_title_started`` before returning so a second
        ``assistant_message`` for the same session is a no-op.

        Best-effort: any failure is swallowed inside the task so the
        chat session never breaks because Haiku is down or
        ``ANTHROPIC_API_KEY`` is missing. The task itself is appended
        to ``live.tasks`` so :meth:`kill` cancels it on shutdown.
        """
        session = self._repo.get_session(live.chat_id)
        if session is None or session.title:
            # Either it already has a title (user-supplied at create
            # time, or a previous auto-title already landed and the
            # flag was reset by a respawn) or the session vanished.
            live.auto_title_started = True
            return
        live.auto_title_started = True
        task = asyncio.create_task(self._run_auto_title(live))
        live.tasks.append(task)

    async def _run_auto_title(self, live: LiveSession) -> None:
        """Task body: fetch the first user message, call Haiku, persist
        the title, broadcast a ``session_renamed`` frame.

        All errors are caught and logged — title generation is a
        cosmetic enhancement, not a load-bearing piece of the chat
        pipeline."""
        from app.chat.auto_title import generate_title

        try:
            first_user = self._repo.get_first_user_message(live.chat_id)
            if not first_user:
                # The runner emitted an assistant_message before any
                # user_msg was persisted — shouldn't happen in normal
                # flow, but bail cleanly if it does.
                return
            title = await generate_title(first_user)
            if not title:
                return
            self._repo.set_title(live.chat_id, title)
            # Push the new title to the live WS so the sidebar +
            # thread header update without a refresh. _broadcast may
            # raise if the socket has dropped — swallow it; the
            # persisted title will surface on the next sidebar load.
            try:
                await self._broadcast(
                    live,
                    {
                        "type": "session_renamed",
                        "chat_id": live.chat_id,
                        "title": title,
                    },
                )
            except Exception:
                logger.debug(
                    "auto-title: ws.send_json failed for %s; title still persisted",
                    live.chat_id,
                )
        except Exception:
            logger.exception("auto-title task crashed for %s", live.chat_id)

    # --- idle reaper --------------------------------------------------------

    def start_idle_reaper(self) -> None:
        if self._idle_task is None or self._idle_task.done():
            self._idle_task = asyncio.create_task(self._idle_reaper_loop())

    async def _idle_reaper_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            await self._reap_once()

    async def _reap_once(self) -> None:
        """One sweep of the reaper.

        For live sessions (ACTIVE/IDLE):
        - Idle longer than ``idle_ttl_seconds``: pause (on_detach='pause') or kill.
        - Active time (accumulated + current segment) exceeds ``max_session_seconds``:
          pause or kill. Active time only counts while ACTIVE — pause stops the clock.
        - Keepalive heartbeat: for ACTIVE sessions with sinks, extend the sandbox
          external timeout so it outlives the in-process reaper horizon.

        Paused-TTL sweep (repo rows, no live session required):
        - Sessions whose sandbox_paused_at is older than ``paused_ttl_seconds``
          have their sandbox destroyed and refs cleared.
        """
        idle_cutoff = self._config.idle_ttl_seconds
        max_active = self._config.max_session_seconds
        now = datetime.now(timezone.utc)
        now_mono = time.monotonic()

        to_pause: list[str] = []
        to_kill: list[tuple[str, str]] = []

        for chat_id, live in list(self._live.items()):
            if live.state not in (SessionState.ACTIVE, SessionState.IDLE):
                continue
            # Active-time cap: accumulator + current active segment.
            active_total = live.active_seconds_accum
            if live.state == SessionState.ACTIVE:
                active_total += now_mono - live.active_since
            if active_total > max_active:
                if self._config.on_detach == "pause":
                    to_pause.append(chat_id)
                else:
                    to_kill.append((chat_id, "max_session_seconds"))
                continue
            # Idle TTL: last_activity recency check.
            if (now - live.last_activity).total_seconds() > idle_cutoff:
                if self._config.on_detach == "pause":
                    to_pause.append(chat_id)
                else:
                    to_kill.append((chat_id, "idle_ttl"))
                continue
            # Keepalive heartbeat for ACTIVE sessions with at least one sink.
            if live.state == SessionState.ACTIVE and live.sinks and live.handle is not None:
                try:
                    await self._provider.keepalive(
                        live.handle,
                        timeout_seconds=idle_cutoff + 300,
                    )
                except Exception:
                    logger.debug("keepalive failed for %s", chat_id)

        for chat_id in to_pause:
            live = self._live.get(chat_id)
            if live is not None:
                try:
                    await self._pause_live(live)
                except Exception:
                    logger.exception("reaper pause failed for %s — killing", chat_id)
                    await self.kill(chat_id, reason="reaper_pause_failed")

        for chat_id, reason in to_kill:
            await self.kill(chat_id, reason=reason)

        # Paused-TTL sweep: destroy sandboxes that have been paused too long.
        # Works purely from repo rows — catches pre-restart leftovers too.
        paused_cutoff = now - timedelta(seconds=self._config.paused_ttl_seconds)
        for session in self._repo.list_paused_sessions(paused_before=paused_cutoff):
            try:
                await self._provider.destroy(sandbox_id=session.sandbox_id)
            except Exception:
                logger.debug("destroy sandbox %s failed (already gone?)", session.sandbox_id)
            self._repo.clear_sandbox_ref(session.id)
            # Drop any in-memory entry
            self._live.pop(session.id, None)
