"""Contradiction detection for corporate memory knowledge items.

Uses domain-based pre-filtering (DuckDB query) to find candidates,
then LLM-as-judge to determine if items actually contradict.
"""

import logging
from typing import Any, Optional

from connectors.llm import StructuredExtractor
from connectors.llm.exceptions import LLMError
from src.repositories.knowledge import KnowledgeRepository

from .prompts import CONTRADICTION_CHECK_PROMPT, CONTRADICTION_SCHEMA

logger = logging.getLogger(__name__)


def find_candidates(
    repo: KnowledgeRepository,
    new_item: dict,
    max_candidates: int = 10,
) -> list[dict]:
    """Find existing items that might contradict the new item.

    Pre-filters by domain and keyword match to avoid O(N) LLM calls.
    """
    domain = new_item.get("domain")
    title = new_item.get("title", "")
    title_words = [w for w in title.split() if len(w) > 3]
    item_id = new_item.get("id", "")

    return repo.find_contradiction_candidates(
        new_item_id=item_id,
        domain=domain,
        title_words=title_words,
        limit=max_candidates,
    )


def check_contradiction(
    extractor: StructuredExtractor,
    item_a: dict,
    item_b: dict,
) -> dict:
    """Use LLM to judge whether two items contradict each other.

    Returns dict with: contradicts (bool), explanation, severity, suggested_resolution
    """
    prompt = CONTRADICTION_CHECK_PROMPT.format(
        title_a=item_a.get("title", ""),
        content_a=item_a.get("content", ""),
        domain_a=item_a.get("domain", "unknown"),
        title_b=item_b.get("title", ""),
        content_b=item_b.get("content", ""),
        domain_b=item_b.get("domain", "unknown"),
    )

    try:
        result = extractor.extract_json(
            prompt=prompt,
            max_tokens=1024,
            json_schema=CONTRADICTION_SCHEMA,
            schema_name="contradiction_check",
        )
        return result
    except LLMError as e:
        logger.error("Contradiction check failed: %s", e)
        return {"contradicts": False, "explanation": f"Check failed: {e}"}


def check_contradictions(
    extractor: StructuredExtractor,
    new_item: dict,
    repo: KnowledgeRepository,
    max_candidates: int = 10,
) -> list[dict]:
    """Check a new item against existing items for contradictions.

    Returns list of contradiction records (empty if none found).
    Each record has: item_a_id, item_b_id, explanation, severity, suggested_resolution
    """
    candidates = find_candidates(repo, new_item, max_candidates)
    if not candidates:
        return []

    contradictions = []
    for candidate in candidates:
        result = check_contradiction(extractor, new_item, candidate)
        if result.get("contradicts"):
            contradiction = {
                "item_a_id": new_item["id"],
                "item_b_id": candidate["id"],
                "explanation": result.get("explanation", ""),
                "severity": result.get("severity"),
                "suggested_resolution": result.get("suggested_resolution"),
            }
            contradictions.append(contradiction)
            logger.info(
                "Contradiction detected: %s vs %s (%s)",
                new_item["id"], candidate["id"], result.get("severity", "unknown"),
            )

    return contradictions


def detect_and_record(
    extractor: StructuredExtractor,
    new_item: dict,
    repo: KnowledgeRepository,
    max_candidates: int = 10,
) -> list[str]:
    """Check for contradictions and record them in the database.

    Returns list of contradiction IDs created.
    """
    contradictions = check_contradictions(extractor, new_item, repo, max_candidates)
    contradiction_ids = []

    for c in contradictions:
        cid = repo.create_contradiction(
            item_a_id=c["item_a_id"],
            item_b_id=c["item_b_id"],
            explanation=c["explanation"],
            severity=c.get("severity"),
            suggested_resolution=c.get("suggested_resolution"),
        )
        contradiction_ids.append(cid)

    return contradiction_ids
