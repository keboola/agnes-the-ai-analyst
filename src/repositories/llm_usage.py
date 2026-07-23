"""Repository for the ``llm_usage`` per-call token accounting ledger (v96).

The broker (Task 8) writes batches of usage rows here; the API (Task 9) reads
month-to-date totals and recent rows for a given agent.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import duckdb


class LlmUsageRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def _row_to_dict(self, row) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, row))

    def _rows_to_dicts(self, rows) -> List[Dict[str, Any]]:
        if not rows:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, r)) for r in rows]

    def insert_batch(self, rows: List[Dict[str, Any]]) -> None:
        if not rows:
            return
        self.conn.executemany(
            """INSERT INTO llm_usage
            (id, agent_id, user_id, session_id, model,
             input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                [
                    row["id"],
                    row.get("agent_id"),
                    row.get("user_id"),
                    row.get("session_id"),
                    row.get("model"),
                    row.get("input_tokens", 0),
                    row.get("output_tokens", 0),
                    row.get("cache_read_tokens", 0),
                    row.get("cache_creation_tokens", 0),
                ]
                for row in rows
            ],
        )

    def month_total_tokens(self, agent_id: str, year_month: str) -> int:
        row = self.conn.execute(
            """SELECT COALESCE(SUM(input_tokens + output_tokens + cache_creation_tokens), 0)
            FROM llm_usage
            WHERE agent_id = ? AND strftime(created_at, '%Y-%m') = ?""",
            [agent_id, year_month],
        ).fetchone()
        return int(row[0]) if row else 0

    def list_for_agent(self, agent_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT * FROM llm_usage
            WHERE agent_id = ?
            ORDER BY created_at DESC
            LIMIT ?""",
            [agent_id, limit],
        ).fetchall()
        return self._rows_to_dicts(rows)

    def list_for_session(self, session_id: str, limit: int = 1000) -> List[Dict[str, Any]]:
        """All `llm_usage` rows for one chat session, filtered in SQL (review
        carry-over, Task 9) — `app.chat.agent_usage.usage_for_session` used
        to call `list_for_agent()` and filter by `session_id` in Python over
        just that agent's most recent `limit` rows, which silently
        undercounts once an agent has more than `limit` rows total (a busy
        agent's OLDER session falls out of the scan window even though its
        own rows are still in the table). `session_id` values are globally
        unique (minted by `ChatManager.create_session`), so filtering by it
        alone in SQL is both exact and cheap — no `agent_id` needed."""
        rows = self.conn.execute(
            """SELECT * FROM llm_usage
            WHERE session_id = ?
            ORDER BY created_at DESC
            LIMIT ?""",
            [session_id, limit],
        ).fetchall()
        return self._rows_to_dicts(rows)
