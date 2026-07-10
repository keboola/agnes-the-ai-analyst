"""Unified knowledge search (K2, #797) — one query across three engines.

Thin fan-out over the EXISTING search surfaces; none is modified here:

1. Collections chunks — ``src.ingest.retrieval.search`` (hybrid lexical+vector)
2. Knowledge base — ``knowledge_repo().search`` (FTS/BM25 or ILIKE fallback)
3. Table catalog cards — lexical term overlap over ``table_registry`` rows;
   a table hit returns a *pivot hint* (query it with SQL), never rows.

RBAC is the caller's responsibility: pass only granted ``corpus_ids`` /
``user_groups`` / ``granted_domains`` / pre-filtered ``tables``. An empty
grant set for a source contributes nothing (fail-closed), never "search all".
``user_groups`` / ``granted_domains`` follow the knowledge repo's convention:
``None`` means *no filter* (privileged viewer), ``[]`` means *no grants* —
the knowledge source is skipped only when BOTH are empty lists.

Merging: scores are min-max normalized WITHIN each source (the engines'
score scales are incomparable), then interleaved by normalized score with a
deterministic tie-break (type, then id) so equal-score runs are stable.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> List[str]:
    return _TOKEN.findall((text or "").lower())


def _chunk_search(corpus_ids: List[str], query: str, k: int = 10) -> List[Dict[str, Any]]:
    from src.ingest.retrieval import search

    return search(corpus_ids, query, k=k)


def _knowledge_search(query: str, **kw: Any) -> List[Dict[str, Any]]:
    from src.repositories import knowledge_repo

    return knowledge_repo().search(query, **kw)


def _minmax(scores: List[float]) -> List[float]:
    if not scores:
        return []
    lo, hi = min(scores), max(scores)
    if hi <= lo:
        return [1.0] * len(scores)
    return [(s - lo) / (hi - lo) for s in scores]


def _table_scores(query: str, tables: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Lexical term-overlap scoring over name + description + column names."""
    q_terms = set(_tokenize(query))
    if not q_terms:
        return []
    out: List[Dict[str, Any]] = []
    for t in tables:
        cols = ""
        cj = t.get("columns_json")
        if cj:
            try:
                cols = " ".join(str(c.get("name", "")) for c in json.loads(cj))
            except (ValueError, TypeError, AttributeError):
                cols = ""
        hay = set(_tokenize(f"{t.get('name', '')} {t.get('description', '')} {cols}"))
        overlap = len(q_terms & hay)
        if overlap == 0:
            continue
        table_id = t.get("id")
        out.append(
            {
                "type": "table",
                "table_id": table_id,
                "name": t.get("name"),
                "description": t.get("description"),
                "score": overlap / len(q_terms),
                "pivot_hint": (f"structured data — query with SQL via `agnes query`, table id: {table_id}"),
            }
        )
    out.sort(key=lambda h: (-h["score"], h["table_id"] or ""))
    return out


def unified_search(
    query: str,
    *,
    corpus_ids: List[str],
    user_groups: Optional[List[str]],
    granted_domains: Optional[List[str]],
    tables: List[Dict[str, Any]],
    k: int = 10,
) -> List[Dict[str, Any]]:
    """Fan the query out over all three sources and return one ranked list."""
    if not (query or "").strip():
        return []

    buckets: List[List[Dict[str, Any]]] = []

    chunk_hits = [dict(h, type="chunk") for h in _chunk_search(corpus_ids, query, k=k)] if corpus_ids else []
    buckets.append(chunk_hits)

    knowledge_hits: List[Dict[str, Any]] = []
    # None = privileged viewer (no filter); [] = zero grants → fail-closed.
    knowledge_enabled = user_groups is None or granted_domains is None or bool(user_groups or granted_domains)
    if knowledge_enabled:
        for rank, item in enumerate(
            _knowledge_search(
                query,
                exclude_personal=True,
                user_groups=user_groups,
                granted_domains=granted_domains,
                limit=k,
            )
        ):
            content = item.get("content") or ""
            knowledge_hits.append(
                {
                    "type": "knowledge",
                    "id": item.get("id"),
                    "title": item.get("title"),
                    "snippet": content[:280],
                    "domain": item.get("domain"),
                    # BM25 rank order only (the repo returns no score in the
                    # ILIKE fallback) — decay by position, top hit = 1.0.
                    "score": 1.0 / (1 + rank),
                }
            )
    buckets.append(knowledge_hits)
    buckets.append(_table_scores(query, tables)[:k] if tables else [])

    merged: List[Dict[str, Any]] = []
    for bucket in buckets:
        norms = _minmax([h["score"] for h in bucket])
        for h, n in zip(bucket, norms):
            merged.append({**h, "score": round(n, 4)})

    merged.sort(
        key=lambda h: (
            -h["score"],
            h["type"],
            str(h.get("chunk_id") or h.get("id") or h.get("table_id") or ""),
        )
    )
    return merged[:k]
