"""Chat-feature shared dataclasses and enums (referenced cross-module)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class Surface(str, Enum):
    WEB = "web"
    SLACK_DM = "slack_dm"
    SLACK_THREAD = "slack_thread"


class SessionState(str, Enum):
    NEW = "NEW"
    ACTIVE = "ACTIVE"
    IDLE = "IDLE"
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


@dataclass
class UserWorkdir:
    user_email: str
    last_init_at: Optional[datetime]
    marketplace_sha: Optional[str]
    initial_workspace_sha: Optional[str]
    agnes_version_at_init: Optional[str]
