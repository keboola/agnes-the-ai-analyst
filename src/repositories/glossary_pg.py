"""Postgres-backed glossary_terms repository. Mirrors src/repositories/glossary.py.

``search`` uses Postgres ``to_tsvector('english', term || ' ' || definition)``
with ``plainto_tsquery`` and ``ts_rank`` for ranking, instead of DuckDB's BM25
extension. Falls back to ``ILIKE`` when the FTS execute raises — same overall
shape and the same ``bm25_score`` result-column naming as
``KnowledgePgRepository.search`` (kept for API-shape consistency with the
DuckDB response, even though the score here is a Postgres ``ts_rank`` value)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


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
        """Relevance-ranked search across term + definition via Postgres
        ``to_tsvector`` / ``plainto_tsquery`` / ``ts_rank`` with an ILIKE
        fallback. Mirrors ``KnowledgePgRepository.search``."""
        params: Dict[str, Any] = {"q": query, "limit": limit}

        fts_sql = (
            "SELECT *, ts_rank("
            "  to_tsvector('english', coalesce(term,'') || ' ' || coalesce(definition,'')), "
            "  plainto_tsquery('english', :q)"
            ") AS bm25_score FROM glossary_terms "
            "WHERE to_tsvector('english', coalesce(term,'') || ' ' || coalesce(definition,'')) "
            "  @@ plainto_tsquery('english', :q) "
            "ORDER BY bm25_score DESC, term LIMIT :limit"
        )
        try:
            with self._engine.connect() as conn:
                rows = conn.execute(sa.text(fts_sql), params).mappings().all()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning("PG FTS failed on glossary_terms (%s); falling back to ILIKE", e)
            pattern = f"%{query}%"
            with self._engine.connect() as conn:
                rows = (
                    conn.execute(
                        sa.text(
                            "SELECT *, NULL AS bm25_score FROM glossary_terms "
                            "WHERE (term ILIKE :p OR definition ILIKE :p) "
                            "ORDER BY term LIMIT :limit"
                        ),
                        {"p": pattern, "limit": limit},
                    )
                    .mappings()
                    .all()
                )
            return [dict(r) for r in rows]
