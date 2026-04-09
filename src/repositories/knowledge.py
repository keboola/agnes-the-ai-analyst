"""Repository for corporate memory knowledge items and votes."""

import json
from datetime import datetime, timezone
from typing import Any, Optional, List, Dict

import duckdb


class KnowledgeRepository:
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
        return [dict(zip(columns, row)) for row in rows]

    def get_by_id(self, item_id: str) -> Optional[Dict[str, Any]]:
        result = self.conn.execute("SELECT * FROM knowledge_items WHERE id = ?", [item_id]).fetchone()
        return self._row_to_dict(result)

    def create(
        self,
        id: str,
        title: str,
        content: str,
        category: str,
        source_user: Optional[str] = None,
        tags: Optional[List[str]] = None,
        status: str = "pending",
    ) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO knowledge_items (id, title, content, category, source_user,
                tags, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [id, title, content, category, source_user,
             json.dumps(tags) if tags else None, status, now, now],
        )

    def update(self, item_id: str, **fields) -> None:
        if not fields:
            return
        now = datetime.now(timezone.utc)
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [now, item_id]
        self.conn.execute(
            f"UPDATE knowledge_items SET {set_clause}, updated_at = ? WHERE id = ?",
            values,
        )

    def update_status(self, item_id: str, status: str) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            "UPDATE knowledge_items SET status = ?, updated_at = ? WHERE id = ?",
            [status, now, item_id],
        )

    def list_items(
        self,
        statuses: Optional[List[str]] = None,
        category: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM knowledge_items WHERE 1=1"
        params: List[Any] = []
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            query += f" AND status IN ({placeholders})"
            params.extend(statuses)
        if category:
            query += " AND category = ?"
            params.append(category)
        query += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return self._rows_to_dicts(self.conn.execute(query, params).fetchall())

    def search(self, query: str) -> List[Dict[str, Any]]:
        pattern = f"%{query}%"
        results = self.conn.execute(
            """SELECT * FROM knowledge_items
            WHERE title ILIKE ? OR content ILIKE ?
            ORDER BY updated_at DESC""",
            [pattern, pattern],
        ).fetchall()
        return self._rows_to_dicts(results)

    def vote(self, item_id: str, user_id: str, vote: int) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO knowledge_votes (item_id, user_id, vote, voted_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (item_id, user_id) DO UPDATE SET vote = excluded.vote, voted_at = excluded.voted_at""",
            [item_id, user_id, vote, now],
        )

    def get_votes(self, item_id: str) -> Dict[str, int]:
        result = self.conn.execute(
            """SELECT
                COALESCE(SUM(CASE WHEN vote > 0 THEN 1 ELSE 0 END), 0) as upvotes,
                COALESCE(SUM(CASE WHEN vote < 0 THEN 1 ELSE 0 END), 0) as downvotes
            FROM knowledge_votes WHERE item_id = ?""",
            [item_id],
        ).fetchone()
        return {"upvotes": result[0], "downvotes": result[1]}
