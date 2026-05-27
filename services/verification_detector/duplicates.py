"""Duplicate-candidate detection hook for the verification detector.

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
"""

import logging
from typing import Optional

from src.repositories.knowledge import KnowledgeRepository

logger = logging.getLogger(__name__)

# Minimum number of shared entities for a duplicate-candidate hint.
# 2 is the lowest threshold where signal-to-noise stays acceptable on the
# 2-4-entity outputs the verification detector typically produces.
MIN_ENTITY_OVERLAP = 2

RELATION_TYPE = "likely_duplicate"


def _record_duplicate_candidates(
    repo: KnowledgeRepository,
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
                item_id, cand_id, e,
            )
    return recorded
