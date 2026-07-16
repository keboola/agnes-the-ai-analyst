"""Hybrid retrieval over Collections chunks.

Brute-force at the current scale (dozens of files): fetch the candidate chunks
for the caller's *granted* corpora, score each by IDF-weighted lexical term
overlap and — when an embedding model is installed and the chunk was embedded —
cosine similarity, fuse the two, and return the top-k with citations.
Brute-force keeps the door open for an indexed strategy (DuckDB
``vss``/HNSW) later behind this same ``search`` interface.

RBAC is the caller's responsibility: pass only ``corpus_ids`` the caller may
access. Empty ``corpus_ids`` → empty result (fail-closed) — never "search all".

Scoring (#756 — tiny-corpus hybrid-search fix)
-----------------------------------------------
The naive "fraction of distinct query terms present" lexical score treats
every term as equally important, so any two chunks that happen to contain the
full query term set tie at exactly 1.0 — on a tiny corpus (a handful of
files, the common case for newly-created Collections) that tie is broken by
DB fetch order, not relevance. Fixed by:

1. IDF-weighting the lexical score over the in-memory candidate set (a chunk
   matching a term unique to it outweighs one matching only terms common to
   most candidates), normalized to the query's total IDF mass.
2. Min-max normalizing each component (lexical, cosine) across the candidate
   set before the fixed 0.5/0.5 blend, so a noisy/degenerate component can't
   silently dominate.
3. A stable ``(-score, chunk_id)`` sort key, so equal-scoring chunks resolve
   deterministically instead of by DB fetch order.
4. A calibrated ``confidence`` ("high"/"medium"/"low") derived from the
   top-vs-runner-up score margin and how many distinct source files the
   candidate set actually spans — a tiny corpus (few distinct files) or a
   thin margin can never earn "high", matching how little the ranking signal
   can be trusted at that scale.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional

from src.ingest.embeddings import embed_query, embedding_capability
from src.repositories import corpus_chunks_repo, corpus_files_repo

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Confidence calibration (see module docstring point 4). Deliberately
# conservative: issue #756 was filed because a 2-5 file corpus surfaced a
# wrong top match at what read as full confidence.
_MIN_FILES_FOR_MEDIUM = 3  # fewer distinct files than this → always "low"
_MIN_FILES_FOR_HIGH = 6  # fewer distinct files than this → capped at "medium"
_HIGH_MARGIN = 0.2  # top-vs-runner-up normalized-score gap required for "high"
_LOW_MARGIN = 0.05  # below this gap, ranking is effectively a toss-up → "low"


def retrieval_mode() -> str:
    """``"hybrid"`` when semantic scoring is active, else ``"lexical_only"``.

    Surfaces the silent lexical-only degradation (no ``agnes[embeddings]``
    extra installed → ``embed_query`` returns None → pure lexical ranking)
    as a response-level label. API/MCP search responses carry it as
    ``retrieval`` so a client can tell hybrid results from degraded ones
    without reading server logs (#898). Uses ``embedding_capability`` — a
    probe that never loads the model — so labeling a response can't force
    an expensive model init on requests where no ranking ran.
    """
    return "hybrid" if embedding_capability() else "lexical_only"


def _tokenize(text: str) -> List[str]:
    return _TOKEN_RE.findall((text or "").lower())


def _idf(doc_freq: int, n_candidates: int) -> float:
    """Smoothed IDF: ``ln((N+1)/(df+1)) + 1`` — always positive, never divides
    by zero, and degrades to a flat 1.0 weight for a term present in every
    candidate (no discriminating power) up to a high weight for a term unique
    to one candidate."""
    return math.log((n_candidates + 1) / (doc_freq + 1)) + 1.0


def _lexical_scores(q_terms: set[str], texts: List[str]) -> List[float]:
    """IDF-weighted lexical overlap for each candidate text, normalized to the
    query's total IDF mass (so the result stays roughly in ``[0, 1]``).

    Terms rare across the candidate set carry more weight than terms common
    to nearly every candidate, so a chunk matching one distinctive term
    outranks a chunk matching only common terms — the core #756 fix.
    """
    n = len(texts)
    if not q_terms or n == 0:
        return [0.0] * n
    tokensets = [set(_tokenize(t)) for t in texts]
    idf = {term: _idf(sum(1 for toks in tokensets if term in toks), n) for term in q_terms}
    total_mass = sum(idf.values()) or 1.0
    return [sum(idf[t] for t in q_terms if t in toks) / total_mass for toks in tokensets]


def _minmax_normalize(values: List[float]) -> List[float]:
    """Min-max normalize to ``[0, 1]`` across the candidate set.

    Degenerate cases (no values, or every value identical) can't divide by a
    zero range: an empty list normalizes to itself, and an all-equal set
    normalizes to 1.0 when the shared value is positive (a real, uniform
    signal) or 0.0 when it's all zero (no signal at all).
    """
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-12:
        return [1.0 if v > 0 else 0.0 for v in values]
    return [(v - lo) / (hi - lo) for v in values]


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _confidence(sorted_scores: List[float], distinct_files: int) -> str:
    """Calibrated confidence label for a ranked result set.

    Driven by two signals: the top-vs-runner-up normalized-score margin (a
    thin margin means the ranking is close to arbitrary) and how many
    distinct source files the candidate set spans (a tiny corpus can't
    reliably discriminate "the best" document regardless of margin — the
    #756 failure mode). A single-candidate result has no runner-up to
    compare against, so its own score stands in for the margin.
    """
    if not sorted_scores:
        return "low"
    margin = sorted_scores[0] - sorted_scores[1] if len(sorted_scores) > 1 else sorted_scores[0]
    if distinct_files < _MIN_FILES_FOR_MEDIUM:
        return "low"
    if margin >= _HIGH_MARGIN and distinct_files >= _MIN_FILES_FOR_HIGH:
        return "high"
    if margin >= _LOW_MARGIN:
        return "medium"
    return "low"


def rank_chunks(
    chunks: List[Dict[str, Any]],
    query: str,
    *,
    k: int = 10,
) -> tuple[List[tuple[float, Dict[str, Any]]], str]:
    """Score+rank a candidate chunk set (the #756 hybrid pipeline).

    Returns ``(top, confidence)`` where ``top`` is up to ``k``
    ``(score, chunk)`` pairs, sorted by fused score descending with a stable
    chunk-id tie-break. Pure scoring — no repo access, no RBAC, no filename
    resolution — so both the server's ``search()`` and the offline
    ``src.search.local`` reader can share the exact same ranking behavior
    over their respective candidate sets.
    """
    q_terms = set(_tokenize(query))
    q_vec: Optional[List[float]] = embed_query(query)  # None when extra absent

    # Raw, un-normalized components over the FULL candidate set (not just the
    # ones with a hit) — IDF needs the non-matching candidates to correctly
    # judge how rare/common each query term is, and the confidence
    # calibration needs the full distinct-file count for the corpus.
    texts = [ch.get("text", "") for ch in chunks]
    lex_raw = _lexical_scores(q_terms, texts)
    vec_raw = [0.0] * len(chunks)
    if q_vec is not None:
        for i, ch in enumerate(chunks):
            emb = ch.get("embedding")
            if emb:
                vec_raw[i] = max(0.0, _cosine(q_vec, list(emb)))

    lex_norm = _minmax_normalize(lex_raw)
    vec_norm = _minmax_normalize(vec_raw) if q_vec is not None else None

    fused: List[float] = []
    for i in range(len(chunks)):
        if vec_norm is not None:
            fused.append(0.5 * lex_norm[i] + 0.5 * vec_norm[i])
        else:
            fused.append(lex_norm[i])

    # Keep only candidates with a real signal (raw, not normalized — the
    # degenerate all-zero-becomes-uniform case in `_minmax_normalize` must
    # never resurrect a chunk with no actual lexical or vector match).
    scored = [(fused[i], ch) for i, ch in enumerate(chunks) if lex_raw[i] > 0 or vec_raw[i] > 0]
    # Deterministic ordering: fused score descending, then chunk id ascending
    # as a stable tie-break — no longer dependent on DB fetch order (#756).
    scored.sort(key=lambda x: (-x[0], str(x[1].get("id") or "")))

    distinct_files = len({ch.get("file_id") for ch in chunks})
    confidence = _confidence([s for s, _ in scored], distinct_files)

    return scored[:k], confidence


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

    top, confidence = rank_chunks(chunks, query, k=k)

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
                "confidence": confidence,
            }
        )
    return results
