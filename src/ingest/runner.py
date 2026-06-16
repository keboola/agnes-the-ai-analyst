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


def _chunk_embed_store(corpus_id: str, file_id: str, source) -> tuple[int, bool]:
    """Chunk ``source`` (ExtractResult or str), embed best-effort, store.

    Returns ``(chunk_count, embedded)``. Idempotent: clears the file's prior
    chunks first. Embedding is best-effort (optional extra); failure → vectors
    NULL and lexical-only retrieval, never an ingest failure.
    """
    chunks = chunk_text(source)
    chunks_repo = corpus_chunks_repo()
    chunks_repo.delete_for_file(file_id)
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
    embedded = False
    try:
        from src.ingest.embeddings import embed_texts

        vectors = embed_texts([r["text"] for r in rows]) if rows else None
        if vectors is not None and len(vectors) == len(rows):
            for r, v in zip(rows, vectors):
                r["embedding"] = v
            embedded = True
    except Exception:  # pragma: no cover - model runtime issues
        logger.warning("embedding failed for file_id=%s — storing without vectors", file_id)
    n = chunks_repo.add_many(rows)
    return n, embedded


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
            # Tier-2 — try the gated vision fallback (multimodal OCR). Without a
            # configured model/key it returns None and we leave the file pending
            # so a later, configured run can pick it up (not an error).
            from src.ingest.vision import extract_image_text

            text = extract_image_text(storage_path, ext=ext)
            if not text:
                cf_repo.set_status(
                    file_id,
                    status="pending",
                    detail={"tier": 2, "kind": "image", "note": "awaiting vision (no model/key)"},
                )
                return "pending"
            n, embedded = _chunk_embed_store(corpus_id, file_id, text)
            cf_repo.set_status(
                file_id,
                status="indexed",
                detail={"tier": 2, "kind": "image", "chunk_count": n, "vision_used": True, "embedded": embedded},
            )
            return "indexed"

        # Prose document → extract + chunk → corpus_chunks.
        result = extract_text(storage_path, file_type)
        n, embedded = _chunk_embed_store(corpus_id, file_id, result)
        cf_repo.set_status(
            file_id,
            status="indexed",
            detail={"tier": 1, "kind": "document", "chunk_count": n, "embedded": embedded},
        )
        return "indexed"

    except (UnsupportedTabular, UnsupportedDocument) as exc:
        cf_repo.set_status(file_id, status="rejected", detail={"reason": str(exc)})
        return "rejected"
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("ingest_file failed file_id=%s", file_id)
        cf_repo.set_status(file_id, status="rejected", detail={"reason": f"ingest_error: {exc}"})
        return "rejected"
