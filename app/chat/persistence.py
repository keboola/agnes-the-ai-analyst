"""Chat persistence — sessions, messages, and per-user workdir markers.

DuckDB 1.5.3 limitation: after inserting a child row into chat_messages
(which has a REFERENCES chat_sessions(id) FK) the secondary index
idx_chat_messages_session causes a false FK violation when any subsequent
UPDATE on the parent chat_sessions row is attempted within the same
connection.  Workaround: do NOT maintain denormalised message_count /
last_message_at counters via UPDATE.  Instead, compute them at read time
from chat_messages via a LEFT JOIN.  append_message() therefore inserts
only into chat_messages; get_session() / list_sessions() use a join query
that derives both values on the fly.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Optional

import duckdb

from app.chat.types import ChatMessage, ChatSession, Surface, UserWorkdir


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def _row_to_session(row: tuple) -> ChatSession:
    # Row order matches _SESSION_COLS_JOIN below:
    # id, user_email, surface, slack_channel_id, slack_thread_ts, title,
    # started_at, last_message_at (derived), message_count (derived), archived
    return ChatSession(
        id=row[0],
        user_email=row[1],
        surface=Surface(row[2]),
        slack_channel_id=row[3],
        slack_thread_ts=row[4],
        title=row[5],
        started_at=row[6],
        last_message_at=row[7],
        message_count=int(row[8]) if row[8] is not None else 0,
        archived=bool(row[9]),
    )


# Session columns derived via LEFT JOIN to compute live stats.
# DuckDB 1.5.3 FK+index bug prevents UPDATE on chat_sessions after any
# INSERT into chat_messages — so message_count / last_message_at are never
# stored; they are always computed from chat_messages at read time.
_SESSION_SELECT = (
    "SELECT s.id, s.user_email, s.surface, s.slack_channel_id, s.slack_thread_ts, "
    "s.title, s.started_at, "
    "MAX(m.created_at) AS last_message_at, "
    "COUNT(m.id) AS message_count, "
    "s.archived "
    "FROM chat_sessions s "
    "LEFT JOIN chat_messages m ON m.session_id = s.id"
)
_SESSION_GROUP = (
    " GROUP BY s.id, s.user_email, s.surface, s.slack_channel_id, s.slack_thread_ts, "
    "s.title, s.started_at, s.archived"
)


class ChatRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn

    # --- sessions ----------------------------------------------------------

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
        self._conn.execute(
            "INSERT INTO chat_sessions "
            "(id, user_email, surface, slack_channel_id, slack_thread_ts, title, "
            "started_at, last_message_at, message_count, archived) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0, FALSE)",
            [chat_id, user_email, surface.value, slack_channel_id, slack_thread_ts, title, now],
        )
        fetched = self.get_session(chat_id)
        assert fetched is not None
        return fetched

    def get_session(self, chat_id: str) -> Optional[ChatSession]:
        row = self._conn.execute(
            _SESSION_SELECT + " WHERE s.id = ?" + _SESSION_GROUP,
            [chat_id],
        ).fetchone()
        return _row_to_session(row) if row else None

    def list_sessions(self, user_email: str, *, include_archived: bool = False) -> list[ChatSession]:
        where = " WHERE s.user_email = ?"
        if not include_archived:
            where += " AND s.archived = FALSE"
        q = (
            _SESSION_SELECT + where + _SESSION_GROUP
            + " ORDER BY MAX(m.created_at) DESC NULLS LAST, s.started_at DESC"
        )
        rows = self._conn.execute(q, [user_email]).fetchall()
        return [_row_to_session(r) for r in rows]

    def get_slack_dm_session(self, slack_channel_id: str) -> Optional[ChatSession]:
        # intentional: no await between SELECT and INSERT — Slack uniqueness without DB partial unique index
        row = self._conn.execute(
            _SESSION_SELECT
            + " WHERE s.surface = 'slack_dm' AND s.slack_channel_id = ? AND s.archived = FALSE"
            + _SESSION_GROUP,
            [slack_channel_id],
        ).fetchone()
        return _row_to_session(row) if row else None

    def get_slack_thread_session(
        self, slack_channel_id: str, slack_thread_ts: str,
    ) -> Optional[ChatSession]:
        # intentional: no await between SELECT and INSERT — Slack uniqueness without DB partial unique index
        row = self._conn.execute(
            _SESSION_SELECT
            + " WHERE s.surface = 'slack_thread'"
            + " AND s.slack_channel_id = ? AND s.slack_thread_ts = ? AND s.archived = FALSE"
            + _SESSION_GROUP,
            [slack_channel_id, slack_thread_ts],
        ).fetchone()
        return _row_to_session(row) if row else None

    def archive_session(self, chat_id: str) -> None:
        self._conn.execute(
            "UPDATE chat_sessions SET archived = TRUE WHERE id = ?", [chat_id]
        )

    def hard_delete_user_sessions(self, user_email: str) -> int:
        n = self._conn.execute(
            "SELECT COUNT(*) FROM chat_sessions WHERE user_email = ?", [user_email]
        ).fetchone()[0]
        # FK on chat_messages.session_id blocks parent delete while
        # children exist (DuckDB has no ON DELETE CASCADE — Task 1.1
        # documented this). Delete messages first.
        self._conn.execute(
            "DELETE FROM chat_messages WHERE session_id IN ("
            " SELECT id FROM chat_sessions WHERE user_email = ?)",
            [user_email],
        )
        self._conn.execute("DELETE FROM chat_sessions WHERE user_email = ?", [user_email])
        return n

    # --- messages ----------------------------------------------------------

    def append_message(
        self,
        *,
        session_id: str,
        role: str,
        content: str,
        tool_calls: Optional[list[dict]] = None,
        tokens_in: Optional[int] = None,
        tokens_out: Optional[int] = None,
        model: Optional[str] = None,
    ) -> ChatMessage:
        msg_id = _gen_id("msg")
        now = datetime.now(timezone.utc)
        # Only insert into chat_messages — do NOT update chat_sessions.
        # DuckDB 1.5.3 has an FK+secondary-index bug that blocks any UPDATE on
        # chat_sessions once a child message row exists under the session.
        # message_count and last_message_at are computed at read time via JOIN.
        self._conn.execute(
            "INSERT INTO chat_messages "
            "(id, session_id, role, content, tool_calls, tokens_in, tokens_out, model, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [msg_id, session_id, role, content,
             json.dumps(tool_calls) if tool_calls else None,
             tokens_in, tokens_out, model, now],
        )
        return ChatMessage(
            id=msg_id, session_id=session_id, role=role, content=content,
            tool_calls=tool_calls, tokens_in=tokens_in, tokens_out=tokens_out,
            model=model, created_at=now,
        )

    def list_messages(
        self, session_id: str, *, after_id: Optional[str] = None, limit: int = 500,
    ) -> list[ChatMessage]:
        if after_id:
            row = self._conn.execute(
                "SELECT created_at FROM chat_messages WHERE id = ?", [after_id]
            ).fetchone()
            cutoff = row[0] if row else None
        else:
            cutoff = None

        q = (
            "SELECT id, session_id, role, content, tool_calls, tokens_in, tokens_out, "
            "model, created_at FROM chat_messages WHERE session_id = ?"
        )
        params: list = [session_id]
        if cutoff is not None:
            q += " AND created_at > ?"
            params.append(cutoff)
        q += " ORDER BY created_at ASC LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(q, params).fetchall()
        return [
            ChatMessage(
                id=r[0], session_id=r[1], role=r[2], content=r[3],
                tool_calls=json.loads(r[4]) if r[4] else None,
                tokens_in=r[5], tokens_out=r[6], model=r[7], created_at=r[8],
            )
            for r in rows
        ]

    # --- workdirs ----------------------------------------------------------

    def get_workdir(self, user_email: str) -> Optional[UserWorkdir]:
        row = self._conn.execute(
            "SELECT user_email, last_init_at, marketplace_sha, initial_workspace_sha, "
            "agnes_version_at_init FROM user_workdirs WHERE user_email = ?",
            [user_email],
        ).fetchone()
        if not row:
            return None
        return UserWorkdir(
            user_email=row[0], last_init_at=row[1], marketplace_sha=row[2],
            initial_workspace_sha=row[3], agnes_version_at_init=row[4],
        )

    def upsert_workdir(
        self,
        *,
        user_email: str,
        marketplace_sha: Optional[str],
        initial_workspace_sha: Optional[str],
        agnes_version: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        self._conn.execute(
            "INSERT OR REPLACE INTO user_workdirs "
            "(user_email, last_init_at, marketplace_sha, initial_workspace_sha, agnes_version_at_init) "
            "VALUES (?, ?, ?, ?, ?)",
            [user_email, now, marketplace_sha, initial_workspace_sha, agnes_version],
        )

    def delete_workdir_row(self, user_email: str) -> None:
        self._conn.execute("DELETE FROM user_workdirs WHERE user_email = ?", [user_email])

    def daily_anthropic_tokens(self, user_email: str) -> tuple[int, int]:
        """Sum of tokens_in / tokens_out for this user's messages since UTC midnight."""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(m.tokens_in), 0), COALESCE(SUM(m.tokens_out), 0) "
            "FROM chat_messages m JOIN chat_sessions s ON m.session_id = s.id "
            "WHERE s.user_email = ? AND DATE_TRUNC('day', m.created_at) = DATE_TRUNC('day', CURRENT_TIMESTAMP)",
            [user_email],
        ).fetchone()
        return int(row[0] or 0), int(row[1] or 0)
