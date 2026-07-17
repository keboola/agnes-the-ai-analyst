"""Repository for glossary_terms — Keboola semantic-glossary import
destination (docs/superpowers/specs/2026-07-17-keboola-glossary-import-design.md)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import duckdb


class GlossaryRepository:
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

    def create(
        self,
        id: str,
        term: str,
        definition: str,
        see_also: Optional[List[str]] = None,
        model_uuid: Optional[str] = None,
        source: str = "manual",
    ) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO glossary_terms (
                id, term, definition, see_also, model_uuid, source,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                term = excluded.term,
                definition = excluded.definition,
                see_also = excluded.see_also,
                model_uuid = excluded.model_uuid,
                source = excluded.source,
                updated_at = excluded.updated_at""",
            [id, term, definition, see_also, model_uuid, source, now, now],
        )
        return self.get(id)  # type: ignore[return-value]

    def get(self, glossary_id: str) -> Optional[Dict[str, Any]]:
        result = self.conn.execute("SELECT * FROM glossary_terms WHERE id = ?", [glossary_id]).fetchone()
        return self._row_to_dict(result)

    def list(self, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM glossary_terms ORDER BY term LIMIT ?", [limit]).fetchall()
        return self._rows_to_dicts(rows)

    def delete(self, glossary_id: str) -> bool:
        existing = self.get(glossary_id)
        if existing is None:
            return False
        self.conn.execute("DELETE FROM glossary_terms WHERE id = ?", [glossary_id])
        return True

    def search(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """ILIKE search over term + definition. Task 3 upgrades this to
        prefer DuckDB FTS BM25 ranking when the extension is available,
        falling back to this same ILIKE query otherwise."""
        pattern = f"%{query}%"
        rows = self.conn.execute(
            "SELECT * FROM glossary_terms WHERE (term ILIKE ? OR definition ILIKE ?) ORDER BY term LIMIT ?",
            [pattern, pattern, limit],
        ).fetchall()
        return self._rows_to_dicts(rows)
