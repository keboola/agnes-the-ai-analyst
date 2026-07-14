"""DuckDB-backed repository for ``chat_broker_tickets`` (v90).

Opaque, short-lived tickets minted for the chat sandbox secret broker
(2026-07-14 incident hardening): a sandbox-local relay holds a ticket in
memory only and presents it to the broker routes instead of a real
credential. ``mint`` returns an opaque ``secrets.token_urlsafe(32)`` value;
``resolve`` rejects unknown or expired tokens; ``revoke``/``revoke_session``
invalidate tickets early (e.g. on session resume, once fresh tickets have
been pushed).

Template: ``src/repositories/setup_tokens.py``.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import duckdb


class TicketRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    def mint(self, session_id: str, scope: str, ttl_seconds: int = 3600) -> str:
        """Insert a new ticket row keyed by an opaque token. Returns the token."""
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=ttl_seconds)
        self.conn.execute(
            """INSERT INTO chat_broker_tickets
               (token, session_id, scope, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            [token, session_id, scope, expires_at, now],
        )
        return token

    def resolve(self, token: str) -> Optional[Dict[str, Any]]:
        """Return ``{"session_id", "scope", "expires_at"}`` if ``token`` exists
        and is not expired, else ``None``."""
        now = datetime.now(timezone.utc)
        row = self.conn.execute(
            """SELECT session_id, scope, expires_at FROM chat_broker_tickets
               WHERE token = ? AND expires_at > ?""",
            [token, now],
        ).fetchone()
        if not row:
            return None
        return {"session_id": row[0], "scope": row[1], "expires_at": row[2]}

    def revoke(self, token: str) -> None:
        self.conn.execute("DELETE FROM chat_broker_tickets WHERE token = ?", [token])

    def revoke_session(self, session_id: str) -> None:
        self.conn.execute("DELETE FROM chat_broker_tickets WHERE session_id = ?", [session_id])
