"""Repository for corporate memory knowledge items, votes, and contradictions."""

import json
import uuid
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
        confidence: Optional[float] = None,
        domain: Optional[str] = None,
        entities: Optional[List[str]] = None,
        source_type: str = "claude_local_md",
        source_ref: Optional[str] = None,
        valid_from: Optional[datetime] = None,
        valid_until: Optional[datetime] = None,
        supersedes: Optional[str] = None,
        sensitivity: str = "internal",
        is_personal: bool = False,
    ) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO knowledge_items (
                id, title, content, category, source_user, tags, status,
                confidence, domain, entities, source_type, source_ref,
                valid_from, valid_until, supersedes, sensitivity, is_personal,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                id, title, content, category, source_user,
                json.dumps(tags) if tags else None, status,
                confidence, domain,
                json.dumps(entities) if entities else None,
                source_type, source_ref,
                valid_from, valid_until, supersedes, sensitivity, is_personal,
                now, now,
            ],
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
        domain: Optional[str] = None,
        source_type: Optional[str] = None,
        exclude_personal: bool = False,
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
        if domain:
            query += " AND domain = ?"
            params.append(domain)
        if source_type:
            query += " AND source_type = ?"
            params.append(source_type)
        if exclude_personal:
            query += " AND (is_personal = FALSE OR is_personal IS NULL)"
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

    def list_by_domain(
        self,
        domain: str,
        statuses: Optional[List[str]] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM knowledge_items WHERE domain = ?"
        params: List[Any] = [domain]
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            query += f" AND status IN ({placeholders})"
            params.extend(statuses)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        return self._rows_to_dicts(self.conn.execute(query, params).fetchall())

    def get_user_contributions(self, source_user: str) -> List[Dict[str, Any]]:
        results = self.conn.execute(
            "SELECT * FROM knowledge_items WHERE source_user = ? ORDER BY updated_at DESC",
            [source_user],
        ).fetchall()
        return self._rows_to_dicts(results)

    def set_personal(self, item_id: str, is_personal: bool) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            "UPDATE knowledge_items SET is_personal = ?, updated_at = ? WHERE id = ?",
            [is_personal, now, item_id],
        )

    # --- Votes ---

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

    # --- Contradictions ---

    def create_contradiction(
        self,
        item_a_id: str,
        item_b_id: str,
        explanation: str,
        severity: Optional[str] = None,
        suggested_resolution: Optional[str] = None,
    ) -> str:
        contradiction_id = f"kc_{uuid.uuid4().hex[:12]}"
        self.conn.execute(
            """INSERT INTO knowledge_contradictions (
                id, item_a_id, item_b_id, explanation, severity, suggested_resolution
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            [contradiction_id, item_a_id, item_b_id, explanation, severity, suggested_resolution],
        )
        return contradiction_id

    def list_contradictions(
        self,
        resolved: Optional[bool] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM knowledge_contradictions WHERE 1=1"
        params: List[Any] = []
        if resolved is not None:
            query += " AND resolved = ?"
            params.append(resolved)
        query += " ORDER BY detected_at DESC LIMIT ?"
        params.append(limit)
        return self._rows_to_dicts(self.conn.execute(query, params).fetchall())

    def resolve_contradiction(
        self,
        contradiction_id: str,
        resolved_by: str,
        resolution: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """UPDATE knowledge_contradictions
            SET resolved = TRUE, resolved_by = ?, resolved_at = ?, resolution = ?
            WHERE id = ?""",
            [resolved_by, now, resolution, contradiction_id],
        )

    def get_contradiction(self, contradiction_id: str) -> Optional[Dict[str, Any]]:
        result = self.conn.execute(
            "SELECT * FROM knowledge_contradictions WHERE id = ?",
            [contradiction_id],
        ).fetchone()
        return self._row_to_dict(result)

    # --- Session Extraction State ---

    def mark_session_processed(
        self,
        session_file: str,
        username: str,
        items_extracted: int = 0,
        file_hash: Optional[str] = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO session_extraction_state (session_file, username, processed_at, items_extracted, file_hash)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (session_file) DO UPDATE
            SET processed_at = excluded.processed_at,
                items_extracted = excluded.items_extracted,
                file_hash = excluded.file_hash""",
            [session_file, username, now, items_extracted, file_hash],
        )

    def is_session_processed(self, session_file: str) -> bool:
        result = self.conn.execute(
            "SELECT 1 FROM session_extraction_state WHERE session_file = ?",
            [session_file],
        ).fetchone()
        return result is not None

    def find_contradiction_candidates(
        self,
        new_item_id: str,
        domain: Optional[str] = None,
        title_words: Optional[List[str]] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Find existing items that might contradict the new item.

        Uses domain match and keyword match to pre-filter before LLM judge.
        """
        conditions = []
        params: List[Any] = [new_item_id]

        if domain:
            conditions.append("domain = ?")
            params.append(domain)
        if title_words:
            for word in title_words[:3]:
                conditions.append("(title ILIKE ? OR content ILIKE ?)")
                pattern = f"%{word}%"
                params.extend([pattern, pattern])

        if not conditions:
            return []

        where_clause = " OR ".join(conditions)
        query = f"""
            SELECT * FROM knowledge_items
            WHERE status IN ('approved', 'mandatory', 'pending')
            AND id != ?
            AND ({where_clause})
            ORDER BY updated_at DESC
            LIMIT ?
        """
        params.append(limit)
        return self._rows_to_dicts(self.conn.execute(query, params).fetchall())
