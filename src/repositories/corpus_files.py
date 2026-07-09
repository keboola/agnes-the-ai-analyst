"""DuckDB-backed repository for ``corpus_files`` (v82).

One row per uploaded file associated with a ``file_corpora`` corpus.
Tracks the processing lifecycle: pending → processing → indexed | needs_review | rejected.
``processing_detail`` is a JSON dict stored as VARCHAR text.

Template: src/repositories/data_packages.py.
"""

from __future__ import annotations

import json
import secrets
from typing import Any, Dict, List, Optional

import duckdb


class CorpusFilesRepository:
    """DuckDB twin for the ``corpus_files`` table."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    _COLS = [
        "id",
        "corpus_id",
        "filename",
        "sha256",
        "file_type",
        "size_bytes",
        "storage_path",
        "processing_status",
        "processing_detail",
        "created_at",
        "updated_at",
    ]
    _SELECT = ", ".join(_COLS)

    @staticmethod
    def _decode_row(row_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Decode ``processing_detail`` from JSON text to dict (or keep None)."""
        v = row_dict.get("processing_detail")
        if v is None or v == "":
            row_dict["processing_detail"] = None
        elif isinstance(v, str):
            try:
                row_dict["processing_detail"] = json.loads(v)
            except (ValueError, TypeError):
                row_dict["processing_detail"] = None
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
        self.conn.execute(
            "INSERT INTO corpus_files "
            "(id, corpus_id, filename, sha256, file_type, size_bytes, storage_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [file_id, corpus_id, filename, sha256, file_type, size_bytes, storage_path],
        )
        return file_id

    def get(self, file_id: str) -> Optional[Dict[str, Any]]:
        """Fetch one file row by id. Returns ``None`` if not found."""
        row = self.conn.execute(
            f"SELECT {self._SELECT} FROM corpus_files WHERE id = ?",
            [file_id],
        ).fetchone()
        if not row:
            return None
        return self._decode_row(dict(zip(self._COLS, row)))

    def list_for_corpus(self, corpus_id: str) -> List[Dict[str, Any]]:
        """All files for a given corpus, ordered by created_at."""
        rows = self.conn.execute(
            f"SELECT {self._SELECT} FROM corpus_files WHERE corpus_id = ? ORDER BY created_at",
            [corpus_id],
        ).fetchall()
        return [self._decode_row(dict(zip(self._COLS, r))) for r in rows]

    def set_status(
        self,
        file_id: str,
        *,
        status: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Update processing_status (and optionally processing_detail).

        ``detail`` is serialised to JSON text before writing.
        """
        detail_json = json.dumps(detail) if detail is not None else None
        self.conn.execute(
            "UPDATE corpus_files "
            "SET processing_status = ?, processing_detail = ?, "
            "    updated_at = current_timestamp "
            "WHERE id = ?",
            [status, detail_json, file_id],
        )

    def delete(self, file_id: str) -> None:
        """Hard-delete a file row (individual files are not soft-deleted)."""
        self.conn.execute("DELETE FROM corpus_files WHERE id = ?", [file_id])
