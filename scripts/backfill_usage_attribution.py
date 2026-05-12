"""Backfill usage_attribution_* tables from existing curated marketplaces + store entities.

Run once on first deploy of telemetry. Idempotent — each plugin / entity
is re-exploded via UsageAttributionRepository.replace_for_*, so re-running
on a populated DB just resets the attribution rows for each known plugin.

Usage::

    python scripts/backfill_usage_attribution.py

The script reads DATA_DIR from the environment (defaulting to ``/data``) and
opens system.duckdb in read-write mode.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from app.utils import get_marketplaces_dir, get_store_dir
from src.db import get_system_db
from src.marketplace_listing import list_commands, list_inner_agents, list_inner_skills
from src.repositories.marketplace_plugins import MarketplacePluginsRepository
from src.repositories.store_entities import StoreEntitiesRepository
from src.repositories.usage_attribution import UsageAttributionRepository

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def _backfill_curated(conn, attr: UsageAttributionRepository) -> int:
    """Walk marketplace_plugins rows and explode each plugin's clone."""
    plugins_repo = MarketplacePluginsRepository(conn)
    all_plugins = plugins_repo.list_all()
    marketplaces_dir = get_marketplaces_dir()
    n_ok = 0
    for plugin in all_plugins:
        marketplace_id = plugin.get("marketplace_id") or ""
        plugin_name = plugin.get("name") or ""
        if not marketplace_id or not plugin_name:
            continue
        plugin_root = marketplaces_dir / marketplace_id / "plugins" / plugin_name
        try:
            skills = list_inner_skills(plugin_root)
            agents = list_inner_agents(plugin_root)
            commands = list_commands(plugin_root)
            attr.replace_for_curated(
                marketplace_id, plugin_name,
                skills=skills, agents=agents, commands=commands,
            )
            log.debug(
                "curated %s/%s: skills=%d agents=%d commands=%d",
                marketplace_id, plugin_name, len(skills), len(agents), len(commands),
            )
            n_ok += 1
        except Exception:
            log.exception(
                "curated explode failed for %s/%s", marketplace_id, plugin_name
            )
    return n_ok


def _backfill_flea(conn, attr: UsageAttributionRepository) -> int:
    """Walk active store_entities rows and explode each entity."""
    entities_repo = StoreEntitiesRepository(conn)
    # list() with no visibility filter returns everything; we want non-archived
    # approved entities as the "live" set to attribute.
    items, _ = entities_repo.list(
        limit=10_000,
        visibility_status=["approved"],
    )
    store_dir = get_store_dir()
    n_ok = 0
    for entity in items:
        entity_id = entity.get("id") or ""
        entity_type = entity.get("type") or ""
        entity_name = entity.get("name") or ""
        if not entity_id:
            continue
        try:
            plugin_dir = store_dir / entity_id / "plugin"
            if entity_type == "skill":
                attr.replace_for_flea(entity_id, skills=[entity_name])
            elif entity_type == "agent":
                attr.replace_for_flea(entity_id, agents=[entity_name])
            elif entity_type == "plugin" and plugin_dir.is_dir():
                skills = list_inner_skills(plugin_dir)
                agents = list_inner_agents(plugin_dir)
                commands = list_commands(plugin_dir)
                attr.replace_for_flea(
                    entity_id, skills=skills, agents=agents, commands=commands,
                )
            else:
                # Unknown type or plugin without on-disk bundle — best-effort.
                attr.replace_for_flea(entity_id, skills=[entity_name])
            n_ok += 1
        except Exception:
            log.exception("flea explode failed for entity %s", entity_id)
    return n_ok


def main() -> int:
    conn = get_system_db()
    attr = UsageAttributionRepository(conn)

    log.info("backfill_usage_attribution: starting curated pass")
    n_curated = _backfill_curated(conn, attr)

    log.info("backfill_usage_attribution: starting flea pass")
    n_flea = _backfill_flea(conn, attr)

    log.info(
        "backfill complete: curated=%d flea=%d", n_curated, n_flea,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
