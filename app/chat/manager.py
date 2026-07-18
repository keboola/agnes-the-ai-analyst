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

from app.chat import routing
from app.chat.audit import hash_args, write_audit
from app.chat.config import ChatConfig
from app.chat.frame_seq import stamp_frame
from app.chat.persistence import ChatRepository
from app.chat.profiles import get_profile
from app.chat.provider import SandboxHandle, SandboxProvider
from app.chat.types import ChatSession, SessionState, Surface
from app.chat.workdir import WorkdirManager
from app.coordination.base import CoordinationUnavailable
from app.coordination.factory import coordination
from app.coordination.leases import default_holder_id
from src.repositories import ticket_repo, usage_repo, users_repo

logger = logging.getLogger(__name__)

# Sonnet pricing constants (USD per million tokens)
_PRICE_IN_PER_MTOK = 3.0
_PRICE_OUT_PER_MTOK = 15.0

# Coordination-backend TTLs for the shared rate-limit/quota counters
# (wave-2C task 4 — see _msg_window_key / _daily_token_keys). Both simply
# need to outlive the wall-clock bucket their key encodes; the counter
# resets to a fresh window the moment the bucket string itself rolls over
# (a new hour/day), not when the TTL expires — the TTL only protects
# against the old bucket's key lingering in the backend forever.
_MSG_WINDOW_TTL_SEC = 2 * 3600  # hour bucket + 1h headroom
_DAILY_TOKENS_TTL_SEC = 25 * 3600  # day bucket + 1h headroom

# Lease guarding the once-per-(user, date) DB seed in
# _seed_daily_tokens_from_db_if_needed (see its docstring). Only needs to
# outlive one DB read + two coordination incr() calls, not the whole day
# bucket — a holder that crashes mid-seed self-heals on the very next
# message (lease expires, another request tries again).
_DAILY_TOKENS_SEED_LEASE_TTL_SEC = 15

# Leader-lease name + TTL for the paused-sandbox TTL sweep inside
# _reap_once (wave-2C task 3). ~90s: comfortably longer than a single
# sweep normally takes (so a healthy replica's own `lease_release` in the
# `finally` clears it long before expiry) but short enough that a replica
# that crashes mid-sweep self-heals within one extra reaper tick or two
# (the reaper runs every 60s — see `_idle_reaper_loop`), not minutes.
_PAUSED_SWEEP_LEASE_NAME = "paused-sandbox-sweep"
_PAUSED_SWEEP_LEASE_TTL_SEC = 90

# Session routing lease (wave-2F task 1 — see app/chat/routing.py). Claimed
# for `chat:{chat_id}` when a session becomes live in this process's
# `self._live` (_spawn_live / _resume_from_row), renewed once per
# idle-reaper tick (_reap_once, ~60s cadence — see _idle_reaper_loop),
# released on teardown (kill()). 180s = 3x the reaper cadence, same
# heartbeat-safety-margin convention as run_with_lease's ttl_s/3 renew
# interval (app/coordination/leases.py) — a single missed reaper tick must
# not lose the lease.
_ROUTING_LEASE_TTL_SEC = 180

# Chat sandbox secret broker (2026-07-14 incident hardening): bumped whenever
# the ticket_push stdin frame contract changes. ChatManager only ever
# considers a session's runner "known-current-protocol" after it has itself
# pushed a ticket to it in this process (see ``_known_protocol_sessions`` /
# ``_push_ticket_frame``) — a session it has no such record for is treated as
# potentially legacy (pre-broker runner) and force-respawned rather than
# resumed (AC-G-resume-legacy). This is deliberately process-lifetime state,
# not a persisted column: it is always empty right after a restart, so a
# genuine restart always force-respawns rather than risk reconnecting an old
# runner that cannot make sense of the ticket_push frame.
RELAY_PROTOCOL_VERSION = 1


def agnes_server_url() -> str:
    """Server URL the chat sandbox and the seeded workspace use to reach Agnes.

    Resolution order — first non-empty wins:

    1. ``SERVER_URL`` — the deployment's public URL.
    2. ``AGNES_INTERNAL_URL`` — data-rails-only override for deployments that
       cannot (or don't want to) set ``SERVER_URL``; it feeds only this chain,
       never OAuth issuers or discovery metadata.
    3. Loopback — local dev.

    Feeds both the sandbox env (``AGNES_SERVER``, read by the agnes CLI) and
    the workspace seed (``WorkdirManager.server_url`` in app/main.py) so the
    two rails can never drift apart again.
    """
    url = os.environ.get("SERVER_URL") or os.environ.get("AGNES_INTERNAL_URL") or "http://127.0.0.1:8000"
    return url.rstrip("/")


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
    # Surface the session was created on (web / slack_dm / slack_thread) —
    # carried onto emitted chat.message usage events so telemetry can slice
    # chat activity per surface without a chat_sessions join.
    surface: str = Surface.WEB.value
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
        # Per-user message-rate window (chat-msgs:...) and daily token spend
        # (chat-tokens:...) now live in the coordination backend (see
        # _msg_window_key / _daily_token_keys below) instead of process-local
        # structures — a process-local deque/dict would give each replica of
        # a multi-process deployment its own independent quota, letting a
        # client multiply its effective limit by the replica count.
        #
        # Spawn-time authoring profile per session id (not persisted). Set in
        # create_session, consumed in _spawn_live. After a process restart the
        # map is empty, but the profile is already materialized on disk in the
        # session workdir, so resume still resolves the persona + skill.
        self._session_profiles: dict[str, str] = {}
        # Chat sandbox secret broker: chat_ids this process has itself pushed
        # a current-protocol ticket to (see RELAY_PROTOCOL_VERSION /
        # _push_ticket_frame). Consulted by the resume paths to decide
        # respawn vs. reconnect (AC-G-resume-legacy).
        self._known_protocol_sessions: set[str] = set()

    @staticmethod
    def _daily_token_keys(user_email: str) -> tuple[str, str]:
        """Coordination-backend counter keys for `user_email`'s running
        daily Anthropic token spend, bucketed by UTC calendar date.

        TTL (``_DAILY_TOKENS_TTL_SEC``, 25h) deliberately outlives the
        24h day it buckets — same "TTL only matters at first-write, and
        just needs to comfortably outlive the window" reasoning as
        ``_msg_window_key``'s 2h TTL on an hour bucket.
        """
        date_bucket = datetime.now(timezone.utc).strftime("%Y%m%d")
        return (
            f"chat-tokens:{user_email}:{date_bucket}:in",
            f"chat-tokens:{user_email}:{date_bucket}:out",
        )

    def _daily_token_totals(self, user_email: str) -> tuple[int, int]:
        """Return (tokens_in, tokens_out) accumulated today for `user_email`.

        Reads the coordination-backend running counters that
        ``_record_daily_tokens`` adds to as turns complete — a
        ``amount=0`` increment is a deliberate no-op "peek" (see
        ``CoordinationBackend.incr``), not a real event.

        Replaces a DB aggregate query (``ChatRepository.daily_anthropic_tokens``)
        fronted by a 60-second process-local TTL cache: in a multi-process
        deployment that cache was N independently-stale copies of the same
        query result, whereas every process now reads and writes the same
        shared counter.

        FLUSHALL / restart story: a lost counter (Redis FLUSHALL, or — the
        common case under the default ``memory`` backend — ANY process
        restart, including a routine mid-day deploy) reads back from the
        bare peek as "0 spent today" even though ``chat_messages`` may hold
        real spend for the day. Left unhandled, that would silently
        re-open the full daily budget until the day's usage re-accumulates
        from scratch — exactly the bug this method closes: a ``(0, 0)``
        peek is not trusted at face value, it triggers a one-time-per-day
        fallback seed from the DB aggregate
        (``ChatRepository.daily_anthropic_tokens``, still the durable
        source of truth used for dashboards/reporting — see
        ``_seed_daily_tokens_from_db_if_needed`` for the full mechanism and
        its double-seed race guard) before the caller ever sees the
        totals, so a restart-lost counter re-inherits today's true spend
        instead of starting over at zero.
        """
        key_in, key_out = self._daily_token_keys(user_email)
        try:
            tokens_in = coordination().incr(key_in, amount=0, ttl_s=_DAILY_TOKENS_TTL_SEC)
            tokens_out = coordination().incr(key_out, amount=0, ttl_s=_DAILY_TOKENS_TTL_SEC)
        except CoordinationUnavailable:
            logger.warning(
                "daily token budget check: coordination backend unavailable; treating %s as 0 spent today",
                user_email,
            )
            return (0, 0)
        if tokens_in == 0 and tokens_out == 0:
            tokens_in, tokens_out = self._seed_daily_tokens_from_db_if_needed(user_email, key_in, key_out)
        return tokens_in, tokens_out

    def _seed_daily_tokens_from_db_if_needed(self, user_email: str, key_in: str, key_out: str) -> tuple[int, int]:
        """Seed `key_in`/`key_out` from the DB aggregate the first time
        `_daily_token_totals` sees a ``(0, 0)`` counter reading for a given
        (user, UTC date) — see that method's FLUSHALL / restart story.

        A ``(0, 0)`` reading is ambiguous: it could be a fresh coordination
        backend that has genuinely never recorded any spend for this user
        today (nothing to seed), or a real restart that just lost non-zero
        history. The counter value alone can't distinguish the two, so
        every ``(0, 0)`` reading is treated as a potential miss — but the
        DB is consulted at most ONCE per (user, date): a separate TTL-KV
        marker (``chat-tokens-seeded:{user}:{date}``, not the counter
        itself — an aggregate of exactly 0 is a legitimate steady state
        for an idle user and must not force a re-query on every one of
        their messages for the rest of the day) is set right after the DB
        read regardless of what the aggregate was, and checked before any
        further attempt.

        Race: two requests can both observe the marker absent for the same
        first-ever miss and both try to seed, which would double-count the
        DB aggregate onto the counter. A short-lived seed lease
        (``chat-tokens-seed:{user}:{date}``,
        ``_DAILY_TOKENS_SEED_LEASE_TTL_SEC``) makes exactly one of them
        perform the DB read + counter seed + marker write; the loser does
        not wait on the winner — it returns the ``(0, 0)`` it already
        peeked. That's a one-message, self-correcting blip (the very next
        message from this user reads the now-seeded counter), not a lost
        enforcement window, and matches this whole mechanism's existing
        "soft cost guardrail, not a billing ledger" posture.

        Coordination-backend outage during the seed attempt (lease
        acquire, the DB read, or either ``incr``) is treated the same as
        an ordinary miss: return ``(0, 0)`` and let the caller's own
        ``CoordinationUnavailable`` handling (already in
        ``_daily_token_totals``) or the next call retry.
        """
        date_bucket = datetime.now(timezone.utc).strftime("%Y%m%d")
        seeded_marker_key = f"chat-tokens-seeded:{user_email}:{date_bucket}"
        seed_lease_name = f"chat-tokens-seed:{user_email}:{date_bucket}"
        try:
            if coordination().kv_get(seeded_marker_key) is not None:
                return (0, 0)  # already checked the DB for this day-bucket
        except CoordinationUnavailable:
            return (0, 0)

        holder = default_holder_id()
        try:
            acquired = coordination().lease_acquire(seed_lease_name, holder, ttl_s=_DAILY_TOKENS_SEED_LEASE_TTL_SEC)
        except CoordinationUnavailable:
            return (0, 0)
        if not acquired:
            # Another request is (or just finished) seeding this bucket —
            # don't block on it; the next check will see the result.
            return (0, 0)
        try:
            # Re-check under the lease: another request may have finished
            # seeding and set the marker between our first kv_get and
            # acquiring the lease.
            if coordination().kv_get(seeded_marker_key) is not None:
                return (0, 0)
            agg_in, agg_out = self._repo.daily_anthropic_tokens(user_email)
            tokens_in = coordination().incr(key_in, amount=agg_in, ttl_s=_DAILY_TOKENS_TTL_SEC) if agg_in else 0
            tokens_out = coordination().incr(key_out, amount=agg_out, ttl_s=_DAILY_TOKENS_TTL_SEC) if agg_out else 0
            coordination().kv_set(seeded_marker_key, "1", ttl_s=_DAILY_TOKENS_TTL_SEC)
            return tokens_in, tokens_out
        except CoordinationUnavailable:
            return (0, 0)
        finally:
            try:
                coordination().lease_release(seed_lease_name, holder)
            except CoordinationUnavailable:
                pass

    def _record_daily_tokens(self, user_email: str, tokens_in: Optional[int], tokens_out: Optional[int]) -> None:
        """Add one completed turn's token delta to `user_email`'s running
        daily counters (see ``_daily_token_totals``).

        Attributed to the SESSION OWNER (`live.user_email`), matching
        ``ChatRepository.daily_anthropic_tokens``'s existing JOIN semantics
        — it sums every message in every session owned by `user_email`,
        not just messages a particular sender typed (an assistant reply
        has no ``sender_email`` of its own to attribute by in the first
        place; co-session per-sender attribution for the ASSISTANT's own
        token spend was never implemented pre-this-task either).
        """
        tin = tokens_in or 0
        tout = tokens_out or 0
        if not tin and not tout:
            return
        key_in, key_out = self._daily_token_keys(user_email)
        try:
            if tin:
                coordination().incr(key_in, amount=tin, ttl_s=_DAILY_TOKENS_TTL_SEC)
            if tout:
                coordination().incr(key_out, amount=tout, ttl_s=_DAILY_TOKENS_TTL_SEC)
        except CoordinationUnavailable:
            logger.warning(
                "daily token budget: coordination backend unavailable; turn's tokens not recorded for %s",
                user_email,
            )

    @staticmethod
    def _msg_window_key(sender: str) -> str:
        """Coordination-backend counter key for `sender`'s hourly message-rate
        window, bucketed by UTC hour (fixed window, not the previous
        per-process sliding window — see ``send_user_message``).

        Disclosure: fixed UTC-hour windows, not sliding — a sender can send
        up to the full ``rate_messages_per_hour`` quota right before an hour
        boundary (e.g. at :59) and another full quota right after it (at
        :00), so up to ~2x the configured hourly rate can land in a short
        burst straddling the boundary. This is standard fixed-window
        limiter behavior (traded for statelessness across restarts/replicas
        via the coordination backend) and is a looser, not stricter, bound
        than the sliding window it replaced.

        FLUSHALL / restart story: a lost counter just means the current
        hour's count restarts at zero — a sender gets a fresh full quota
        for the rest of the hour rather than losing the entire hour. Same
        "soft guardrail, briefly looser after a backend hiccup" story as
        ``_daily_token_totals``.
        """
        hour_bucket = datetime.now(timezone.utc).strftime("%Y%m%d%H")
        return f"chat-msgs:{sender}:{hour_bucket}"

    # --- public API used by app/api/chat.py and services/slack_bot/ -------

    async def create_session(
        self,
        *,
        user_email: str,
        surface: Surface,
        slack_channel_id: Optional[str] = None,
        slack_thread_ts: Optional[str] = None,
        title: Optional[str] = None,
        profile: Optional[str] = None,
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
        if profile is not None:
            self._session_profiles[created.id] = profile
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
        # Deliberately process-local (unlike the rate/quota counters above)
        # for wave-2C task 4: it counts LIVE sandbox handles in THIS
        # process's `self._live`, not a value coordination() can hold — the
        # concurrency cap is only meaningful once it can see every replica's
        # live sessions for a user, which needs a real per-session lease
        # (who is this session's owning replica right now?), not a plain
        # counter. That lands in wave-2C's WS D once routing leases exist;
        # building a throwaway counter-based version here now would just be
        # thrown away then. Until WS D, this cap is only accurate within one
        # process/replica — a documented, temporary gap, not a silent one.
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
            if session is None:
                raise SessionNotFound(chat_id)
        live = await self._spawn_live(session)
        await self._seat_sink(live, ws, is_primary=is_primary)

    async def _seat_sink(self, live: "LiveSession", ws, *, is_primary: bool) -> None:
        """Replay the in-progress turn buffer to ws, append to sinks, send ready.

        Deliberately does NOT replay persisted history: the web client loads
        it via GET /sessions/{id}/messages before opening the WS (replaying
        here would render every message twice), and the Slack bridge must not
        re-post old messages into the channel. Full history replay lives only
        in add_sink() for late joiners that have no REST history-load step.
        The turn buffer IS replayed — a mid-turn reconnect picks up exactly
        the frames the runner already emitted (snapshot to avoid racing the
        pump task)."""
        for frame in list(live.turn_buffer):
            await ws.send_json(frame)
        if is_primary:
            live.sinks.insert(0, SinkEntry(participant_email=live.user_email, sink=ws))
        else:
            live.sinks.append(SinkEntry(participant_email=live.user_email, sink=ws))
        # Sent directly to this one sink (not _broadcast — every other sink
        # already has its own connection, no fan-out needed here), so it
        # needs its own stamp (wave-2F task 2).
        await ws.send_json(stamp_frame(live.chat_id, {"type": "ready"}))

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
            prof_slug = self._session_profiles.get(session.id)
            prof = get_profile(prof_slug) if prof_slug else None
            session_dir = self._workdir_mgr.prepare_session_dir(session.user_email, chat_id, profile=prof)

        handle = await self._spawn_runner(session, session_dir)
        import time as _t

        live = LiveSession(
            chat_id=chat_id,
            user_email=session.user_email,
            state=SessionState.ACTIVE,
            handle=handle,
            started_at=datetime.now(timezone.utc),
            last_activity=datetime.now(timezone.utc),
            surface=getattr(session.surface, "value", str(session.surface)),
            sinks=[],
            participant_emails=emails,
            session_dir=session_dir,
            active_since=_t.monotonic(),
        )
        self._live[chat_id] = live
        await self._claim_routing_lease(chat_id)
        self._repo.set_sandbox_ref(chat_id, sandbox_id=handle.sandbox_id, runner_pid=handle.pid)
        # Broker: push the session's initial main+mcp tickets before the
        # session is considered ready to serve messages.
        await self._push_ticket_frame(live)
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
        # Wait for any in-flight turn to complete first. The spin must
        # also bail if the runner died (3× crash → SessionState.DEAD)
        # without ever emitting a `done` frame to clear `turn_in_flight`:
        # without this guard the loop would spin forever and the entry
        # in `_live` would leak (the reaper skips DEAD sessions).
        # Devin Review BUG_0001 follow-up from #605.
        while live.turn_in_flight:
            if live.state != SessionState.ACTIVE:
                return
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
        cancelled = list(live.tasks)
        for t in cancelled:
            t.cancel()
        # Drain the cancelled tasks before touching the provider: if pause()
        # fails and we fall back to kill(), an un-awaited pump task would be
        # orphaned and could write a frame into a handle kill() has already
        # torn down.
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)
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
        """Resume a PAUSED in-memory session by reconnecting the sandbox.

        AC-G-resume-legacy: a session this process has never itself pushed a
        current-protocol ticket to (``_known_protocol_sessions``) is never
        reconnected via resume() — an old runner might not understand the
        ``ticket_push`` stdin frame — so we force a fresh spawn instead.
        """
        if live.chat_id not in self._known_protocol_sessions:
            # Destroy the old (paused, billable) sandbox BEFORE respawning —
            # _respawn_fresh overwrites sandbox_id via set_sandbox_ref, so
            # without this the paused microVM is orphaned and leaks until its
            # absolute TTL (mirror of the _resume_from_row legacy path; Devin
            # review on #849).
            session = self._repo.get_session(live.chat_id)
            if session is not None:
                await self._destroy_old_sandbox(session)
                self._repo.clear_sandbox_ref(live.chat_id)
            # Revoke the paused session's old broker tickets BEFORE _respawn_fresh
            # mints+pushes new ones — same as the non-legacy resume path below.
            # revoke_session deletes by session_id, so revoking after the fresh
            # mint would delete the ticket _respawn_fresh just pushed. Without
            # this, the old tickets linger (redeemable) until their TTL even
            # though the old sandbox is gone. (Devin review on #851)
            ticket_repo().revoke_session(live.chat_id)
            await self._respawn_fresh(live)
            return
        session = self._repo.get_session(live.chat_id)
        if session is None or session.sandbox_id is None or session.runner_pid is None:
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
        # AC-G-resume-fresh: rotate tickets on every resume — the paused
        # window may have exceeded their TTL — before any further message is
        # forwarded. Revoke the old ones FIRST: revoke_session deletes by
        # session_id, so revoking after the fresh mint would delete the
        # tickets _push_ticket_frame just pushed.
        ticket_repo().revoke_session(live.chat_id)
        await self._push_ticket_frame(live)
        pump_task = asyncio.create_task(self._pump_subprocess_to_ws(live))
        wait_task = asyncio.create_task(self._wait_for_exit_and_respawn(live, live.session_dir or Path("/tmp")))
        live.tasks = [pump_task, wait_task]
        live.current_pump = pump_task
        live.current_wait = wait_task
        self._repo.set_sandbox_paused_at(live.chat_id, None)

    async def _destroy_old_sandbox(self, session: "ChatSession") -> None:
        """Best-effort teardown of a session's paused E2B sandbox before its
        refs are cleared. Never raises — a destroy failure must not block the
        fresh spawn, but skipping it entirely leaks a billable microVM (§11)."""
        sandbox_id = getattr(session, "sandbox_id", None)
        if not sandbox_id:
            return
        try:
            await self._provider.destroy(sandbox_id=sandbox_id)
        except Exception:
            logger.warning("failed to destroy old sandbox %s for %s (continuing)", sandbox_id, session.id)

    async def _resume_from_row(self, session: "ChatSession") -> Optional["LiveSession"]:
        """Post-restart resume: no LiveSession in memory, but repo row has refs.

        Returns a new LiveSession on success, None on failure (refs cleared).

        AC-G-resume-legacy: ``_known_protocol_sessions`` is always empty right
        after a process restart, so this branch fires on every genuine
        restart — deliberately conservative: reconnecting via resume() risks
        an old runner that predates the ticket_push stdin contract, so we
        force a fresh spawn (which always starts a current-protocol runner
        and pushes its own ticket) via the existing _spawn_live path instead
        of resuming the possibly-legacy process.
        """
        if session.id not in self._known_protocol_sessions:
            # Force a fresh spawn rather than resume a possibly-legacy runner.
            # Destroy the old (paused, billable) sandbox BEFORE clearing its
            # ref — clear_sandbox_ref NULLs sandbox_paused_at, after which the
            # paused-TTL reaper can never find it, so skipping the destroy here
            # leaks one E2B microVM per resumable session on every restart (§11).
            await self._destroy_old_sandbox(session)
            self._repo.clear_sandbox_ref(session.id)
            return await self._spawn_live(session)
        import time as _t

        try:
            handle = await self._provider.resume(
                sandbox_id=session.sandbox_id,
                runner_pid=session.runner_pid,
                env={},
            )
        except Exception:
            logger.warning(
                "_resume_from_row failed for %s — destroying old sandbox + clearing refs for fresh spawn",
                session.id,
            )
            await self._destroy_old_sandbox(session)
            self._repo.clear_sandbox_ref(session.id)
            return None
        # Mirror _spawn_live's workspace selection: co-sessions get the
        # ephemeral grant-intersection dir (SR-6), never a personal one —
        # the crash-respawn path re-uploads the workspace from session_dir.
        if session.is_co_session:
            parts = self._repo.get_session_participants(session.id)
            emails = [p.user_email for p in parts if p.left_at is None]
            from src.grant_intersection import compute_grant_intersection

            inter = compute_grant_intersection(emails, self._repo._conn)
            session_dir = self._workdir_mgr.prepare_ephemeral_session_dir(session.id, emails, inter)
        else:
            emails = []
            self._workdir_mgr.ensure_user_workdir(session.user_email)
            session_dir = self._workdir_mgr.prepare_session_dir(session.user_email, session.id)
        live = LiveSession(
            chat_id=session.id,
            user_email=session.user_email,
            state=SessionState.ACTIVE,
            handle=handle,
            started_at=datetime.now(timezone.utc),
            last_activity=datetime.now(timezone.utc),
            surface=getattr(session.surface, "value", str(session.surface)),
            sinks=[],
            session_dir=session_dir,
            active_since=_t.monotonic(),
            participant_emails=emails,
        )
        self._live[session.id] = live
        await self._claim_routing_lease(session.id)
        self._repo.set_sandbox_paused_at(session.id, None)
        # This branch only runs when session.id IS a known-current-protocol
        # session (the legacy branch above returns early), but the runner's
        # relay memory does not survive the pause/resume round trip, so it
        # still needs a fresh ticket before serving messages.
        ticket_repo().revoke_session(session.id)
        await self._push_ticket_frame(live)
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
        await self._push_ticket_frame(live)
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
        # Sent directly to this one sink (not _broadcast), so it needs its
        # own stamp (wave-2F task 2). The history-replay frames above are
        # reconstructed from persisted chat_messages, which predates seq
        # entirely — left unstamped, per the additive/back-compat contract.
        await sink.send_json(stamp_frame(chat_id, {"type": "ready"}))

    async def _spawn_runner(self, session: ChatSession, session_dir: Path):
        from app.auth.access import mint_session_jwt, mint_co_session_jwt

        if session.is_co_session:
            # SR-5: NO seed fallback for co-sessions. A mint failure re-raises
            # and aborts the spawn — never inject a seed token (which carries no
            # co claims and could resolve to admin via the normal user path).
            # The JWT itself is no longer forwarded into the sandbox env (see
            # below) — it is minted here purely for its validation side
            # effect (aborting the spawn on a bad co-session); the real
            # session credential now flows to the runner via the ticket
            # broker (_push_ticket_frame), never as a raw env var.
            mint_co_session_jwt(session.id)
        else:
            try:
                mint_session_jwt(session.user_email, session.id)
            except ValueError:
                # User not found in DB (e.g. deleted mid-session) — non-fatal:
                # the spawn still proceeds (the ticket the runner receives
                # will simply fail to redeem at the broker, surfacing a clear
                # auth error to the user on first API call).
                logger.warning(
                    "_spawn_runner: mint_session_jwt failed for %s",
                    session.user_email,
                )
        env = {
            # The agnes CLI inside the sandbox reads its server URL from
            # AGNES_SERVER (cli/config.py) — the previous AGNES_API had no
            # consumer, so `agnes catalog`/`query`/… silently fell back to
            # http://localhost:8000 and could never reach the server. The
            # sandbox is a remote microVM, so this MUST be a reachable URL:
            # prefer SERVER_URL (the deployment's public URL, same value
            # WorkdirManager seeds into the workspace), falling back to
            # AGNES_INTERNAL_URL then loopback — see agnes_server_url().
            # Operators running cloud chat must set one of the two for the
            # data rails to work.
            "AGNES_SERVER": agnes_server_url(),
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
            # No ANTHROPIC_API_KEY / AGNES_TOKEN here (chat sandbox secret
            # broker hardening, 2026-07-14): the real Anthropic key never
            # enters the sandbox env. The runner's own ``_start_relay``
            # starts an in-sandbox loopback relay and points
            # ANTHROPIC_BASE_URL/ANTHROPIC_API_KEY at it with a fixed dummy
            # value — the relay is the only thing that ever holds a real
            # credential, fed in-memory via the ``ticket_push`` stdin frame
            # this manager pushes after spawn/resume (_push_ticket_frame).
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

    async def _push_ticket_frame(self, live: "LiveSession") -> None:
        """Mint fresh main+mcp broker tickets and push them to the sandbox's
        in-process relay over stdin (chat sandbox secret broker, 2026-07-14).

        Every runner process — freshly spawned or reconnected via
        ``provider.resume`` — starts with no ticket in its relay's memory, so
        this must run (under ``_stdin_lock``, like every other stdin write)
        before any user message is forwarded to it. Marks ``live.chat_id`` as
        a known-current-protocol session so a later resume never mistakes it
        for a legacy (pre-broker) runner (AC-G-resume-legacy).
        """
        assert live.handle is not None
        main = ticket_repo().mint(live.chat_id, "main")
        mcp = ticket_repo().mint(live.chat_id, "mcp")
        payload = json.dumps({"type": "ticket_push", "main": main, "mcp": mcp}) + "\n"
        async with live._stdin_lock:
            live.handle.stdin.write(payload.encode("utf-8"))
            await live.handle.stdin.drain()
        self._known_protocol_sessions.add(live.chat_id)

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
                # Feed the turn's token delta into the shared daily-spend
                # counters _daily_token_totals checks in send_user_message.
                self._record_daily_tokens(live.user_email, frame.get("tokens_in"), frame.get("tokens_out"))
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
        broadcast to the others.

        Stamps ``seq``/``id`` onto ``frame`` (wave-2F task 2 — see
        app.chat.frame_seq) BEFORE fanning out, so this is the single seam
        every runner frame and every manager-originated broadcast (ready /
        error / cancelled / session_renamed) shares — Slack, web, and
        co-drive sinks all see the same stamped envelope. ``frame`` is
        mutated in place: callers that also stash it (e.g.
        ``_pump_subprocess_to_ws`` appending to ``live.turn_buffer`` after
        this returns) see the stamped version too, so a later turn-buffer
        replay carries the original seq/id rather than getting re-stamped.
        """
        stamp_frame(live.chat_id, frame)
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
        # A dead-sink sweep can be the moment the LAST sink disappears (e.g.
        # a co-drive joiner's socket died without a clean detach_sink). Fire
        # the same on-detach policy detach_sink would have, or the session
        # outlives its audience until the idle reaper notices.
        if dead and not live.sinks and live.state == SessionState.ACTIVE:
            self._on_all_sinks_gone(live)

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
            # Refresh the persisted refs or a later pause/resume cycle would
            # try to reconnect the DEAD sandbox and lose the agent context.
            self._repo.set_sandbox_ref(live.chat_id, sandbox_id=new_handle.sandbox_id, runner_pid=new_handle.pid)
            await self._push_ticket_frame(live)
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

    def _emit_chat_message_event(self, live: LiveSession, sender: str) -> None:
        """Emit one ``chat.message`` usage event per user chat turn.

        Web and Slack turns both funnel through send_user_message, so this is
        the single chokepoint that makes interactive chat visible in
        /admin/telemetry and the adoption DAU — usage_events otherwise only
        sees desktop CC sessions (agnes push) and server product events.
        Best-effort by contract: telemetry must never block or fail a send.
        """
        try:
            user_id: Optional[str] = None
            try:
                row = users_repo().get_by_email(sender)
                user_id = (row or {}).get("id")
            except Exception:
                # Identity resolution is best-effort; username still keys the event.
                pass
            usage_repo().emit_server_event(
                event_type="chat.message",
                user_id=user_id,
                username=sender,
                props={"surface": live.surface, "session_id": live.chat_id},
            )
        except Exception:
            logger.warning("usage_events emit failed for chat.message (session %s)", live.chat_id)

    async def send_user_message(self, chat_id: str, text: str, *, sender_email: Optional[str] = None) -> None:
        live = self._live.get(chat_id)
        # Resume on-demand: PAUSED live session (Slack DM after hours, web race).
        if live is not None and live.state == SessionState.PAUSED:
            await self._resume_live(live)
        elif live is None:
            # Post-restart: no LiveSession in memory, but repo row may have sandbox refs.
            session = self._repo.get_session(chat_id)
            if session is not None and session.sandbox_id is not None and session.runner_pid is not None:
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
        # Enforce daily Anthropic spend cap — see _daily_token_totals.
        tokens_in, tokens_out = self._daily_token_totals(sender)
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
        # Per-user message-rate cap keyed on the SENDER (SR-10), enforced via
        # a coordination-backend fixed-window counter (see _msg_window_key) —
        # atomic incr-then-compare: this attempt is unconditionally counted
        # (matches how most fixed-window API rate limiters behave — an
        # attempt made while already over the cap still consumes a slot in
        # the window rather than being a free retry) and only rejected if
        # the count including it exceeds the configured cap.
        try:
            attempt_count = coordination().incr(self._msg_window_key(sender), ttl_s=_MSG_WINDOW_TTL_SEC)
        except CoordinationUnavailable:
            logger.warning("message-rate check: coordination backend unavailable; allowing message for %s", sender)
            attempt_count = 0
        if attempt_count > self._config.rate_messages_per_hour:
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
        self._repo.append_message(
            session_id=chat_id,
            role="user",
            content=text,
            sender_email=sender_email or live.user_email,
        )
        self._emit_chat_message_event(live, sender)
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
        # Same stale-ref hazard as the crash-respawn path: persist the new
        # sandbox identity for later pause/resume.
        self._repo.set_sandbox_ref(live.chat_id, sandbox_id=new_handle.sandbox_id, runner_pid=new_handle.pid)
        await self._push_ticket_frame(live)
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
        # Spawn-time profile is no longer needed once the session is torn down;
        # drop it so the map doesn't grow unboundedly with studio usage.
        self._session_profiles.pop(chat_id, None)
        self._known_protocol_sessions.discard(chat_id)
        # Revoke any broker tickets for this session so the rows don't linger
        # in the DB until TTL expiry (the raw values only ever lived in the
        # now-torn-down sandbox relay's memory, so this is hygiene, not a
        # security fix). Runs before the early-return so a not-live session
        # still gets its stale tickets cleared. (Devin review on #849.)
        try:
            ticket_repo().revoke_session(chat_id)
        except Exception:
            logger.warning("broker ticket revocation failed for %s on kill (non-fatal)", chat_id)
        live = self._live.pop(chat_id, None)
        if live is None:
            return
        await self._release_routing_lease(chat_id)
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
            title = await generate_title(first_user, llm_auth=self._config.llm_auth)
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

    # --- session routing leases (wave-2F task 1) -----------------------------
    #
    # `chat:{chat_id}` on the coordination backend marks which gateway
    # replica currently hosts this session's LiveSession — claimed once per
    # entry into `self._live` (`_spawn_live` / `_resume_from_row`), renewed
    # every reaper tick (`_renew_routing_leases`, called from `_reap_once`),
    # released on teardown (`kill`). All three helpers are best-effort: a
    # claim that loses to another gateway, or a renew that is lost, is
    # logged and otherwise ignored here — this process keeps serving the
    # session locally regardless (see app/chat/routing.py's module
    # docstring for why: deciding what to actually DO about a lost/contended
    # lease, e.g. redirecting a client to the real owner, is a later
    # wave-2F task, not this one). Under the default `memory` backend this
    # can never actually contend (single process, nothing else holds the
    # lease), so behavior there is unchanged from before routing leases
    # existed.

    async def _claim_routing_lease(self, chat_id: str) -> None:
        # asyncio.to_thread: routing.claim_session's coordination().lease_acquire
        # is a blocking Redis round-trip (WATCH/MULTI/EXEC) under the redis
        # backend — running it synchronously here would stall this replica's
        # entire event loop once per live session spawn. Same rationale as the
        # paused-sandbox-sweep lease acquire in _reap_once below.
        gateway_id = routing.this_gateway_id()
        claimed = await asyncio.to_thread(routing.claim_session, chat_id, gateway_id, ttl_s=_ROUTING_LEASE_TTL_SEC)
        if not claimed:
            owner = await asyncio.to_thread(routing.owner_of, chat_id)
            logger.warning(
                "routing lease for %s not claimed (held by %s) — serving it locally anyway; "
                "cross-gateway takeover is not yet enforced (wave-2F task 1)",
                chat_id,
                owner,
            )

    async def _release_routing_lease(self, chat_id: str) -> None:
        # asyncio.to_thread: see _claim_routing_lease above.
        await asyncio.to_thread(routing.release_session, chat_id, routing.this_gateway_id())

    async def _renew_routing_leases(self) -> None:
        # asyncio.to_thread: see _claim_routing_lease above — this runs once
        # per non-DEAD live session on every ~60s reaper tick, so a blocking
        # renew per session under the redis backend would otherwise stall the
        # event loop repeatedly on every tick.
        gateway_id = routing.this_gateway_id()
        for chat_id, live in list(self._live.items()):
            if live.state == SessionState.DEAD:
                continue
            renewed = await asyncio.to_thread(routing.renew_session, chat_id, gateway_id, ttl_s=_ROUTING_LEASE_TTL_SEC)
            if not renewed:
                logger.warning(
                    "routing lease for %s lost (expired or claimed by another gateway) — "
                    "continuing to serve it locally; cross-gateway takeover is not yet "
                    "enforced (wave-2F task 1)",
                    chat_id,
                )

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
        - Routing-lease heartbeat: renew every non-DEAD session's `chat:{chat_id}`
          routing lease (wave-2F task 1 — see app/chat/routing.py) so it survives
          the ~60s gap until the next tick.

        Paused-TTL sweep (repo rows, no live session required):
        - Sessions whose sandbox_paused_at is older than ``paused_ttl_seconds``
          have their sandbox destroyed and refs cleared.
        """
        idle_cutoff = self._config.idle_ttl_seconds
        max_active = self._config.max_session_seconds
        now = datetime.now(timezone.utc)
        now_mono = time.monotonic()

        await self._renew_routing_leases()

        to_pause: list[str] = []
        to_kill: list[tuple[str, str]] = []

        for chat_id, live in list(self._live.items()):
            if live.state == SessionState.DEAD:
                # 3x-crash respawn marks a session DEAD without popping it
                # from _live (only kill() pops). GC it here or dead entries
                # accumulate forever on long-running servers; kill() also
                # fires the partial-save and clears sandbox refs.
                to_kill.append((chat_id, "dead_gc"))
                continue
            if live.state not in (SessionState.ACTIVE, SessionState.IDLE):
                continue
            # Active-time cap: accumulator + current active segment.
            active_total = live.active_seconds_accum
            if live.state == SessionState.ACTIVE:
                active_total += now_mono - live.active_since
            if active_total > max_active:
                # Hard ceiling — ALWAYS kill, never pause, regardless of
                # on_detach. active_seconds_accum survives pause/resume by
                # design, so pausing here would re-trip on the next sweep
                # after every resume: an infinite pause/resume loop that
                # leaves the session permanently unusable but never freed.
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
        #
        # Cross-process singleton guard (wave-2C task 3): unlike the
        # idle/active reaping above (which only ever touches THIS replica's
        # in-memory `_live` sessions), this sweep reads the shared
        # `chat_sessions` table directly — every replica of a multi-replica
        # deployment would otherwise race to destroy the same paused
        # sandboxes on the same ~60s tick (harmless-but-wasteful duplicate
        # `destroy()` calls against the sandbox provider). A single
        # non-blocking `lease_acquire` per tick — not the full
        # acquire/renew/reacquire loop in app/coordination/leases.py's
        # `run_with_lease` — is the right granularity here: this work has
        # no long-lived "start"/"stop" state to bracket, just "did this
        # replica win this tick's coin flip". A replica that loses simply
        # tries again next tick (60s later — see `_idle_reaper_loop` — well
        # under any reasonable staleness tolerance for a TTL sweep).
        #
        # FLUSHALL story: the lease is only ever held for the duration of
        # one sweep (released in the `finally` below, or self-healing via
        # `_PAUSED_SWEEP_LEASE_TTL_SEC` if a replica dies mid-sweep) — there
        # is no persistent "held" state to lose if the coordination backend
        # loses its own state, so a backend outage just means every replica
        # skips (or every replica proceeds, under `memory` mode) until the
        # backend recovers; nothing to reacquire across a restart.
        try:
            # asyncio.to_thread: this runs on the same event loop as every
            # other request this replica serves, on a continuous ~60s tick
            # (see _idle_reaper_loop above) — a blocking Redis round-trip
            # here (real backend, not `memory`) must not stall unrelated
            # traffic while it waits on the socket.
            acquired_sweep_lease = await asyncio.to_thread(
                coordination().lease_acquire,
                _PAUSED_SWEEP_LEASE_NAME,
                default_holder_id(),
                ttl_s=_PAUSED_SWEEP_LEASE_TTL_SEC,
            )
        except CoordinationUnavailable:
            logger.warning("paused-sandbox sweep: coordination backend unavailable; skipping this tick")
            acquired_sweep_lease = False
        if acquired_sweep_lease:
            try:
                paused_cutoff = now - timedelta(seconds=self._config.paused_ttl_seconds)
                for session in self._repo.list_paused_sessions(paused_before=paused_cutoff):
                    try:
                        await self._provider.destroy(sandbox_id=session.sandbox_id)
                    except Exception:
                        logger.debug("destroy sandbox %s failed (already gone?)", session.sandbox_id)
                    self._repo.clear_sandbox_ref(session.id)
                    # Drop any in-memory entry
                    self._live.pop(session.id, None)
                    # This sweep destroys the sandbox directly rather than
                    # going through kill() (the usual _release_routing_lease
                    # call site), so without an explicit release here the
                    # routing lease would only self-heal at its own TTL
                    # (_ROUTING_LEASE_TTL_SEC, ~180s) instead of freeing
                    # immediately. Best-effort: a release failure must not
                    # break the rest of the sweep.
                    try:
                        await self._release_routing_lease(session.id)
                    except Exception:
                        logger.debug(
                            "routing lease release failed for %s during paused-sandbox sweep",
                            session.id,
                        )
            finally:
                try:
                    await asyncio.to_thread(coordination().lease_release, _PAUSED_SWEEP_LEASE_NAME, default_holder_id())
                except CoordinationUnavailable:
                    pass
        else:
            logger.debug("paused-sandbox sweep: lease held elsewhere this tick; skipping")
