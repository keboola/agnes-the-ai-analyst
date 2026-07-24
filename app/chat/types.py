"""Chat-feature shared dataclasses and enums (referenced cross-module)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

# Relay protocol version the in-sandbox loopback relay speaks (chat sandbox
# secret broker, ticket_push stdin contract — see app.chat.manager). Bumped
# whenever that stdin frame contract changes. Persisted on
# ``ChatSession.relay_protocol_version`` (written by
# ``ChatRepository.set_sandbox_ref`` / ``_push_ticket_frame``) so
# ChatManager's resume-vs-respawn decision (``_resume_from_row`` /
# ``_resume_live``) survives a process restart: NULL/absent or a value below
# this constant means the session's last-known runner is unknown/legacy
# (pre-migration row, or refs never reconnected), so resume is refused and a
# fresh spawn is forced instead — that spawn always starts a current-protocol
# runner and pushes its own ticket. Lives here (not app.chat.manager) so
# lower-level modules (persistence, the PG repo) can reference it without an
# import cycle back into manager.py.
RELAY_PROTOCOL_VERSION = 1


class Surface(str, Enum):
    WEB = "web"
    SLACK_DM = "slack_dm"
    SLACK_THREAD = "slack_thread"


class SessionState(str, Enum):
    NEW = "NEW"
    ACTIVE = "ACTIVE"
    IDLE = "IDLE"
    PAUSED = "PAUSED"
    DEAD = "DEAD"


@dataclass
class ChatSession:
    id: str
    user_email: str
    surface: Surface
    slack_channel_id: Optional[str]
    slack_thread_ts: Optional[str]
    title: Optional[str]
    started_at: datetime
    last_message_at: Optional[datetime]
    message_count: int
    archived: bool
    is_co_session: bool = False
    ephemeral: bool = False
    # Sandbox lifecycle refs (pause/resume). Nullable; cleared on real kill.
    # NOTE: never index these columns — DuckDB 1.5.3 FK+index bug (src/db.py).
    sandbox_id: Optional[str] = None
    runner_pid: Optional[int] = None
    sandbox_paused_at: Optional[datetime] = None
    # Relay protocol version of the runner these sandbox refs point at (Tier
    # 1 restart-invariant reuse). NULL means unknown/legacy — see
    # RELAY_PROTOCOL_VERSION's docstring above.
    relay_protocol_version: Optional[int] = None


@dataclass
class ChatMessage:
    id: str
    session_id: str
    role: str
    content: str
    tool_calls: Optional[list[dict]]
    tokens_in: Optional[int]
    tokens_out: Optional[int]
    model: Optional[str]
    created_at: datetime
    sender_email: Optional[str] = None


@dataclass
class SessionParticipant:
    id: str
    session_id: str
    user_email: str
    user_id: str
    role: str  # 'owner' | 'collaborator'
    joined_at: Optional[datetime]
    left_at: Optional[datetime]  # None = active


@dataclass
class UserWorkdir:
    user_email: str
    last_init_at: Optional[datetime]
    marketplace_sha: Optional[str]
    initial_workspace_sha: Optional[str]
    agnes_version_at_init: Optional[str]
