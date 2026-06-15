"""Postgres-backed repository for ``corpus_files`` (v77).

Mirrors ``src/repositories/corpus_files.py`` (the DuckDB impl) on the
``CorpusFilesRepository`` public surface. Cross-engine parity is covered
by ``tests/db_pg/test_corpus_files_contract.py``.

Implementation notes vs DuckDB:
- ``processing_detail`` is stored as VARCHAR text on both sides (not JSONB)
  so that the DuckDB↔PG behaviour is symmetric: writes go through
  ``json.dumps``, reads come back as text and are decoded to dict by
  ``_decode_row`` on both sides. This avoids the need for JSONB casts
  and keeps the parity contract simple.
"""

from __future__ import annotations

import json
import secrets
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class CorpusFilesPgRepository:
    """Postgres twin of ``CorpusFilesRepository``."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @staticmethod
    def _decode_row(row_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Decode ``processing_detail`` from JSON text to dict (or keep None).

        PG stores the column as VARCHAR (not JSONB), so psycopg returns
        a plain str — we json.loads it here just like the DuckDB side.
        """
        v = row_dict.get("processing_detail")
        if v is None or v == "":
            row_dict["processing_detail"] = None
        elif isinstance(v, str):
            try:
                row_dict["processing_detail"] = json.loads(v)
            except (ValueError, TypeError):
                row_dict["processing_detail"] = None
        # If psycopg already deserialised it (e.g. a future JSONB migration),
        # leave it as-is.
        return row_dict

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def add(
        self,
        *,
        corpus_id: str,
        filename: str,
        sha256: str,
        file_type: Optional[str],
        size_bytes: Optional[int],
        storage_path: Optional[str],
    ) -> str:
        """Insert a new file row with default status 'pending'.

        Returns the generated ``cf_*`` id.
        """
        file_id = "cf_" + secrets.token_hex(8)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO corpus_files "
                    "(id, corpus_id, filename, sha256, file_type, size_bytes, storage_path) "
                    "VALUES (:id, :corpus_id, :filename, :sha256, "
                    "        :file_type, :size_bytes, :storage_path)"
                ),
                {
                    "id": file_id,
                    "corpus_id": corpus_id,
                    "filename": filename,
                    "sha256": sha256,
                    "file_type": file_type,
                    "size_bytes": size_bytes,
                    "storage_path": storage_path,
                },
            )
        return file_id

    def get(self, file_id: str) -> Optional[Dict[str, Any]]:
        """Fetch one file row by id. Returns ``None`` if not found."""
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text("SELECT * FROM corpus_files WHERE id = :id"),
                    {"id": file_id},
                )
                .mappings()
                .first()
            )
        return self._decode_row(dict(row)) if row else None

    def list_for_corpus(self, corpus_id: str) -> List[Dict[str, Any]]:
        """All files for a given corpus, ordered by created_at."""
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text("SELECT * FROM corpus_files WHERE corpus_id = :corpus_id ORDER BY created_at"),
                    {"corpus_id": corpus_id},
                )
                .mappings()
                .all()
            )
        return [self._decode_row(dict(r)) for r in rows]

    def set_status(
        self,
        file_id: str,
        *,
        status: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Update processing_status (and optionally processing_detail).

        ``detail`` is serialised to JSON text before writing — matches
        the DuckDB side's VARCHAR storage.
        """
        detail_json = json.dumps(detail) if detail is not None else None
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE corpus_files "
                    "SET processing_status = :status, "
                    "    processing_detail = :detail, "
                    "    updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = :id"
                ),
                {"status": status, "detail": detail_json, "id": file_id},
            )
