"""Repository for ``memory_mining_consent`` (v78).

Per-user opt-IN to having their session transcripts mined into shared corporate
memory (privacy gate, design spec §4.4). The miner reads ``list_opted_in()`` and
only mines those authors' transcripts.

Opt-in state is determined solely by ``opted_in_at IS NOT NULL`` — opting out
clears it. No timestamp comparison (``now()`` is transaction-scoped
on DuckDB, so two calls in one txn can be equal and an ``a < b`` test is
ambiguous). ``opted_out_at`` is retained for audit.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import duckdb


class MemoryMiningConsentRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    def set_consent(self, user_email: str, *, opted_in: bool) -> None:
        if opted_in:
            self.conn.execute(
                "INSERT INTO memory_mining_consent "
                "(user_email, opted_in_at, opted_out_at, updated_at) "
                "VALUES (?, now(), NULL, now()) "
                "ON CONFLICT (user_email) DO UPDATE SET "
                "opted_in_at = now(), opted_out_at = NULL, "
                "updated_at = now()",
                [user_email],
            )
        else:
            self.conn.execute(
                "INSERT INTO memory_mining_consent "
                "(user_email, opted_in_at, opted_out_at, updated_at) "
                "VALUES (?, NULL, now(), now()) "
                "ON CONFLICT (user_email) DO UPDATE SET "
                "opted_in_at = NULL, opted_out_at = now(), "
                "updated_at = now()",
                [user_email],
            )

    def is_opted_in(self, user_email: str) -> bool:
        row = self.conn.execute(
            "SELECT opted_in_at FROM memory_mining_consent WHERE user_email = ?",
            [user_email],
        ).fetchone()
        return row is not None and row[0] is not None

    def get(self, user_email: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT user_email, opted_in_at, opted_out_at, updated_at FROM memory_mining_consent WHERE user_email = ?",
            [user_email],
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
        rows = self.conn.execute(
            "SELECT user_email FROM memory_mining_consent WHERE opted_in_at IS NOT NULL"
        ).fetchall()
        return [r[0] for r in rows]
