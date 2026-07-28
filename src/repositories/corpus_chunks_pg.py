"""Postgres-backed repository for ``corpus_chunks`` (v82).

Mirrors ``src/repositories/corpus_chunks.py`` (the DuckDB impl) on the
``CorpusChunksRepository`` public surface. Cross-engine parity is covered
by ``tests/db_pg/test_corpus_chunks_contract.py``.
"""

from __future__ import annotations

import secrets
from typing import Any, Dict, List

import sqlalchemy as sa
from sqlalchemy.engine import Engine

_EMBED_DIM = 384


class CorpusChunksPgRepository:
    """Postgres twin of ``CorpusChunksRepository``."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def add_many(self, chunks: List[Dict[str, Any]]) -> int:
        """Bulk-insert chunk rows.

        Each dict must contain ``corpus_id``, ``file_id``, ``ordinal``,
        ``text``.  Optional keys: ``embedding`` (list of 384 floats, else NULL),
        ``section_path``, ``page``, ``bbox``, ``metadata``.

        The PG ``embedding`` column is an unbounded ``real[]`` (float4, matching
        the DuckDB ``FLOAT[384]`` storage precision; pgvector is a later option),
        so the dimension is not enforced by the column type on either backend.
        Both repos therefore validate explicitly, up front in a pre-loop pass —
        so a wrong-dimension vector raises the same ``ValueError`` before any
        insert round-trip, mirroring the DuckDB sibling.

        Returns the number of rows inserted.
        """
        if not chunks:
            return 0
        for chunk in chunks:
            embedding = chunk.get("embedding")
            if embedding is not None and len(embedding) != _EMBED_DIM:
                raise ValueError(f"embedding must be {_EMBED_DIM}-dim, got {len(embedding)}")
        with self._engine.begin() as conn:
            for chunk in chunks:
                chunk_id = "ck_" + secrets.token_hex(8)
                embedding = chunk.get("embedding")
                conn.execute(
                    sa.text(
                        "INSERT INTO corpus_chunks "
                        "(id, corpus_id, file_id, ordinal, text, embedding, "
                        " section_path, page, bbox, metadata) "
                        "VALUES (:id, :corpus_id, :file_id, :ordinal, :text, :embedding, "
                        "        :section_path, :page, :bbox, :metadata)"
                    ),
                    {
                        "id": chunk_id,
                        "corpus_id": chunk["corpus_id"],
                        "file_id": chunk["file_id"],
                        "ordinal": chunk.get("ordinal"),
                        "text": chunk.get("text"),
                        "embedding": embedding,
                        "section_path": chunk.get("section_path"),
                        "page": chunk.get("page"),
                        "bbox": chunk.get("bbox"),
                        "metadata": chunk.get("metadata"),
                    },
                )
        return len(chunks)

    def delete_for_file(self, file_id: str) -> None:
        """Remove all chunks for the given file (idempotent)."""
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM corpus_chunks WHERE file_id = :file_id"),
                {"file_id": file_id},
            )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def list_for_file(self, file_id: str) -> List[Dict[str, Any]]:
        """All chunks for one file, ordered by ordinal."""
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT id, corpus_id, file_id, ordinal, text, embedding, "
                        "       section_path, page, bbox, metadata, created_at "
                        "FROM corpus_chunks WHERE file_id = :file_id ORDER BY ordinal"
                    ),
                    {"file_id": file_id},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def list_for_corpus(self, corpus_id: str) -> List[Dict[str, Any]]:
        """All chunks for an entire corpus, ordered by file_id then ordinal."""
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT id, corpus_id, file_id, ordinal, text, embedding, "
                        "       section_path, page, bbox, metadata, created_at "
                        "FROM corpus_chunks WHERE corpus_id = :corpus_id "
                        "ORDER BY file_id, ordinal"
                    ),
                    {"corpus_id": corpus_id},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def list_for_corpora(self, corpus_ids: List[str]) -> List[Dict[str, Any]]:
        """All chunks across several corpora (for retrieval). Empty list → []."""
        if not corpus_ids:
            return []
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT id, corpus_id, file_id, ordinal, text, embedding, "
                        "       section_path, page, bbox, metadata, created_at "
                        "FROM corpus_chunks WHERE corpus_id IN :corpus_ids "
                        "ORDER BY file_id, ordinal"
                    ).bindparams(sa.bindparam("corpus_ids", expanding=True)),
                    {"corpus_ids": list(corpus_ids)},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]
