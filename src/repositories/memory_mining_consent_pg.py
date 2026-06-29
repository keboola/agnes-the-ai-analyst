"""Postgres-backed repository for ``memory_mining_consent`` (v78).

Mirrors ``src/repositories/memory_mining_consent.py`` (the DuckDB impl) on the
``MemoryMiningConsentRepository`` public surface. Parity is covered by
``tests/db_pg/test_memory_mining_consent_contract.py``. Opt-in state =
``opted_in_at IS NOT NULL``; opting out clears it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class MemoryMiningConsentPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def set_consent(self, user_email: str, *, opted_in: bool) -> None:
        if opted_in:
            sql = (
                "INSERT INTO memory_mining_consent "
                "(user_email, opted_in_at, opted_out_at, updated_at) "
                "VALUES (:e, CURRENT_TIMESTAMP, NULL, CURRENT_TIMESTAMP) "
                "ON CONFLICT (user_email) DO UPDATE SET "
                "opted_in_at = CURRENT_TIMESTAMP, opted_out_at = NULL, "
                "updated_at = CURRENT_TIMESTAMP"
            )
        else:
            sql = (
                "INSERT INTO memory_mining_consent "
                "(user_email, opted_in_at, opted_out_at, updated_at) "
                "VALUES (:e, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
                "ON CONFLICT (user_email) DO UPDATE SET "
                "opted_in_at = NULL, opted_out_at = CURRENT_TIMESTAMP, "
                "updated_at = CURRENT_TIMESTAMP"
            )
        with self._engine.begin() as conn:
            conn.execute(sa.text(sql), {"e": user_email})

    def is_opted_in(self, user_email: str) -> bool:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT opted_in_at FROM memory_mining_consent WHERE user_email = :e"),
                {"e": user_email},
            ).fetchone()
        return row is not None and row[0] is not None

    def get(self, user_email: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT user_email, opted_in_at, opted_out_at, updated_at "
                    "FROM memory_mining_consent WHERE user_email = :e"
                ),
                {"e": user_email},
            ).fetchone()
        if row is None:
            return None
        return {
            "user_email": row[0],
            "opted_in_at": row[1],
            "opted_out_at": row[2],
            "updated_at": row[3],
            "opted_in": row[1] is not None,
        }

    def list_opted_in(self) -> List[str]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT user_email FROM memory_mining_consent WHERE opted_in_at IS NOT NULL")
            ).fetchall()
        return [r[0] for r in rows]
