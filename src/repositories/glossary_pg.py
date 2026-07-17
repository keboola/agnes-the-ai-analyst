"""Postgres-backed glossary_terms repository. Mirrors src/repositories/glossary.py."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class GlossaryPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

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
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO glossary_terms (
                        id, term, definition, see_also, model_uuid, source,
                        created_at, updated_at
                    ) VALUES (
                        :id, :term, :definition, :see_also, :model_uuid, :source,
                        :now, :now
                    )
                    ON CONFLICT (id) DO UPDATE SET
                        term = EXCLUDED.term,
                        definition = EXCLUDED.definition,
                        see_also = EXCLUDED.see_also,
                        model_uuid = EXCLUDED.model_uuid,
                        source = EXCLUDED.source,
                        updated_at = EXCLUDED.updated_at"""
                ),
                {
                    "id": id,
                    "term": term,
                    "definition": definition,
                    "see_also": see_also,
                    "model_uuid": model_uuid,
                    "source": source,
                    "now": now,
                },
            )
        return self.get(id)  # type: ignore[return-value]

    def get(self, glossary_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text("SELECT * FROM glossary_terms WHERE id = :id"),
                    {"id": glossary_id},
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def list(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text("SELECT * FROM glossary_terms ORDER BY term LIMIT :limit"),
                    {"limit": limit},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def delete(self, glossary_id: str) -> bool:
        existing = self.get(glossary_id)
        if existing is None:
            return False
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM glossary_terms WHERE id = :id"),
                {"id": glossary_id},
            )
        return True

    def search(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        pattern = f"%{query}%"
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT * FROM glossary_terms WHERE (term ILIKE :p OR definition ILIKE :p) "
                        "ORDER BY term LIMIT :limit"
                    ),
                    {"p": pattern, "limit": limit},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]
