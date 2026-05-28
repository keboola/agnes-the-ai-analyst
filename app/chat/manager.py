"""ChatManager: session state machine, lifecycle, WS attachment."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
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


class ConcurrencyCapHit(Exception):
    """Raised when a user already has the maximum allowed active sessions."""


class SessionNotFound(Exception):
    pass


@dataclass
class LiveSession:
    chat_id: str
    user_email: str
    state: SessionState
    handle: Optional[SandboxHandle]
    ws: object  # WebSocket; typed loosely to avoid FastAPI import cycle
    started_at: datetime
    last_activity: datetime
    crash_count: int = 0
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    tasks: list[asyncio.Task] = field(default_factory=list)
    # Latest pump-subprocess-to-ws task. Each crash respawn replaces this
    # (and removes the previous one from `tasks`) so the per-session task
    # list does not grow unboundedly across crashes.
    current_pump: Optional[asyncio.Task] = None


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
        return self._repo.create_session(
            user_email=user_email,
            surface=surface,
            slack_channel_id=slack_channel_id,
            slack_thread_ts=slack_thread_ts,
            title=title,
        )

    def _active_count_for_user(self, user_email: str) -> int:
        return sum(
            1
            for s in self._live.values()
            if s.user_email == user_email
            and s.state in (SessionState.NEW, SessionState.ACTIVE, SessionState.IDLE)
        )

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

    async def attach(self, chat_id: str, ws) -> None:
        session = self._repo.get_session(chat_id)
        if session is None:
            raise SessionNotFound(chat_id)

        self._workdir_mgr.ensure_user_workdir(session.user_email)
        session_dir = self._workdir_mgr.prepare_session_dir(session.user_email, chat_id)

        handle = await self._spawn_runner(session, session_dir)
        live = LiveSession(
            chat_id=chat_id,
            user_email=session.user_email,
            state=SessionState.ACTIVE,
            handle=handle,
            ws=ws,
            started_at=datetime.now(timezone.utc),
            last_activity=datetime.now(timezone.utc),
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
            "AGNES_API": os.environ.get("AGNES_INTERNAL_URL", "http://127.0.0.1:8000"),
            "AGNES_SESSION_ID": session.id,
            "AGNES_USER_EMAIL": session.user_email,
            "AGNES_DAILY_BUDGET_USD": str(self._config.daily_anthropic_spend_usd),
            "AGNES_PER_TOOL_CALL_SECONDS": str(self._config.per_tool_call_seconds),
            "PATH": "/usr/bin:/bin",
            "HOME": str(session_dir),
            "TERM": "dumb",
            "LANG": "C.UTF-8",
            "PYTHONUNBUFFERED": "1",
        }
        argv = [sys.executable, "-m", "app.chat.runner", "--session-id", session.id]
        return await self._provider.spawn(workdir=session_dir, env=env, argv=argv)

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
            try:
                await live.ws.send_json(frame)
            except Exception:
                logger.warning("ws send failed for %s", live.chat_id)
                return
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

    async def _wait_for_exit_and_respawn(self, live: LiveSession, session_dir: Path) -> None:
        while True:
            assert live.handle is not None
            rc = await live.handle.wait()
            if rc == 0 or live.state == SessionState.DEAD:
                return
            # Crash path
            live.crash_count += 1
            await live.ws.send_json({
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
            await live.ws.send_json({"type": "ready"})
            # Replay last 3 user turns into the new subprocess
            history = self._repo.list_messages(live.chat_id)[-3:]
            for msg in history:
                if msg.role == "user":
                    payload = json.dumps({"type": "user_msg", "text": msg.content}) + "\n"
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

    async def send_user_message(self, chat_id: str, text: str) -> None:
        live = self._live.get(chat_id)
        if live is None or live.handle is None or live.state == SessionState.DEAD:
            raise SessionNotFound(chat_id)
        # Enforce daily Anthropic spend cap
        tokens_in, tokens_out = self._repo.daily_anthropic_tokens(live.user_email)
        spent_usd = (
            tokens_in * _PRICE_IN_PER_MTOK / 1_000_000
            + tokens_out * _PRICE_OUT_PER_MTOK / 1_000_000
        )
        if spent_usd >= self._config.daily_anthropic_spend_usd:
            await live.ws.send_json({
                "type": "error",
                "kind": "daily_budget",
                "message": (
                    f"Daily spend cap of ${self._config.daily_anthropic_spend_usd:.2f} reached. "
                    "Try again tomorrow."
                ),
            })
            raise RuntimeError("daily_budget_exhausted")
        self._repo.append_message(session_id=chat_id, role="user", content=text)
        payload = json.dumps({"type": "user_msg", "text": text}) + "\n"
        live.handle.stdin.write(payload.encode("utf-8"))
        await live.handle.stdin.drain()
        live.last_activity = datetime.now(timezone.utc)
        live.state = SessionState.ACTIVE

    async def cancel(self, chat_id: str) -> None:
        live = self._live.get(chat_id)
        if live is None or live.handle is None:
            return
        payload = json.dumps({"type": "cancel"}) + "\n"
        live.handle.stdin.write(payload.encode("utf-8"))
        await live.handle.stdin.drain()
        await live.ws.send_json({"type": "cancelled"})

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

    # --- idle reaper --------------------------------------------------------

    def start_idle_reaper(self) -> None:
        if self._idle_task is None or self._idle_task.done():
            self._idle_task = asyncio.create_task(self._idle_reaper_loop())

    async def _idle_reaper_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            cutoff_age = self._config.idle_ttl_seconds
            now = datetime.now(timezone.utc)
            to_kill = [
                chat_id for chat_id, live in list(self._live.items())
                if (now - live.last_activity).total_seconds() > cutoff_age
            ]
            for chat_id in to_kill:
                await self.kill(chat_id, reason="idle_ttl")
