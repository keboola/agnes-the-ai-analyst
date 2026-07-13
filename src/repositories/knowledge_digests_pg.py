"""Postgres-backed repository for ``knowledge_digests`` (v89, K4, #799).

Mirrors ``src/repositories/knowledge_digests.py`` (the DuckDB impl) on the
``KnowledgeDigestsRepository`` public surface. Cross-engine parity is
covered by ``tests/db_pg/test_knowledge_digests_contract.py``.

Implementation notes vs DuckDB:
- ``source_corpus_ids`` is stored as VARCHAR text on both sides (not
  JSONB) so the DuckDB↔PG behaviour is symmetric: writes go through
  ``json.dumps``, reads come back as text and are decoded to a list by
  ``_decode_row`` on both sides.
"""

from __future__ import annotations

import json
import secrets
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class KnowledgeDigestsPgRepository:
    """Postgres twin of ``KnowledgeDigestsRepository``."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @staticmethod
    def _decode_row(row_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Decode ``source_corpus_ids`` from JSON text to a list (or ``[]``).

        PG stores the column as VARCHAR (not JSONB), so psycopg returns a
        plain str — we json.loads it here just like the DuckDB side.
        """
        v = row_dict.get("source_corpus_ids")
        if v is None or v == "":
            row_dict["source_corpus_ids"] = []
        elif isinstance(v, str):
            try:
                row_dict["source_corpus_ids"] = json.loads(v)
            except (ValueError, TypeError):
                row_dict["source_corpus_ids"] = []
        return row_dict

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        slug: str,
        title: str,
        instructions: str,
        source_corpus_ids: List[str],
        created_by: str,
    ) -> str:
        """Insert a new digest row with default status 'pending'.

        Returns the generated ``kd_*`` id. Raises on duplicate ``slug``
        (UNIQUE constraint) — slug is immutable after creation, it is a
        filename on every analyst laptop.
        """
        digest_id = "kd_" + secrets.token_hex(8)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO knowledge_digests "
                    "(id, slug, title, instructions, source_corpus_ids, created_by) "
                    "VALUES (:id, :slug, :title, :instructions, :source_corpus_ids, :created_by)"
                ),
                {
                    "id": digest_id,
                    "slug": slug,
                    "title": title,
                    "instructions": instructions,
                    "source_corpus_ids": json.dumps(source_corpus_ids),
                    "created_by": created_by,
                },
            )
        return digest_id

    def get(self, digest_id: str) -> Optional[Dict[str, Any]]:
        """Fetch one digest row by id. Returns ``None`` if not found."""
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text("SELECT * FROM knowledge_digests WHERE id = :id"),
                    {"id": digest_id},
                )
                .mappings()
                .first()
            )
        return self._decode_row(dict(row)) if row else None

    def get_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        """Fetch one digest row by slug. Returns ``None`` if not found."""
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text("SELECT * FROM knowledge_digests WHERE slug = :slug"),
                    {"slug": slug},
                )
                .mappings()
                .first()
            )
        return self._decode_row(dict(row)) if row else None

    def list(self) -> List[Dict[str, Any]]:
        """All digests, ordered by created_at."""
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text("SELECT * FROM knowledge_digests ORDER BY created_at")).mappings().all()
        return [self._decode_row(dict(r)) for r in rows]

    def update(
        self,
        digest_id: str,
        *,
        title: Optional[str] = None,
        instructions: Optional[str] = None,
        source_corpus_ids: Optional[List[str]] = None,
    ) -> None:
        """Edit ``title``/``instructions``/``source_corpus_ids``.

        ``slug`` is NOT updatable (it is a filename on every analyst
        laptop). Only the provided (non-``None``) fields change; passing
        all-``None`` is a no-op that still bumps nothing and does not raise.
        """
        sets: List[str] = []
        params: Dict[str, Any] = {"id": digest_id}
        if title is not None:
            sets.append("title = :title")
            params["title"] = title
        if instructions is not None:
            sets.append("instructions = :instructions")
            params["instructions"] = instructions
        if source_corpus_ids is not None:
            sets.append("source_corpus_ids = :source_corpus_ids")
            params["source_corpus_ids"] = json.dumps(source_corpus_ids)
        if not sets:
            return
        sets.append("updated_at = CURRENT_TIMESTAMP")
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(f"UPDATE knowledge_digests SET {', '.join(sets)} WHERE id = :id"),
                params,
            )

    def set_generated(
        self,
        digest_id: str,
        *,
        output_md: str,
        source_fingerprint: str,
        model: Optional[str],
    ) -> None:
        """Commit a successful regeneration — ONE atomic statement.

        Sets ``output_md``, ``source_fingerprint``, ``model``,
        ``generated_at=now``, ``status='fresh'``, clears
        ``status_reason`` — the never-half-written invariant.
        """
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE knowledge_digests "
                    "SET output_md = :output_md, source_fingerprint = :source_fingerprint, "
                    "    model = :model, generated_at = CURRENT_TIMESTAMP, status = 'fresh', "
                    "    status_reason = NULL, updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = :id"
                ),
                {
                    "output_md": output_md,
                    "source_fingerprint": source_fingerprint,
                    "model": model,
                    "id": digest_id,
                },
            )

    def mark_stale(self, digest_id: str, *, reason: str) -> None:
        """Mark a digest visibly stale — ONE atomic statement.

        Only ``status``/``status_reason``/``updated_at`` change;
        ``output_md``/``source_fingerprint``/``generated_at`` survive
        untouched so the previous (last-good) markdown keeps shipping.
        """
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE knowledge_digests "
                    "SET status = 'stale', status_reason = :reason, updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = :id"
                ),
                {"reason": reason, "id": digest_id},
            )

    def delete(self, digest_id: str) -> None:
        """Hard-delete a digest row."""
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM knowledge_digests WHERE id = :id"),
                {"id": digest_id},
            )
