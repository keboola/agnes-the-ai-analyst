"""DuckDB-backed repository for ``corpus_chunks`` (v77).

One row per text chunk extracted from a ``corpus_files`` document.
``embedding`` is left NULL by this repo (populated in Retrieval slice 4).

Template: src/repositories/corpus_files.py.
"""

from __future__ import annotations

import secrets
from typing import Any, Dict, List

import duckdb

_COLS = [
    "id",
    "corpus_id",
    "file_id",
    "ordinal",
    "text",
    "embedding",
    "section_path",
    "page",
    "bbox",
    "metadata",
    "created_at",
]
_SELECT = ", ".join(_COLS)


class CorpusChunksRepository:
    """DuckDB twin for the ``corpus_chunks`` table."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def add_many(self, chunks: List[Dict[str, Any]]) -> int:
        """Bulk-insert chunk rows.

        Each dict must contain ``corpus_id``, ``file_id``, ``ordinal``,
        ``text``.  Optional keys: ``embedding`` (list of 384 floats, else NULL),
        ``section_path``, ``page``, ``bbox``, ``metadata``.

        Returns the number of rows inserted.
        """
        if not chunks:
            return 0
        for chunk in chunks:
            chunk_id = "ck_" + secrets.token_hex(8)
            self.conn.execute(
                "INSERT INTO corpus_chunks "
                "(id, corpus_id, file_id, ordinal, text, embedding, "
                " section_path, page, bbox, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    chunk_id,
                    chunk["corpus_id"],
                    chunk["file_id"],
                    chunk.get("ordinal"),
                    chunk.get("text"),
                    chunk.get("embedding"),
                    chunk.get("section_path"),
                    chunk.get("page"),
                    chunk.get("bbox"),
                    chunk.get("metadata"),
                ],
            )
        return len(chunks)

    def delete_for_file(self, file_id: str) -> None:
        """Remove all chunks for the given file (idempotent)."""
        self.conn.execute("DELETE FROM corpus_chunks WHERE file_id = ?", [file_id])

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def list_for_file(self, file_id: str) -> List[Dict[str, Any]]:
        """All chunks for one file, ordered by ordinal."""
        rows = self.conn.execute(
            f"SELECT {_SELECT} FROM corpus_chunks WHERE file_id = ? ORDER BY ordinal",
            [file_id],
        ).fetchall()
        return [dict(zip(_COLS, r)) for r in rows]

    def list_for_corpus(self, corpus_id: str) -> List[Dict[str, Any]]:
        """All chunks for an entire corpus, ordered by file_id then ordinal."""
        rows = self.conn.execute(
            f"SELECT {_SELECT} FROM corpus_chunks WHERE corpus_id = ? ORDER BY file_id, ordinal",
            [corpus_id],
        ).fetchall()
        return [dict(zip(_COLS, r)) for r in rows]

    def list_for_corpora(self, corpus_ids: List[str]) -> List[Dict[str, Any]]:
        """All chunks across several corpora (for retrieval). Empty list → []."""
        if not corpus_ids:
            return []
        placeholders = ", ".join("?" for _ in corpus_ids)
        rows = self.conn.execute(
            f"SELECT {_SELECT} FROM corpus_chunks WHERE corpus_id IN ({placeholders}) ORDER BY file_id, ordinal",
            list(corpus_ids),
        ).fetchall()
        return [dict(zip(_COLS, r)) for r in rows]
