"""Entity resolution v1 for corporate memory.

Simple case-insensitive string matching against a static entity registry.
Runs as post-processing on new knowledge items to tag them with recognized entities.
"""

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def build_entity_registry(
    groups: dict[str, Any] | None = None,
    domain_owners: dict[str, list[str]] | None = None,
    entity_config: dict[str, list[str]] | None = None,
    metric_names: list[str] | None = None,
) -> dict[str, list[str]]:
    """Build a flat entity registry from various config sources.

    Returns dict mapping category -> list of entity names.
    """
    registry: dict[str, list[str]] = {}

    if groups:
        registry["teams"] = list(groups.keys())

    if domain_owners:
        registry["domains"] = list(domain_owners.keys())

    if entity_config:
        for category, entities in entity_config.items():
            registry[category] = entities

    if metric_names:
        existing_metrics = registry.get("metrics", [])
        registry["metrics"] = list(set(existing_metrics + metric_names))

    return registry


def resolve_entities(
    content: str,
    title: str,
    entity_registry: dict[str, list[str]],
) -> list[str]:
    """Find entity matches in title and content using case-insensitive substring matching.

    Returns deduplicated list of matched entity names.
    """
    text = f"{title} {content}".lower()
    matched: set[str] = set()

    for _category, entities in entity_registry.items():
        for entity in entities:
            if entity.lower() in text:
                matched.add(entity)

    return sorted(matched)


def resolve_and_merge(
    item: dict,
    entity_registry: dict[str, list[str]],
) -> list[str]:
    """Resolve entities for an item and merge with any existing entity tags.

    Returns combined deduplicated entity list.
    """
    existing = item.get("entities") or []
    if isinstance(existing, str):
        try:
            existing = json.loads(existing)
        except (json.JSONDecodeError, TypeError):
            existing = []

    resolved = resolve_entities(
        content=item.get("content", ""),
        title=item.get("title", ""),
        entity_registry=entity_registry,
    )

    combined = set(existing) | set(resolved)
    return sorted(combined)
