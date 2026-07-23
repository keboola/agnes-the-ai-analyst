"""Repository for the ``idempotency_keys`` table (v96) — replay storage for
the agent-as-API surface (`POST /api/v1/agents/{slug}/responses`, Task 9).

A key is scoped to `(key, owner_user_id, agent_id)` — the same idempotency
key string can be reused by a different owner or against a different agent
without colliding. Each stored row carries the sha256 of the raw request
body (`request_hash`) alongside the previously-computed response, so a
replay with an IDENTICAL body can be served straight from the row while a
replay with a DIFFERENT body under the same key is detectable as key reuse
(the caller maps that to `409 idempotency_key_reuse`, not handled here).

``get`` never returns an expired row — TTL enforcement lives here, not at
the call site, so every caller gets it for free.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import duckdb


class IdempotencyRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def _row_to_dict(self, row) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, row))

    def get(self, key: str, owner_user_id: str, agent_id: str) -> Optional[Dict[str, Any]]:
        """Return the stored row for `(key, owner_user_id, agent_id)`, or
        ``None`` if absent OR expired (an expired row is treated exactly
        like a miss — callers re-run the request and `put()` overwrites
        it)."""
        row = self.conn.execute(
            """SELECT * FROM idempotency_keys
            WHERE key = ? AND owner_user_id = ? AND agent_id = ?""",
            [key, owner_user_id, agent_id],
        ).fetchone()
        record = self._row_to_dict(row)
        if record is None:
            return None
        expires_at = record.get("expires_at")
        if expires_at is not None:
            if isinstance(expires_at, str):
                expires_at = datetime.fromisoformat(expires_at)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > expires_at:
                return None
        return record

    def put(
        self,
        key: str,
        owner_user_id: str,
        agent_id: str,
        request_hash: str,
        response_body: str,
        status_code: int,
        ttl_s: int,
    ) -> None:
        """Insert-or-replace the row for `(key, owner_user_id, agent_id)`.

        DuckDB's composite PRIMARY KEY on `(key, owner_user_id, agent_id)`
        (see `src/db.py`'s `idempotency_keys` DDL) makes a plain re-INSERT
        of the same triple raise a constraint violation — DELETE-then-INSERT
        under one call keeps `put()` idempotent itself, matching the ON
        CONFLICT DO UPDATE the Postgres sibling uses.
        """
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=ttl_s)
        self.conn.execute(
            "DELETE FROM idempotency_keys WHERE key = ? AND owner_user_id = ? AND agent_id = ?",
            [key, owner_user_id, agent_id],
        )
        self.conn.execute(
            """INSERT INTO idempotency_keys
            (key, owner_user_id, agent_id, request_hash, response_body, status_code, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [key, owner_user_id, agent_id, request_hash, response_body, status_code, now, expires_at],
        )

    def purge_expired(self) -> int:
        """Delete every row whose `expires_at` is in the past. Returns the
        number of rows removed — callable from a maintenance job or a test
        assertion."""
        rows = self.conn.execute(
            "DELETE FROM idempotency_keys WHERE expires_at IS NOT NULL AND expires_at < ? RETURNING key",
            [datetime.now(timezone.utc)],
        ).fetchall()
        return len(rows)
