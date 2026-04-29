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

    def get_by_ids(self, item_ids: List[str]) -> Dict[str, Any]:
        """Fetch multiple items by ID in one query. Returns dict keyed by id."""
        if not item_ids:
            return {}
        placeholders = ", ".join("?" for _ in item_ids)
        rows = self.conn.execute(
            f"SELECT * FROM knowledge_items WHERE id IN ({placeholders})",
            item_ids,
        ).fetchall()
        items = self._rows_to_dicts(rows)
        return {item["id"]: item for item in items}

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

    _UPDATABLE_FIELDS = {
        "title", "content", "category", "tags", "domain", "entities",
        "source_type", "source_ref", "source_user", "audience",
        "confidence", "status", "sensitivity", "is_personal",
        "valid_from", "valid_until", "supersedes",
    }

    def update(self, item_id: str, **fields) -> None:
        safe = {k: v for k, v in fields.items() if k in self._UPDATABLE_FIELDS}
        if not safe:
            return
        now = datetime.now(timezone.utc)
        set_clause = ", ".join(f"{k} = ?" for k in safe)
        values = list(safe.values()) + [now, item_id]
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
        user_groups: Optional[List[str]] = None,
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
        if user_groups is not None:
            # Audience filter: null/all → visible to everyone; group:X → only that group.
            if user_groups:
                audience_placeholders = ", ".join("?" for _ in user_groups)
                query += f" AND (audience IS NULL OR audience = 'all' OR audience IN ({audience_placeholders}))"
                params.extend(user_groups)
            else:
                query += " AND (audience IS NULL OR audience = 'all')"
        query += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return self._rows_to_dicts(self.conn.execute(query, params).fetchall())

    def search(
        self,
        query: str,
        exclude_personal: bool = False,
        user_groups: Optional[List[str]] = None,
        statuses: Optional[List[str]] = None,
        category: Optional[str] = None,
        domain: Optional[str] = None,
        source_type: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        pattern = f"%{query}%"
        sql = """SELECT * FROM knowledge_items
            WHERE (title ILIKE ? OR content ILIKE ?)"""
        params: List[Any] = [pattern, pattern]
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            sql += f" AND status IN ({placeholders})"
            params.extend(statuses)
        if category:
            sql += " AND category = ?"
            params.append(category)
        if domain:
            sql += " AND domain = ?"
            params.append(domain)
        if source_type:
            sql += " AND source_type = ?"
            params.append(source_type)
        if exclude_personal:
            sql += " AND (is_personal = FALSE OR is_personal IS NULL)"
        if user_groups is not None:
            if user_groups:
                audience_placeholders = ", ".join("?" for _ in user_groups)
                sql += f" AND (audience IS NULL OR audience = 'all' OR audience IN ({audience_placeholders}))"
                params.extend(user_groups)
            else:
                sql += " AND (audience IS NULL OR audience = 'all')"
        sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        results = self.conn.execute(sql, params).fetchall()
        return self._rows_to_dicts(results)

    def count_items(
        self,
        search: Optional[str] = None,
        statuses: Optional[List[str]] = None,
        category: Optional[str] = None,
        domain: Optional[str] = None,
        source_type: Optional[str] = None,
        exclude_personal: bool = False,
        user_groups: Optional[List[str]] = None,
    ) -> int:
        if search:
            pattern = f"%{search}%"
            sql = "SELECT COUNT(*) FROM knowledge_items WHERE (title ILIKE ? OR content ILIKE ?)"
            params: List[Any] = [pattern, pattern]
        else:
            sql = "SELECT COUNT(*) FROM knowledge_items WHERE 1=1"
            params = []
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            sql += f" AND status IN ({placeholders})"
            params.extend(statuses)
        if category:
            sql += " AND category = ?"
            params.append(category)
        if domain:
            sql += " AND domain = ?"
            params.append(domain)
        if source_type:
            sql += " AND source_type = ?"
            params.append(source_type)
        if exclude_personal:
            sql += " AND (is_personal = FALSE OR is_personal IS NULL)"
        if user_groups is not None:
            if user_groups:
                audience_placeholders = ", ".join("?" for _ in user_groups)
                sql += f" AND (audience IS NULL OR audience = 'all' OR audience IN ({audience_placeholders}))"
                params.extend(user_groups)
            else:
                sql += " AND (audience IS NULL OR audience = 'all')"
        return self.conn.execute(sql, params).fetchone()[0]

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

    def unvote(self, item_id: str, user_id: str) -> None:
        self.conn.execute(
            "DELETE FROM knowledge_votes WHERE item_id = ? AND user_id = ?",
            [item_id, user_id],
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
        suggested_resolution: Optional[Any] = None,
    ) -> str:
        """Persist a contradiction.

        ``suggested_resolution`` may be either a free-form string (legacy
        callers) or a dict (the structured shape produced by Haiku — see ADR
        Decision 4). Dicts are JSON-encoded into the existing TEXT column so
        no schema migration is needed; the read side decodes back to dict.
        """
        if isinstance(suggested_resolution, dict):
            suggested_resolution_db: Optional[str] = json.dumps(suggested_resolution)
        else:
            suggested_resolution_db = suggested_resolution
        contradiction_id = f"kc_{uuid.uuid4().hex[:12]}"
        self.conn.execute(
            """INSERT INTO knowledge_contradictions (
                id, item_a_id, item_b_id, explanation, severity, suggested_resolution
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            [contradiction_id, item_a_id, item_b_id, explanation, severity, suggested_resolution_db],
        )
        return contradiction_id

    @staticmethod
    def _decode_suggested_resolution(row: Dict[str, Any]) -> Dict[str, Any]:
        """If the stored suggested_resolution is JSON, decode it to a dict.
        Plain strings (legacy rows) are returned unchanged.
        """
        raw = row.get("suggested_resolution")
        if isinstance(raw, str) and raw and raw.lstrip().startswith("{"):
            try:
                row["suggested_resolution"] = json.loads(raw)
            except json.JSONDecodeError:
                pass
        return row

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
        rows = self._rows_to_dicts(self.conn.execute(query, params).fetchall())
        return [self._decode_suggested_resolution(r) for r in rows]

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
        row = self._row_to_dict(result)
        if row is not None:
            self._decode_suggested_resolution(row)
        return row

    # --- Verification Evidence ---

    def create_evidence(
        self,
        item_id: str,
        source_user: Optional[str] = None,
        source_ref: Optional[str] = None,
        detection_type: Optional[str] = None,
        user_quote: Optional[str] = None,
    ) -> str:
        """Persist one verification evidence row for a knowledge item.

        Multiple evidence rows per item are expected — each new analyst
        confirmation/correction adds one. user_quote and detection_type are the
        raw signal future Bayesian re-calibration consumes.
        """
        evidence_id = f"ev_{uuid.uuid4().hex[:12]}"
        self.conn.execute(
            """INSERT INTO verification_evidence (
                id, item_id, source_user, source_ref, detection_type, user_quote
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            [evidence_id, item_id, source_user, source_ref, detection_type, user_quote],
        )
        return evidence_id

    def list_evidence(self, item_id: str) -> List[Dict[str, Any]]:
        results = self.conn.execute(
            """SELECT * FROM verification_evidence
            WHERE item_id = ?
            ORDER BY created_at ASC""",
            [item_id],
        ).fetchall()
        return self._rows_to_dicts(results)

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
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Same-domain candidates for LLM-based contradiction judgment.

        Domain is the only narrowing applied at the SQL layer. Topic / content
        matching is delegated to the LLM judge in
        services.corporate_memory.contradiction.find_and_judge() — see ADR
        Decision 4. The brittle keyword-substring layer that used to live here
        was removed; it had recall holes (synonyms, paraphrases) and the
        domain conjunct alone is enough as a hard ACL.

        Personal items (`is_personal = TRUE`) are excluded unconditionally —
        the LLM call is a read site (and exfiltrates content to the external
        API), so ADR Decision 1 ("hard privacy boundary, not a UI hint")
        applies. Without this filter, personal item content would be
        serialized into every contradiction prompt and could be paraphrased
        into `knowledge_contradictions.suggested_resolution.merged_content`
        — bypassing the contributor-only visibility rule.
        """
        sql = """
            SELECT * FROM knowledge_items
            WHERE status IN ('approved', 'mandatory', 'pending')
              AND (is_personal = FALSE OR is_personal IS NULL)
              AND id != ?
        """
        params: List[Any] = [new_item_id]
        if domain:
            sql += " AND domain = ?"
            params.append(domain)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        return self._rows_to_dicts(self.conn.execute(sql, params).fetchall())
