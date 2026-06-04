"""Postgres-backed chat-session repository.

Mirrors the ``chat_sessions`` operations of
``app/chat/persistence.py::ChatRepository``. Public surface returns the same
``app.chat.types`` dataclasses so ChatRepository can delegate transparently.

Unlike the DuckDB path, Postgres has no FK+index false-violation bug, so
``message_count`` / ``last_message_at`` are kept current here (maintained by
the chat-message repo on append) and read straight off the row rather than
re-derived via LEFT JOIN. Per-surface Slack uniqueness is enforced by the
partial unique indexes created in migration 0015 (not by application code).
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.chat.types import ChatSession, Surface


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def _row_to_session(row) -> ChatSession:
    return ChatSession(
        id=row["id"],
        user_email=row["user_email"],
        surface=Surface(row["surface"]),
        slack_channel_id=row["slack_channel_id"],
        slack_thread_ts=row["slack_thread_ts"],
        title=row["title"],
        started_at=row["started_at"],
        last_message_at=row["last_message_at"],
        message_count=int(row["message_count"]) if row["message_count"] is not None else 0,
        archived=bool(row["archived"]),
        is_co_session=bool(row["is_co_session"]),
        ephemeral=bool(row["ephemeral"]),
    )


class ChatSessionPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def create_session(
        self,
        *,
        user_email: str,
        surface: Surface,
        slack_channel_id: Optional[str] = None,
        slack_thread_ts: Optional[str] = None,
        title: Optional[str] = None,
    ) -> ChatSession:
        chat_id = _gen_id("chat")
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO chat_sessions "
                    "(id, user_email, surface, slack_channel_id, slack_thread_ts, "
                    "title, started_at, last_message_at, message_count, archived) "
                    "VALUES (:id, :user_email, :surface, :slack_channel_id, "
                    ":slack_thread_ts, :title, :started_at, NULL, 0, FALSE)"
                ),
                {
                    "id": chat_id,
                    "user_email": user_email,
                    "surface": surface.value,
                    "slack_channel_id": slack_channel_id,
                    "slack_thread_ts": slack_thread_ts,
                    "title": title,
                    "started_at": now,
                },
            )
        fetched = self.get_session(chat_id)
        assert fetched is not None
        return fetched

    def get_session(self, chat_id: str) -> Optional[ChatSession]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT * FROM chat_sessions WHERE id = :id"),
                {"id": chat_id},
            ).mappings().first()
        return _row_to_session(row) if row else None

    def list_sessions(
        self, user_email: str, *, include_archived: bool = False
    ) -> list[ChatSession]:
        sql = "SELECT * FROM chat_sessions WHERE user_email = :user_email"
        if not include_archived:
            sql += " AND archived = FALSE"
        sql += " ORDER BY last_message_at DESC NULLS LAST, started_at DESC"
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(sql), {"user_email": user_email}).mappings().all()
        return [_row_to_session(r) for r in rows]

    def get_slack_dm_session(self, slack_channel_id: str) -> Optional[ChatSession]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT * FROM chat_sessions "
                    "WHERE surface = 'slack_dm' AND slack_channel_id = :cid "
                    "AND archived = FALSE"
                ),
                {"cid": slack_channel_id},
            ).mappings().first()
        return _row_to_session(row) if row else None

    def get_slack_thread_session(
        self, slack_channel_id: str, slack_thread_ts: str
    ) -> Optional[ChatSession]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT * FROM chat_sessions "
                    "WHERE surface = 'slack_thread' AND slack_channel_id = :cid "
                    "AND slack_thread_ts = :ts AND archived = FALSE"
                ),
                {"cid": slack_channel_id, "ts": slack_thread_ts},
            ).mappings().first()
        return _row_to_session(row) if row else None

    def archive_session(self, chat_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE chat_sessions SET archived = TRUE WHERE id = :id"),
                {"id": chat_id},
            )

    def set_title(self, chat_id: str, title: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE chat_sessions SET title = :title WHERE id = :id"),
                {"title": title, "id": chat_id},
            )

    def archive_empty_user_sessions(
        self,
        user_email: str,
        *,
        surface: Optional[Surface] = None,
        exclude_id: Optional[str] = None,
    ) -> int:
        """Soft-archive every empty (zero-message) session owned by
        ``user_email``. Returns the number of rows archived.

        Empty = no rows in chat_messages. ``message_count`` is kept current
        on PG, but we filter on the actual child count via NOT EXISTS so the
        semantics match the DuckDB LEFT JOIN exactly.
        """
        params: dict = {"user_email": user_email}
        sql = (
            "UPDATE chat_sessions s SET archived = TRUE "
            "WHERE s.user_email = :user_email "
            "  AND s.archived = FALSE "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM chat_messages m WHERE m.session_id = s.id)"
        )
        if surface is not None:
            sql += " AND s.surface = :surface"
            params["surface"] = surface.value
        if exclude_id is not None:
            sql += " AND s.id != :exclude_id"
            params["exclude_id"] = exclude_id
        with self._engine.begin() as conn:
            result = conn.execute(sa.text(sql), params)
        return result.rowcount if result.rowcount is not None else 0

    def hard_delete_user_sessions(self, user_email: str) -> int:
        with self._engine.begin() as conn:
            n = conn.execute(
                sa.text("SELECT COUNT(*) FROM chat_sessions WHERE user_email = :ue"),
                {"ue": user_email},
            ).scalar() or 0
            # ON DELETE CASCADE removes child chat_messages automatically.
            conn.execute(
                sa.text("DELETE FROM chat_sessions WHERE user_email = :ue"),
                {"ue": user_email},
            )
        return int(n)
