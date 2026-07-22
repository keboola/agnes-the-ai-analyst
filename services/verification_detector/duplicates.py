"""Duplicate-candidate detection + fuzzy dedup gate for the verification
detector.

Issue #62 — when a new knowledge item lands via the verification detector
pipeline, look for already-stored items in the same ``domain`` whose
``entities`` set overlaps significantly. A heuristic-only detector — no LLM
call — so it stays cheap to run inline after every item create.

Heuristic, per design decisions in issue #62:
  - Both items must share the same ``domain`` (NULL domain → no candidates).
  - Entity overlap >= ``MIN_ENTITY_OVERLAP`` (default 2). Below this the
    signal is dominated by generic terms and noise.
  - Similarity score = Jaccard ratio = |A ∩ B| / |A ∪ B| over the two
    entity sets. Persisted on the relation row for downstream sorting.

Personal items are excluded by the repository helper unconditionally — even
though the detector path itself only writes non-personal items today, the
``find_*`` helper enforces the privacy boundary so future callers can't
accidentally bypass it.

``find_duplicate_target`` (below) promotes this same entity-overlap heuristic
from an advisory-only hint into a real pre-insert dedup gate: the
verification detector's item id is an exact hash of (title, content), so any
paraphrase of an already-known fact hashes differently and would otherwise
land as a second PENDING item. It adds a lexical-similarity fallback for
items with too few shared entity tags for the Jaccard check to fire.
"""

import logging
import re
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

# ``repo`` is duck-typed — accepts both the DuckDB ``KnowledgeRepository``
# and the Postgres ``KnowledgePgRepository`` via the factory in
# ``src.repositories.knowledge_repo()``.

logger = logging.getLogger(__name__)

# Minimum number of shared entities for a duplicate-candidate hint.
# 2 is the lowest threshold where signal-to-noise stays acceptable on the
# 2-4-entity outputs the verification detector typically produces.
MIN_ENTITY_OVERLAP = 2

RELATION_TYPE = "likely_duplicate"

# Lexical-similarity fallback threshold, used by find_duplicate_target() when
# an item has too few entity tags for the Jaccard check above to fire.
# difflib.SequenceMatcher.ratio() over normalized "title\ncontent" text:
# 1.0 = identical, 0.0 = no overlap. 0.82 was picked to catch near-verbatim
# paraphrases (reworded sentences, synonym swaps) while staying clear of
# unrelated same-domain facts on the observed corpus — tunable, revisit if
# false positives/negatives show up in review or production.
LEXICAL_SIMILARITY_THRESHOLD = 0.82

# How many same-domain pending/approved items to scan for the lexical
# fallback. Matches find_duplicate_candidates_by_entities' own default so
# both signals see a comparably sized window.
_LEXICAL_CANDIDATE_LIMIT = 100

# Character-level SequenceMatcher.ratio() is noisy on short strings — a
# handful of matching characters in a 15-character string produces the same
# high ratio as a genuine paraphrase of a full sentence. Real verification
# titles+content are full sentences (well over this length); skip the
# lexical check below it rather than risk merging two short, unrelated
# facts.
_MIN_TEXT_LENGTH_FOR_LEXICAL_MATCH = 40


def _normalize_text(title: Optional[str], content: Optional[str]) -> str:
    """Lowercase + collapse whitespace so wording/formatting noise doesn't
    dominate the SequenceMatcher ratio."""
    text = f"{title or ''}\n{content or ''}".lower()
    return re.sub(r"\s+", " ", text).strip()


def _choose_canonical(candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Deterministically pick one canonical item among several duplicate
    candidates: approved beats pending, required beats not, then the oldest
    ``created_at``, then the lowest id as a final tie-break."""

    def sort_key(item: Dict[str, Any]) -> tuple:
        status_rank = 0 if item.get("status") == "approved" else 1
        required_rank = 0 if item.get("is_required") else 1
        return (
            status_rank,
            required_rank,
            str(item.get("created_at") or ""),
            str(item.get("id") or ""),
        )

    return sorted(candidates, key=sort_key)[0]


def find_duplicate_target(
    repo,
    *,
    item_id: str,
    title: str,
    content: str,
    domain: Optional[str],
    entities: Optional[List[str]],
) -> Optional[Dict[str, Any]]:
    """Look for an existing same-domain item that is effectively the same
    fact as ``(title, content)``, for a prospective verification item whose
    exact-hash id (``item_id``) did not already exist.

    Either of two independent signals is sufficient:

    1. Entity-tag Jaccard overlap >= ``MIN_ENTITY_OVERLAP``, via the existing
       ``find_duplicate_candidates_by_entities`` heuristic.
    2. Lexical title+content similarity >= ``LEXICAL_SIMILARITY_THRESHOLD``,
       for items with too few entity tags for (1) to fire.

    Returns the canonical existing item to attach evidence to, or ``None``
    if no strong duplicate was found — the caller should create a new row.
    """
    if not domain:
        return None

    candidates: Dict[str, Dict[str, Any]] = {}

    if entities:
        for cand in repo.find_duplicate_candidates_by_entities(
            new_item_id=item_id,
            entities=entities,
            domain=domain,
            min_overlap=MIN_ENTITY_OVERLAP,
        ):
            cand_id = cand.get("id")
            if cand_id:
                candidates[cand_id] = cand

    if not candidates:
        # Entity-overlap signal found nothing (or there were too few/no
        # entity tags to try) — fall back to lexical similarity over the
        # same-domain candidate pool.
        normalized_new = _normalize_text(title, content)
        if len(normalized_new) >= _MIN_TEXT_LENGTH_FOR_LEXICAL_MATCH:
            for cand in repo.list_by_domain(domain, statuses=["approved", "pending"], limit=_LEXICAL_CANDIDATE_LIMIT):
                cand_id = cand.get("id")
                if not cand_id or cand_id == item_id:
                    continue
                if cand.get("is_personal"):
                    continue
                normalized_cand = _normalize_text(cand.get("title"), cand.get("content"))
                if len(normalized_cand) < _MIN_TEXT_LENGTH_FOR_LEXICAL_MATCH:
                    continue
                ratio = SequenceMatcher(None, normalized_new, normalized_cand).ratio()
                if ratio >= LEXICAL_SIMILARITY_THRESHOLD:
                    cand["lexical_ratio"] = ratio
                    candidates[cand_id] = cand

    if not candidates:
        return None
    return _choose_canonical(list(candidates.values()))


def _record_duplicate_candidates(
    repo,
    new_item: dict,
) -> int:
    """Record duplicate-candidate relations for ``new_item``.

    Returns the number of relation rows created. Skips silently when
    ``new_item`` lacks a domain or entities — these items can't participate
    in the entity-overlap heuristic so there's nothing to record.
    """
    item_id: Optional[str] = new_item.get("id")
    if not item_id:
        return 0

    entities = new_item.get("entities")
    if isinstance(entities, str):
        # The repo round-trips ``entities`` as JSON; tolerate either shape.
        import json

        try:
            entities = json.loads(entities)
        except json.JSONDecodeError:
            entities = None

    if not entities or not isinstance(entities, list):
        return 0

    domain = new_item.get("domain")
    if not domain:
        return 0

    candidates = repo.find_duplicate_candidates_by_entities(
        new_item_id=item_id,
        entities=entities,
        domain=domain,
        min_overlap=MIN_ENTITY_OVERLAP,
    )

    recorded = 0
    for cand in candidates:
        cand_id = cand.get("id")
        if not cand_id:
            continue
        try:
            repo.create_relation(
                item_a_id=item_id,
                item_b_id=cand_id,
                relation_type=RELATION_TYPE,
                score=cand.get("jaccard"),
            )
            recorded += 1
        except Exception as e:  # pragma: no cover - defensive, ON CONFLICT swallows dups
            logger.warning(
                "Failed to record duplicate-candidate relation %s <-> %s: %s",
                item_id,
                cand_id,
                e,
            )
    return recorded
