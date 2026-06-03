"""ChatManager: session state machine, lifecycle, WS attachment."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    # Set to True once an auto-title task has been scheduled for this
    # session — guarantees we only fire Haiku once per live session
    # even if the user sends a second turn while the first one is
    # still in-flight.
    auto_title_started: bool = False
    # Output sinks the runner's frames fan out to. One SinkEntry per
    # attached principal (web WS or SlackSinkBridge). The primary sink is
    # seated by attach(); add_sink() appends late joiners (co-drive, 5b).
    sinks: list["SinkEntry"] = field(default_factory=list)
    # Serializes the stdin write+drain pair so two participants' concurrent
    # turns can never interleave partial JSON lines on the shared stdin
    # (spec §6.2).
    _stdin_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


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
                f"user {user_email} has {active} active sessions; cap = "
                f"{self._config.concurrency_per_user}"
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
                    user_email, surface=Surface.WEB, exclude_id=created.id,
                )
            except Exception:
                logger.exception(
                    "archive_empty_user_sessions failed for %s; not fatal",
                    user_email,
                )
        return created

    def _active_count_for_user(self, user_email: str) -> int:
        return sum(
            1
            for s in self._live.values()
            if s.user_email == user_email
            and s.state in (SessionState.NEW, SessionState.ACTIVE, SessionState.IDLE)
        )

    def active_count_for_user(self, user_email: str) -> int:
        """Public wrapper over the private cap predicate so callers
        (e.g. /agnes-status) report exactly what create_session enforces."""
        return self._active_count_for_user(user_email)

    def list_live(self) -> list[LiveSession]:
        return list(self._live.values())

    async def shutdown(self) -> None:
        chat_ids = list(self._live.keys())
        for chat_id in chat_ids:
            try:
                await self.kill(chat_id, reason="server_shutdown")
            except Exception:
                logger.exception("error killing session %s on shutdown", chat_id)

    # --- attach + runtime methods (Task 5.2) --------------------------------

    async def attach(self, chat_id: str, ws, *, is_primary: bool = True) -> None:
        session = self._repo.get_session(chat_id)
        if session is None:
            raise SessionNotFound(chat_id)

        self._workdir_mgr.ensure_user_workdir(session.user_email)
        session_dir = self._workdir_mgr.prepare_session_dir(session.user_email, chat_id)

        handle = await self._spawn_runner(session, session_dir)
        # 5a seats the primary sink unconditionally. is_primary is part of
        # the spec §6.3 attach contract so 5b co-drive can attach a runner
        # without a primary seat (it seats collaborators via add_sink); 5a
        # never passes is_primary=False, so the primary is always present.
        live = LiveSession(
            chat_id=chat_id,
            user_email=session.user_email,
            state=SessionState.ACTIVE,
            handle=handle,
            started_at=datetime.now(timezone.utc),
            last_activity=datetime.now(timezone.utc),
            sinks=(
                [SinkEntry(participant_email=session.user_email, sink=ws)]
                if is_primary else []
            ),
        )
        self._live[chat_id] = live
        await ws.send_json({"type": "ready"})

        pump_task = asyncio.create_task(self._pump_subprocess_to_ws(live))
        wait_task = asyncio.create_task(self._wait_for_exit_and_respawn(live, session_dir))
        live.tasks = [pump_task, wait_task]
        live.current_pump = pump_task

        try:
            await asyncio.gather(*live.tasks, return_exceptions=True)
        finally:
            await self.kill(chat_id, reason="ws_disconnect")

    async def add_sink(self, chat_id: str, sink, participant_email: str) -> None:
        """Attach an additional output sink to an already-live session.

        Replays persisted history to the new sink BEFORE appending it to the
        broadcast list, so a late joiner never misses in-flight frames and
        never double-receives one (replay + append are serialized here; the
        pump only ever sees the sink once it's in live.sinks). Sends ``ready``
        last. Used by single-principal Slack cross-surface attach and (5b)
        co-drive join."""
        live = self._live.get(chat_id)
        if live is None or live.state == SessionState.DEAD:
            raise SessionNotFound(chat_id)
        for msg in self._repo.list_messages(chat_id):
            await sink.send_json({
                "type": "assistant_message" if msg.role == "assistant" else "user_msg",
                "content": msg.content,
                "sender_email": msg.sender_email,
            })
        live.sinks.append(SinkEntry(participant_email=participant_email, sink=sink))
        await sink.send_json({"type": "ready"})

    async def _spawn_runner(self, session: ChatSession, session_dir: Path):
        from app.auth.access import mint_session_jwt
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
                os.environ.get("SERVER_URL")
                or os.environ.get("AGNES_INTERNAL_URL")
                or "http://127.0.0.1:8000"
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
                        "agnes wheel upload failed; `agnes` CLI will be absent "
                        "in sandbox for session %s", session.id,
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
            if frame.get("type") == "assistant_message":
                self._repo.append_message(
                    session_id=live.chat_id,
                    role="assistant",
                    content=frame.get("content", ""),
                    tool_calls=frame.get("tool_calls"),
                    tokens_in=frame.get("tokens_in"),
                    tokens_out=frame.get("tokens_out"),
                    model=frame.get("model"),
                )
                # Auto-title: the first assistant_message in a session
                # is the trigger to ask Haiku for a short title. We
                # check the per-session flag (not just the persisted
                # title) so two rapid-fire assistant frames during
                # crash-respawn replay don't both fire the call.
                if not live.auto_title_started:
                    self._maybe_start_auto_title(live)
            elif frame.get("type") == "tool_call":
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
            if rc == 0 or live.state == SessionState.DEAD:
                return
            # Crash path
            live.crash_count += 1
            await self._broadcast(live, {
                "type": "error",
                "kind": "subprocess_crashed",
                "auto_respawn": live.crash_count < 3,
            })
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
            # Replay last 3 user turns into the new subprocess
            history = self._repo.list_messages(live.chat_id)[-3:]
            for msg in history:
                if msg.role == "user":
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
        if live is None or live.handle is None or live.state == SessionState.DEAD:
            raise SessionNotFound(chat_id)
        # Enforce daily Anthropic spend cap (result is TTL-cached — see _cached_daily_tokens)
        tokens_in, tokens_out = self._cached_daily_tokens(live.user_email)
        spent_usd = (
            tokens_in * _PRICE_IN_PER_MTOK / 1_000_000
            + tokens_out * _PRICE_OUT_PER_MTOK / 1_000_000
        )
        if spent_usd >= self._config.daily_anthropic_spend_usd:
            await self._broadcast(live, {
                "type": "error",
                "kind": "daily_budget",
                "message": (
                    f"Daily spend cap of ${self._config.daily_anthropic_spend_usd:.2f} reached. "
                    "Try again tomorrow."
                ),
            })
            raise RuntimeError("daily_budget_exhausted")
        # Per-session token cap — operators set max_session_tokens in
        # instance.yaml; previously the knob was dead config. Tokens already
        # spent in this session are summed from chat_messages on every send;
        # the session row itself is never UPDATEd (DuckDB 1.5.3 FK+index bug
        # documented in persistence.py).
        session_tokens = self._repo.session_total_tokens(chat_id)
        if session_tokens >= self._config.max_session_tokens:
            await self._broadcast(live, {
                "type": "error",
                "kind": "max_session_tokens",
                "message": (
                    f"Per-session token cap of {self._config.max_session_tokens} reached "
                    f"(used {session_tokens}). Start a new chat session."
                ),
            })
            raise RuntimeError("max_session_tokens_exhausted")
        # Per-user sliding-window message-rate cap. Trim entries older than
        # one hour, then check the count.  Anti-abuse knob; previously dead
        # config in instance.yaml.
        import time as _time
        now_mono = _time.monotonic()
        window = self._user_msg_window.setdefault(
            live.user_email, self._deque_cls(),
        )
        while window and (now_mono - window[0]) > 3600:
            window.popleft()
        if len(window) >= self._config.rate_messages_per_hour:
            await self._broadcast(live, {
                "type": "error",
                "kind": "rate_limit",
                "message": (
                    f"Rate limit hit: {self._config.rate_messages_per_hour} messages/hour. "
                    "Slow down or wait an hour."
                ),
            })
            raise RuntimeError("rate_limit_exceeded")
        window.append(now_mono)
        self._repo.append_message(
            session_id=chat_id, role="user", content=text,
            sender_email=sender_email or live.user_email,
        )
        payload = json.dumps({"type": "user_msg", "text": text}) + "\n"
        async with live._stdin_lock:
            live.handle.stdin.write(payload.encode("utf-8"))
            await live.handle.stdin.drain()
        live.last_activity = datetime.now(timezone.utc)
        live.state = SessionState.ACTIVE

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
            session_id=chat_id, role="assistant",
            content="",
            tool_calls=[{"cancelled": True}],
        )
        await self._broadcast(live, {"type": "cancelled"})

    async def kill(self, chat_id: str, *, reason: str) -> None:
        live = self._live.pop(chat_id, None)
        if live is None:
            return
        live.state = SessionState.DEAD
        if live.handle is not None:
            await live.handle.kill()
        for t in live.tasks:
            t.cancel()
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
                await self._broadcast(live, {
                    "type": "session_renamed",
                    "chat_id": live.chat_id,
                    "title": title,
                })
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

        Kills sessions that have either:
        - been idle longer than ``idle_ttl_seconds`` (reason ``idle_ttl``), or
        - been running longer than ``max_session_seconds`` (reason
          ``max_session_seconds``), regardless of recent activity.

        The wallclock cap was previously dead config (knob in instance.yaml
        nobody read). Operators setting it now actually get the behavior.
        """
        idle_cutoff = self._config.idle_ttl_seconds
        max_wallclock = self._config.max_session_seconds
        now = datetime.now(timezone.utc)
        to_kill: list[tuple[str, str]] = []
        for chat_id, live in list(self._live.items()):
            if (now - live.last_activity).total_seconds() > idle_cutoff:
                to_kill.append((chat_id, "idle_ttl"))
            elif (now - live.started_at).total_seconds() > max_wallclock:
                to_kill.append((chat_id, "max_session_seconds"))
        for chat_id, reason in to_kill:
            await self.kill(chat_id, reason=reason)
