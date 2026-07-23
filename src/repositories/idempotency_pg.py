"""Postgres-backed repository for ``idempotency_keys`` (v96).

Mirrors ``src/repositories/idempotency.py`` (the DuckDB impl) on the
``IdempotencyRepository`` public surface. Cross-engine parity is covered by
``tests/db_pg/test_idempotency_contract.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class IdempotencyPgRepository:
    """Postgres twin of ``IdempotencyRepository``."""

    def __init__(self, engine: Engine):
        self._engine = engine

    def get(self, key: str, owner_user_id: str, agent_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text(
                        "SELECT * FROM idempotency_keys "
                        "WHERE key = :key AND owner_user_id = :owner_user_id AND agent_id = :agent_id"
                    ),
                    {"key": key, "owner_user_id": owner_user_id, "agent_id": agent_id},
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        record = dict(row)
        expires_at = record.get("expires_at")
        if expires_at is not None:
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
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=ttl_s)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO idempotency_keys
                      (key, owner_user_id, agent_id, request_hash, response_body, status_code, created_at, expires_at)
                    VALUES
                      (:key, :owner_user_id, :agent_id, :request_hash, :response_body, :status_code, :created_at, :expires_at)
                    ON CONFLICT (key, owner_user_id, agent_id) DO UPDATE SET
                      request_hash = EXCLUDED.request_hash,
                      response_body = EXCLUDED.response_body,
                      status_code = EXCLUDED.status_code,
                      created_at = EXCLUDED.created_at,
                      expires_at = EXCLUDED.expires_at
                    """
                ),
                {
                    "key": key,
                    "owner_user_id": owner_user_id,
                    "agent_id": agent_id,
                    "request_hash": request_hash,
                    "response_body": response_body,
                    "status_code": status_code,
                    "created_at": now,
                    "expires_at": expires_at,
                },
            )

    def purge_expired(self) -> int:
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.text(
                    "DELETE FROM idempotency_keys WHERE expires_at IS NOT NULL AND expires_at < :now RETURNING key"
                ),
                {"now": datetime.now(timezone.utc)},
            ).fetchall()
        return len(rows)
