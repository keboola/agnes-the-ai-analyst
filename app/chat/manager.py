"""ChatManager: session state machine, lifecycle, WS attachment."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.chat.config import ChatConfig
from app.chat.persistence import ChatRepository
from app.chat.provider import SandboxHandle, SandboxProvider
from app.chat.types import ChatSession, SessionState, Surface
from app.chat.workdir import WorkdirManager

logger = logging.getLogger(__name__)


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

    # --- placeholders implemented in Task 5.2 -----------------------------

    async def attach(self, chat_id: str, ws) -> None:
        raise NotImplementedError("Task 5.2")

    async def send_user_message(self, chat_id: str, text: str) -> None:
        raise NotImplementedError("Task 5.2")

    async def cancel(self, chat_id: str) -> None:
        raise NotImplementedError("Task 5.2")

    async def kill(self, chat_id: str, *, reason: str) -> None:
        # Minimal impl so shutdown works.
        live = self._live.pop(chat_id, None)
        if live and live.handle is not None:
            await live.handle.kill()
        for t in (live.tasks if live else []):
            t.cancel()
