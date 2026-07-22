"""Postgres-backed repository for ``llm_usage`` (v96).

Mirrors ``src/repositories/llm_usage.py`` (the DuckDB impl) on the
``LlmUsageRepository`` public surface. Cross-engine parity is covered by
``tests/db_pg/test_llm_usage_contract.py``.
"""

from __future__ import annotations

from typing import Any, Dict, List

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class LlmUsagePgRepository:
    """Postgres twin of ``LlmUsageRepository``."""

    def __init__(self, engine: Engine):
        self._engine = engine

    def insert_batch(self, rows: List[Dict[str, Any]]) -> None:
        if not rows:
            return
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO llm_usage
                      (id, agent_id, user_id, session_id, model,
                       input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens)
                    VALUES
                      (:id, :agent_id, :user_id, :session_id, :model,
                       :input_tokens, :output_tokens, :cache_read_tokens, :cache_creation_tokens)
                    """
                ),
                [
                    {
                        "id": row["id"],
                        "agent_id": row.get("agent_id"),
                        "user_id": row.get("user_id"),
                        "session_id": row.get("session_id"),
                        "model": row.get("model"),
                        "input_tokens": row.get("input_tokens", 0),
                        "output_tokens": row.get("output_tokens", 0),
                        "cache_read_tokens": row.get("cache_read_tokens", 0),
                        "cache_creation_tokens": row.get("cache_creation_tokens", 0),
                    }
                    for row in rows
                ],
            )

    def month_total_tokens(self, agent_id: str, year_month: str) -> int:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    """
                    SELECT COALESCE(SUM(input_tokens + output_tokens + cache_creation_tokens), 0)
                    FROM llm_usage
                    WHERE agent_id = :agent_id AND to_char(created_at, 'YYYY-MM') = :ym
                    """
                ),
                {"agent_id": agent_id, "ym": year_month},
            ).first()
        return int(row[0]) if row else 0

    def list_for_agent(self, agent_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        """
                        SELECT * FROM llm_usage
                        WHERE agent_id = :agent_id
                        ORDER BY created_at DESC
                        LIMIT :limit
                        """
                    ),
                    {"agent_id": agent_id, "limit": limit},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]
