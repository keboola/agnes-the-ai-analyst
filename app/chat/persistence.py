"""Chat persistence — sessions, messages, and per-user workdir markers.

DuckDB 1.5.3 bug: updating a column that is part of a secondary index
on the parent table triggers a false FK constraint violation if any
child rows exist. ``last_message_at`` is part of the
``idx_chat_sessions_user(user_email, last_message_at)`` index, so
``UPDATE chat_sessions SET last_message_at = …`` after any
``INSERT INTO chat_messages`` fails. ``message_count`` and ``archived``
are not indexed and can be UPDATEd safely. Workaround: compute both
``last_message_at`` and ``message_count`` at read time via LEFT JOIN.
When DuckDB ships the fix, this module's reads can be simplified
back to plain ``SELECT … FROM chat_sessions WHERE …``.
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

    def set_title(self, chat_id: str, title: str) -> None:
        """Persist a new title for a session. Safe to call after
        ``chat_messages`` rows exist — ``title`` is not part of any
        secondary index on ``chat_sessions``, so it's not subject to the
        DuckDB 1.5.3 FK+index bug that prevents UPDATE of
        ``last_message_at`` / ``message_count``.

        Used by the auto-title path (Haiku-generated title after the
        first assistant turn) and would also be the home for any future
        inline-rename UI."""
        self._conn.execute(
            "UPDATE chat_sessions SET title = ? WHERE id = ?", [title, chat_id]
        )

    def get_first_user_message(self, chat_id: str) -> Optional[str]:
        """First user-role message content in a session (oldest by
        ``created_at``), or ``None`` if the session has no user turns yet.

        Used as the auto-title prompt: the first user message captures
        the topic better than any later turn (which is usually a
        follow-up / refinement)."""
        row = self._conn.execute(
            "SELECT content FROM chat_messages "
            "WHERE session_id = ? AND role = 'user' "
            "ORDER BY created_at ASC LIMIT 1",
            [chat_id],
        ).fetchone()
        return row[0] if row else None

    def archive_empty_user_sessions(
        self,
        user_email: str,
        *,
        surface: Optional[Surface] = None,
        exclude_id: Optional[str] = None,
    ) -> int:
        """Soft-archive every empty (zero-message) session owned by
        ``user_email``. Returns the number of rows archived.

        Called from ``POST /api/chat/sessions`` so clicking "+ New
        chat" repeatedly never accumulates Untitled-chat orphans in
        the sidebar.

        ``surface`` scopes the GC — pass ``Surface.WEB`` so a web
        click doesn't also nuke the user's empty Slack DM/thread
        placeholders, which the manager intentionally keeps around
        keyed by channel/thread id for re-attach. ``None`` (default)
        archives across every surface.

        ``exclude_id`` lets the caller protect the session it just
        created — otherwise we'd race-archive the brand-new one.

        Implementation: a single UPDATE filtered by the same LEFT JOIN
        used in ``_SESSION_SELECT`` so we only touch sessions with
        zero messages. ``archived = FALSE`` in the WHERE keeps the
        count accurate (don't re-archive what's already archived).
        """
        params: list = [user_email]
        surface_clause = ""
        if surface is not None:
            surface_clause = " AND s.surface = ?"
        sql = (
            "UPDATE chat_sessions SET archived = TRUE "
            "WHERE user_email = ? "
            "  AND archived = FALSE "
            "  AND id IN ("
            "    SELECT s.id FROM chat_sessions s "
            "    LEFT JOIN chat_messages m ON m.session_id = s.id "
            "    WHERE s.user_email = ? AND s.archived = FALSE"
            f"{surface_clause}"
            "    GROUP BY s.id HAVING COUNT(m.id) = 0"
            "  )"
        )
        params.append(user_email)
        if surface is not None:
            params.append(surface.value)
        if exclude_id is not None:
            sql += " AND id != ?"
            params.append(exclude_id)
        before = self._conn.execute(
            "SELECT COUNT(*) FROM chat_sessions WHERE user_email = ? AND archived = FALSE",
            [user_email],
        ).fetchone()[0]
        self._conn.execute(sql, params)
        after = self._conn.execute(
            "SELECT COUNT(*) FROM chat_sessions WHERE user_email = ? AND archived = FALSE",
            [user_email],
        ).fetchone()[0]
        return before - after

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
        # DuckDB 1.5.3 bug: updating a column that is part of a secondary
        # index on the parent table triggers a false FK constraint violation
        # if any child rows exist. last_message_at is part of
        # idx_chat_sessions_user(user_email, last_message_at), so
        # UPDATE chat_sessions SET last_message_at = … after any
        # INSERT INTO chat_messages fails. Workaround: don't UPDATE at all
        # — compute last_message_at and message_count at read time via
        # LEFT JOIN (see _SESSION_SELECT). message_count and archived are
        # not indexed and could be UPDATEd safely, but we keep the same
        # read-time-derivation approach for both columns for consistency.
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

    def session_total_tokens(self, session_id: str) -> int:
        """Sum of (tokens_in + tokens_out) across every persisted message in
        this session.

        Used by ChatManager.send_user_message to enforce
        ChatConfig.max_session_tokens. A session row is a slow-changing
        rollup; counting at read time on every send_user_message is fine
        — DuckDB indexes on session_id and DuckDB is in-process.
        """
        row = self._conn.execute(
            "SELECT COALESCE(SUM(COALESCE(tokens_in, 0) + COALESCE(tokens_out, 0)), 0) "
            "FROM chat_messages WHERE session_id = ?",
            [session_id],
        ).fetchone()
        return int(row[0] or 0)

    def daily_anthropic_tokens(self, user_email: str) -> tuple[int, int]:
        """Sum of tokens_in / tokens_out for this user's messages since UTC midnight."""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(m.tokens_in), 0), COALESCE(SUM(m.tokens_out), 0) "
            "FROM chat_messages m JOIN chat_sessions s ON m.session_id = s.id "
            "WHERE s.user_email = ? AND DATE_TRUNC('day', m.created_at) = DATE_TRUNC('day', CURRENT_TIMESTAMP)",
            [user_email],
        ).fetchone()
        return int(row[0] or 0), int(row[1] or 0)
