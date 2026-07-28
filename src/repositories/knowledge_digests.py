"""DuckDB-backed repository for ``knowledge_digests`` (v89, K4, #799).

Admin-defined digest document the scheduler regenerates via LLM when its
source corpora change. ``source_corpus_ids`` is a JSON array stored as
VARCHAR text, decoded to a list on every read (the ``processing_detail``
idiom, ``src/repositories/corpus_files.py``).

Two invariants enforced at the SQL layer (never-half-written / never-silent,
see the K4 plan's Global Constraints):

- :meth:`set_generated` is ONE statement that sets ``output_md``,
  ``source_fingerprint``, ``model``, ``generated_at``, flips
  ``status='fresh'`` and clears ``status_reason`` — a digest is either
  fully regenerated or untouched, never half-written.
- :meth:`mark_stale` is ONE statement that only flips ``status``/
  ``status_reason`` — ``output_md``/``source_fingerprint``/``generated_at``
  survive untouched so the previous (last-good) markdown keeps shipping.

Template: src/repositories/corpus_files.py.
"""

from __future__ import annotations

import json
import secrets
from typing import Any, Dict, List, Optional

import duckdb


class KnowledgeDigestsRepository:
    """DuckDB twin for the ``knowledge_digests`` table."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    _COLS = [
        "id",
        "slug",
        "title",
        "instructions",
        "source_corpus_ids",
        "output_md",
        "source_fingerprint",
        "generated_at",
        "model",
        "status",
        "status_reason",
        "created_by",
        "created_at",
        "updated_at",
    ]
    _SELECT = ", ".join(_COLS)

    @staticmethod
    def _decode_row(row_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Decode ``source_corpus_ids`` from JSON text to a list (or ``[]``)."""
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
        self.conn.execute(
            "INSERT INTO knowledge_digests "
            "(id, slug, title, instructions, source_corpus_ids, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [digest_id, slug, title, instructions, json.dumps(source_corpus_ids), created_by],
        )
        return digest_id

    def get(self, digest_id: str) -> Optional[Dict[str, Any]]:
        """Fetch one digest row by id. Returns ``None`` if not found."""
        row = self.conn.execute(
            f"SELECT {self._SELECT} FROM knowledge_digests WHERE id = ?",
            [digest_id],
        ).fetchone()
        if not row:
            return None
        return self._decode_row(dict(zip(self._COLS, row)))

    def get_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        """Fetch one digest row by slug. Returns ``None`` if not found."""
        row = self.conn.execute(
            f"SELECT {self._SELECT} FROM knowledge_digests WHERE slug = ?",
            [slug],
        ).fetchone()
        if not row:
            return None
        return self._decode_row(dict(zip(self._COLS, row)))

    def list(self) -> List[Dict[str, Any]]:
        """All digests, ordered by created_at."""
        rows = self.conn.execute(
            f"SELECT {self._SELECT} FROM knowledge_digests ORDER BY created_at",
        ).fetchall()
        return [self._decode_row(dict(zip(self._COLS, r))) for r in rows]

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
        params: List[Any] = []
        if title is not None:
            sets.append("title = ?")
            params.append(title)
        if instructions is not None:
            sets.append("instructions = ?")
            params.append(instructions)
        if source_corpus_ids is not None:
            sets.append("source_corpus_ids = ?")
            params.append(json.dumps(source_corpus_ids))
        if not sets:
            return
        sets.append("updated_at = current_timestamp")
        params.append(digest_id)
        self.conn.execute(
            f"UPDATE knowledge_digests SET {', '.join(sets)} WHERE id = ?",
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
        self.conn.execute(
            "UPDATE knowledge_digests "
            "SET output_md = ?, source_fingerprint = ?, model = ?, "
            "    generated_at = current_timestamp, status = 'fresh', "
            "    status_reason = NULL, updated_at = current_timestamp "
            "WHERE id = ?",
            [output_md, source_fingerprint, model, digest_id],
        )

    def mark_stale(self, digest_id: str, *, reason: str) -> None:
        """Mark a digest visibly stale — ONE atomic statement.

        Only ``status``/``status_reason``/``updated_at`` change;
        ``output_md``/``source_fingerprint``/``generated_at`` survive
        untouched so the previous (last-good) markdown keeps shipping.
        """
        self.conn.execute(
            "UPDATE knowledge_digests "
            "SET status = 'stale', status_reason = ?, updated_at = current_timestamp "
            "WHERE id = ?",
            [reason, digest_id],
        )

    def delete(self, digest_id: str) -> None:
        """Hard-delete a digest row."""
        self.conn.execute("DELETE FROM knowledge_digests WHERE id = ?", [digest_id])
