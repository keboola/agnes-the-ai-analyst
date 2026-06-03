"""Demo-seed helpers: populate a fresh instance with the bundled sample
content so a brand-new deployment has something to explore out of the box.

All seeders are idempotent — safe to call on every boot.
"""

import json
import logging
from pathlib import Path

from src.repositories.knowledge import KnowledgeRepository
from src.repositories.memory_domains import MemoryDomainsRepository
from src.repositories.metrics import MetricRepository

log = logging.getLogger(__name__)

_METRICS_DIR = Path(__file__).resolve().parent.parent / "docs" / "metrics"
_MEMORY_FIXTURE = Path(__file__).resolve().parent / "_demo_seed" / "memory_items.json"


def seed_metrics(conn) -> int:
    """Import bundled metric definitions from ``docs/metrics/``.

    Idempotent across boots: ``MetricsRepository.import_from_yaml`` upserts each
    metric keyed on its ``category/name`` id (``INSERT ... ON CONFLICT (id) DO
    UPDATE``), so re-running replaces rather than duplicating.

    Returns the number of metrics imported on this call.
    """
    count = MetricRepository(conn).import_from_yaml(_METRICS_DIR)
    log.info("seed_metrics: imported %d metric definitions from %s", count, _METRICS_DIR)
    return count


def seed_memory(conn) -> int:
    """Seed bundled corporate-memory domains + knowledge items from
    ``src/_demo_seed/memory_items.json`` directly into the system DB.

    Idempotent across boots: neither ``MemoryDomainsRepository.create`` nor
    ``KnowledgeRepository.create`` upserts, so we existence-check first —
    domains by ``slug`` (``SELECT id FROM memory_domains WHERE slug = ?``,
    matching the slug-uniqueness the table enforces) and items by ``id``
    (``SELECT 1 FROM knowledge_items WHERE id = ?``). Domains are created
    before items because ``create(..., domain=<slug>)`` routes through the
    junction and requires the slug to already exist.

    Each row is wrapped in its own try/except so one bad row logs a warning
    and is skipped rather than aborting boot. Returns the number of knowledge
    items created on this call.
    """
    payload = json.loads(_MEMORY_FIXTURE.read_text())
    domains_repo = MemoryDomainsRepository(conn)
    knowledge_repo = KnowledgeRepository(conn)

    domains_created = 0
    for domain in payload.get("domains", []):
        slug = domain.get("slug")
        try:
            exists = conn.execute(
                "SELECT id FROM memory_domains WHERE slug = ?", [slug]
            ).fetchone()
            if exists:
                continue
            domains_repo.create(
                name=domain["name"],
                slug=slug,
                description=domain.get("description"),
                icon=domain.get("icon"),
                color=domain.get("color"),
                created_by="system",
            )
            domains_created += 1
        except Exception:  # noqa: BLE001 — tolerate one bad row, keep booting
            log.warning("seed_memory: failed to seed domain %r", slug, exc_info=True)

    items_created = 0
    for item in payload.get("items", []):
        item_id = item.get("id")
        try:
            exists = conn.execute(
                "SELECT 1 FROM knowledge_items WHERE id = ?", [item_id]
            ).fetchone()
            if exists:
                continue
            knowledge_repo.create(
                item_id,
                item["title"],
                item["content"],
                item["category"],
                status=item.get("status", "approved"),
                is_required=bool(item.get("mandated", False)),
                domain=item.get("domain"),
                source_user="system",
                added_by="system",
            )
            items_created += 1
        except Exception:  # noqa: BLE001 — tolerate one bad row, keep booting
            log.warning("seed_memory: failed to seed item %r", item_id, exc_info=True)

    log.info(
        "seed_memory: created %d domains, %d knowledge items from %s",
        domains_created,
        items_created,
        _MEMORY_FIXTURE,
    )
    return items_created
