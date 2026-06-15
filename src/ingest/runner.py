"""Ingestion router — drive a single uploaded file through Tier-1 ingestion.

Routes by file type: tabular → a registered DuckDB table; prose → ``corpus_chunks``
(text only; embeddings are Slice 4); images (tier-2) → left ``pending`` for the
vision slice (Slice 5). Moves ``processing_status`` pending → processing →
indexed | rejected. Idempotent: re-ingesting a document replaces its chunks.
"""

from __future__ import annotations

import logging
from typing import Optional

from src.ingest.chunking import chunk_text
from src.ingest.tabular import UnsupportedTabular, ingest_tabular
from src.ingest.text_extract import UnsupportedDocument, extract_text
from src.repositories import corpus_chunks_repo, corpus_files_repo

logger = logging.getLogger(__name__)

TABULAR_EXTS = {"csv", "tsv", "parquet", "json", "jsonl", "xlsx", "xls"}
IMAGE_EXTS = {"png", "jpg", "jpeg", "tif", "tiff", "gif", "webp", "bmp"}


def _ext_of(filename: str, file_type: Optional[str]) -> str:
    if file_type and "/" not in file_type:
        return file_type.lower().lstrip(".")
    if "." in filename:
        return filename.rsplit(".", 1)[-1].lower()
    return ""


def ingest_file(file_id: str) -> str:
    """Ingest one uploaded file. Returns the final ``processing_status``."""
    cf_repo = corpus_files_repo()
    row = cf_repo.get(file_id)
    if not row:
        return "missing"
    if row.get("processing_status") == "rejected":
        return "rejected"  # already rejected at upload (unsupported type)

    corpus_id = row["corpus_id"]
    filename = row.get("filename") or ""
    storage_path = row.get("storage_path")
    file_type = row.get("file_type")

    if not storage_path:
        cf_repo.set_status(file_id, status="rejected", detail={"reason": "no_storage_path"})
        return "rejected"

    cf_repo.set_status(file_id, status="processing")
    ext = _ext_of(filename, file_type)

    try:
        if ext in TABULAR_EXTS:
            table_id = ingest_tabular(corpus_id, file_id, storage_path, file_type, filename=filename)
            cf_repo.set_status(
                file_id,
                status="indexed",
                detail={"tier": 1, "kind": "tabular", "derived_table_id": table_id},
            )
            return "indexed"

        if ext in IMAGE_EXTS:
            # Tier-2 — vision/OCR ingestion is Slice 5. Leave pending so the
            # vision slice can pick it up; not an error.
            cf_repo.set_status(
                file_id,
                status="pending",
                detail={"tier": 2, "kind": "image", "note": "vision ingestion deferred (Slice 5)"},
            )
            return "pending"

        # Prose document → extract + chunk → corpus_chunks.
        result = extract_text(storage_path, file_type)
        chunks = chunk_text(result)
        chunks_repo = corpus_chunks_repo()
        chunks_repo.delete_for_file(file_id)  # idempotent re-ingest
        rows = [
            {
                "corpus_id": corpus_id,
                "file_id": file_id,
                "ordinal": c.ordinal,
                "text": c.text,
                "section_path": c.section_path,
            }
            for c in chunks
        ]
        n = chunks_repo.add_many(rows)
        cf_repo.set_status(
            file_id,
            status="indexed",
            detail={"tier": 1, "kind": "document", "chunk_count": n},
        )
        return "indexed"

    except (UnsupportedTabular, UnsupportedDocument) as exc:
        cf_repo.set_status(file_id, status="rejected", detail={"reason": str(exc)})
        return "rejected"
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("ingest_file failed file_id=%s", file_id)
        cf_repo.set_status(file_id, status="rejected", detail={"reason": f"ingest_error: {exc}"})
        return "rejected"
