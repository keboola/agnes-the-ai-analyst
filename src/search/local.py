"""Offline unified search over pulled knowledge artifacts (K3, #798).

Reads every ``<workspace>/user/knowledge/<corpus_id>.duckdb`` shipped by
``agnes pull``, pools their chunk rows into ONE candidate set (exactly what
``list_for_corpora`` yields server-side for the caller's granted corpora —
the pull manifest already applied the same collection grants), and ranks
with the SAME ``rank_chunks`` core the server uses. Vector scoring engages
only when the ``agnes[embeddings]`` extra is installed (``embed_query``
returns None otherwise → lexical-only, the server's own degradation rule).

Chunk-source only by design: knowledge items already live locally as
``.claude/rules/km_*.md`` (in the agent's context), and table catalog cards
require the server. No FTS index is needed — the chunk engine's lexical
scorer is pure Python (see the K3 plan's spec-drift note).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

import duckdb

from src.duckdb_conn import _open_duckdb

logger = logging.getLogger(__name__)

_COLS = [
    "id",
    "corpus_id",
    "file_id",
    "filename",
    "ordinal",
    "text",
    "embedding",
    "section_path",
    "page",
    "bbox",
    "metadata",
]


def list_artifacts(workspace: Path) -> List[Path]:
    kdir = Path(workspace) / "user" / "knowledge"
    if not kdir.is_dir():
        return []
    return sorted(kdir.glob("*.duckdb"))


def _load_chunks(path: Path) -> List[Dict[str, Any]]:
    con = _open_duckdb(str(path), read_only=True)
    try:
        rows = con.execute(f"SELECT {', '.join(_COLS)} FROM chunks").fetchall()
    finally:
        con.close()
    return [dict(zip(_COLS, r)) for r in rows]


def local_search(query: str, *, workspace: Path, k: int = 10) -> List[Dict[str, Any]]:
    """Ranked, cited chunk hits from local artifacts. Fail-closed on nothing local."""
    if not (query or "").strip():
        return []
    chunks: List[Dict[str, Any]] = []
    for path in list_artifacts(workspace):
        try:
            chunks.extend(_load_chunks(path))
        except duckdb.Error as exc:
            # A torn/foreign file must not kill offline search; pull re-verifies
            # by hash next run.
            logger.warning("skipping unreadable knowledge artifact %s: %s", path, exc)
    if not chunks:
        return []

    from src.ingest.retrieval import rank_chunks

    top, confidence = rank_chunks(chunks, query, k=k)
    return [
        {
            "type": "chunk",
            "chunk_id": ch.get("id"),
            "corpus_id": ch.get("corpus_id"),
            "file_id": ch.get("file_id"),
            "filename": ch.get("filename"),
            "ordinal": ch.get("ordinal"),
            "section_path": ch.get("section_path"),
            "text": ch.get("text"),
            "score": round(float(score), 4),
            "confidence": confidence,
        }
        for score, ch in top
    ]
