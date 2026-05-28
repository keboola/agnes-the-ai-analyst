"""DuckDB CRUD for chat_sessions + chat_messages.

The two tables are simple — no soft-delete, no fancy indexing — so a
small repository module is sufficient. We don't put this under
``src/repositories/`` because it is only consumed by ``app/chat/`` and
``app/api/chat.py`` (the chat agent is self-contained); promoting it
later if anything else grows a dependency is a one-line move.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from typing import Any, Optional

import duckdb


_SESSION_ID_PREFIX = "chat_"
_MESSAGE_ID_PREFIX = "msg_"
_ID_HEX_BYTES = 6  # 12 hex chars


def _new_id(prefix: str) -> str:
    return f"{prefix}{secrets.token_hex(_ID_HEX_BYTES)}"


@dataclass
class ChatMessage:
    id: str
    session_id: str
    role: str  # 'user' | 'assistant' | 'tool_result'
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    model: Optional[str] = None
    created_at: Optional[str] = None


@dataclass
class ChatSession:
    id: str
    user_email: str
    title: Optional[str]
    started_at: Optional[str]
    last_message_at: Optional[str]
    message_count: int
    archived: bool


class ChatRepository:
    """Thin DuckDB-backed CRUD for chat sessions + messages."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    # ------------------------------ sessions

    def create_session(self, user_email: str, title: Optional[str] = None) -> ChatSession:
        sid = _new_id(_SESSION_ID_PREFIX)
        self.conn.execute(
            """INSERT INTO chat_sessions (id, user_email, title, message_count, archived)
               VALUES (?, ?, ?, 0, FALSE)""",
            [sid, user_email, title],
        )
        return self.get_session(sid)  # type: ignore[return-value]

    def get_session(self, session_id: str) -> Optional[ChatSession]:
        row = self.conn.execute(
            """SELECT id, user_email, title, started_at, last_message_at,
                      message_count, archived
                 FROM chat_sessions WHERE id = ?""",
            [session_id],
        ).fetchone()
        return _row_to_session(row) if row else None

    def list_sessions(
        self, user_email: str, include_archived: bool = False, limit: int = 200
    ) -> list[ChatSession]:
        where = ["user_email = ?"]
        params: list[Any] = [user_email]
        if not include_archived:
            where.append("archived = FALSE")
        sql = (
            "SELECT id, user_email, title, started_at, last_message_at, "
            "message_count, archived "
            "FROM chat_sessions "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY COALESCE(last_message_at, started_at) DESC "
            "LIMIT ?"
        )
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [_row_to_session(r) for r in rows]

    def archive_session(self, session_id: str) -> None:
        self.conn.execute(
            "UPDATE chat_sessions SET archived = TRUE WHERE id = ?",
            [session_id],
        )

    def set_title(self, session_id: str, title: str) -> None:
        self.conn.execute(
            "UPDATE chat_sessions SET title = ? WHERE id = ?",
            [title, session_id],
        )

    # ------------------------------ messages

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_calls: Optional[list[dict[str, Any]]] = None,
        tokens_in: Optional[int] = None,
        tokens_out: Optional[int] = None,
        model: Optional[str] = None,
    ) -> ChatMessage:
        mid = _new_id(_MESSAGE_ID_PREFIX)
        self.conn.execute(
            """INSERT INTO chat_messages
               (id, session_id, role, content, tool_calls,
                tokens_in, tokens_out, model)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                mid,
                session_id,
                role,
                content,
                json.dumps(tool_calls) if tool_calls else None,
                tokens_in,
                tokens_out,
                model,
            ],
        )
        # Bump session counters in the same transaction so list_sessions
        # ordering reflects the new message immediately.
        self.conn.execute(
            """UPDATE chat_sessions
                  SET message_count = message_count + 1,
                      last_message_at = current_timestamp
                WHERE id = ?""",
            [session_id],
        )
        return ChatMessage(
            id=mid,
            session_id=session_id,
            role=role,
            content=content,
            tool_calls=tool_calls or [],
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=model,
        )

    def list_messages(self, session_id: str) -> list[ChatMessage]:
        rows = self.conn.execute(
            """SELECT id, session_id, role, content, tool_calls,
                      tokens_in, tokens_out, model, created_at
                 FROM chat_messages
                WHERE session_id = ?
                ORDER BY created_at ASC, id ASC""",
            [session_id],
        ).fetchall()
        return [_row_to_message(r) for r in rows]


def _row_to_session(row: tuple) -> ChatSession:
    return ChatSession(
        id=row[0],
        user_email=row[1],
        title=row[2],
        started_at=row[3].isoformat() if row[3] else None,
        last_message_at=row[4].isoformat() if row[4] else None,
        message_count=row[5],
        archived=bool(row[6]),
    )


def _row_to_message(row: tuple) -> ChatMessage:
    tool_calls_json = row[4]
    tool_calls = json.loads(tool_calls_json) if tool_calls_json else []
    return ChatMessage(
        id=row[0],
        session_id=row[1],
        role=row[2],
        content=row[3],
        tool_calls=tool_calls,
        tokens_in=row[5],
        tokens_out=row[6],
        model=row[7],
        created_at=row[8].isoformat() if row[8] else None,
    )
