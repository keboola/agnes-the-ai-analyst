"""Postgres-backed TicketRepository.

Mirrors ``src/repositories/ticket_repo.py``.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class TicketPgRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def mint(self, session_id: str, scope: str, ttl_seconds: int = 3600) -> str:
        """Insert a new ticket row keyed by an opaque token. Returns the token."""
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=ttl_seconds)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO chat_broker_tickets
                       (token, session_id, scope, expires_at, created_at)
                       VALUES (:token, :session_id, :scope, :expires_at, :now)"""
                ),
                {
                    "token": token,
                    "session_id": session_id,
                    "scope": scope,
                    "expires_at": expires_at,
                    "now": now,
                },
            )
        return token

    def resolve(self, token: str) -> Optional[Dict[str, Any]]:
        """Return ``{"session_id", "scope", "expires_at"}`` if ``token`` exists
        and is not expired, else ``None``."""
        now = datetime.now(timezone.utc)
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text(
                        """SELECT session_id, scope, expires_at FROM chat_broker_tickets
                       WHERE token = :token AND expires_at > :now"""
                    ),
                    {"token": token, "now": now},
                )
                .mappings()
                .first()
            )
        if not row:
            return None
        return {"session_id": row["session_id"], "scope": row["scope"], "expires_at": row["expires_at"]}

    def revoke(self, token: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM chat_broker_tickets WHERE token = :token"),
                {"token": token},
            )

    def revoke_session(self, session_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM chat_broker_tickets WHERE session_id = :session_id"),
                {"session_id": session_id},
            )
