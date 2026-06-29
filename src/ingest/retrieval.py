"""Hybrid retrieval over Collections chunks.

Brute-force at the current scale (dozens of files): fetch the candidate chunks
for the caller's *granted* corpora, score each by lexical term overlap and — when
an embedding model is installed and the chunk was embedded — cosine similarity,
fuse the two, and return the top-k with citations. Brute-force keeps the door
open for an indexed strategy (DuckDB ``vss``/HNSW) later behind this same
``search`` interface.

RBAC is the caller's responsibility: pass only ``corpus_ids`` the caller may
access. Empty ``corpus_ids`` → empty result (fail-closed) — never "search all".
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional

from src.ingest.embeddings import embed_query
from src.repositories import corpus_chunks_repo, corpus_files_repo

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> List[str]:
    return _TOKEN_RE.findall((text or "").lower())


def _lexical_score(q_terms: set[str], text: str) -> float:
    if not q_terms:
        return 0.0
    toks = _tokenize(text)
    if not toks:
        return 0.0
    tokset = set(toks)
    hits = sum(1 for t in q_terms if t in tokset)
    return hits / len(q_terms)


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def search(
    corpus_ids: List[str],
    query: str,
    *,
    k: int = 10,
) -> List[Dict[str, Any]]:
    """Return up to ``k`` ranked chunks from the given corpora, with citations.

    Fail-closed: empty ``corpus_ids`` or blank query → ``[]``.
    """
    if not corpus_ids or not (query or "").strip():
        return []

    chunks = corpus_chunks_repo().list_for_corpora(corpus_ids)
    if not chunks:
        return []

    q_terms = set(_tokenize(query))
    q_vec: Optional[List[float]] = embed_query(query)  # None when extra absent

    scored: List[tuple[float, Dict[str, Any]]] = []
    for ch in chunks:
        lex = _lexical_score(q_terms, ch.get("text", ""))
        emb = ch.get("embedding")
        if q_vec is not None and emb:
            vec = max(0.0, _cosine(q_vec, list(emb)))
            score = 0.5 * lex + 0.5 * vec
        else:
            score = lex
        if score > 0:
            scored.append((score, ch))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:k]

    # Resolve filenames for citations (cache per file).
    cf_repo = corpus_files_repo()
    name_cache: Dict[str, Optional[str]] = {}

    def _filename(file_id: str) -> Optional[str]:
        if file_id not in name_cache:
            row = cf_repo.get(file_id)
            name_cache[file_id] = row.get("filename") if row else None
        return name_cache[file_id]

    results: List[Dict[str, Any]] = []
    for score, ch in top:
        results.append(
            {
                "chunk_id": ch.get("id"),
                "corpus_id": ch.get("corpus_id"),
                "file_id": ch.get("file_id"),
                "filename": _filename(ch.get("file_id")),
                "ordinal": ch.get("ordinal"),
                "section_path": ch.get("section_path"),
                "text": ch.get("text"),
                "score": round(float(score), 4),
            }
        )
    return results
