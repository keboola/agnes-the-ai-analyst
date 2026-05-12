"""Orchestration helpers that bridge store_entities rows to attribution rows.

Knows the bundle-vs-single-row fallback semantics that
``UsageAttributionRepository`` itself is too low-level to express.
Called from ``app.api.store``, ``app.api.admin``, and
``src.store_guardrails.runner``.

All public functions are best-effort: failures are logged and never
re-raised.  Attribution may briefly lag entity writes on crash — it runs
independently of the entity write (no shared transaction).
"""
from __future__ import annotations

import logging
from pathlib import Path

import duckdb

from src.marketplace_listing import list_commands, list_inner_agents, list_inner_skills
from src.repositories.usage_attribution import UsageAttributionRepository

logger = logging.getLogger(__name__)


def _plugin_dir(entity_id: str) -> Path:
    """Return the live plugin directory for a store entity.

    Mirrors the private ``_plugin_dir`` in ``app.api.store`` without
    importing from the FastAPI layer.
    """
    from app.utils import get_store_dir
    return get_store_dir() / entity_id / "plugin"


def update_flea_attribution(
    conn: duckdb.DuckDBPyConnection,
    entity_id: str,
    entity_type: str,
    entity_name: str,
) -> None:
    """Refresh attribution rows for a flea entity.

    For ``type='plugin'`` walks the baked plugin tree to enumerate skills,
    agents, and commands.  For ``type='skill'`` / ``type='agent'`` the baked
    tree contains exactly one component, so falls back to the entity name
    directly (single-row attribution).

    Best-effort — failures are logged and never re-raised.  Runs
    independently of the entity write (no shared transaction).  Attribution
    may briefly lag entity writes on crash.
    """
    try:
        attr = UsageAttributionRepository(conn)
        plugin_dir = _plugin_dir(entity_id)

        if entity_type == "skill":
            attr.replace_for_flea(entity_id, skills=[entity_name])
        elif entity_type == "agent":
            attr.replace_for_flea(entity_id, agents=[entity_name])
        elif entity_type == "plugin":
            if plugin_dir.is_dir():
                skills = list_inner_skills(plugin_dir)
                agents = list_inner_agents(plugin_dir)
                commands = list_commands(plugin_dir)
                attr.replace_for_flea(
                    entity_id, skills=skills, agents=agents, commands=commands,
                )
            else:
                # Bundle path not yet on disk — record name as a skill as a
                # best-effort fallback until the bundle lands.
                attr.replace_for_flea(entity_id, skills=[entity_name])
        else:
            # Unknown type — record name as a skill as a best-effort fallback.
            logger.warning(
                "update_flea_attribution: unknown type %r for entity %s — recording as skill",
                entity_type, entity_id,
            )
            attr.replace_for_flea(entity_id, skills=[entity_name])
    except Exception:  # noqa: BLE001
        logger.exception(
            "flea attribution update failed for entity %s (type=%s); continuing",
            entity_id, entity_type,
        )


def delete_flea_attribution(
    conn: duckdb.DuckDBPyConnection,
    entity_id: str,
) -> None:
    """Remove usage-attribution rows for a deleted / archived flea entity.

    Best-effort — failures are logged and never re-raised.
    """
    try:
        UsageAttributionRepository(conn).delete_for_flea(entity_id)
    except Exception:  # noqa: BLE001
        logger.exception(
            "flea attribution delete failed for entity %s; continuing", entity_id,
        )
