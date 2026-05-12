"""Seed the internal-source rows into ``table_registry`` at startup.

The internal connector has no extraction step (data lives in
``system.duckdb``), so the rows can't be created via the usual admin
``POST /api/admin/register-table`` flow. Instead, we idempotently insert
them on app boot — same pattern Agnes uses for the seeded ``Admin`` /
``Everyone`` groups.

Idempotency: ``TableRegistryRepository.register`` uses
``ON CONFLICT (id) DO UPDATE`` so re-running this on every startup just
re-applies the canonical description / display name (operators can't
accidentally edit the rows away).
"""

from __future__ import annotations

import logging

import duckdb

from connectors.internal.access import INTERNAL_TABLES
from src.repositories.table_registry import TableRegistryRepository

logger = logging.getLogger(__name__)


def ensure_internal_tables_registered(conn: duckdb.DuckDBPyConnection) -> None:
    """Insert / refresh the internal-source rows in ``table_registry``.

    Safe to call on every boot. Operators see these in /admin/tables
    flagged as ``source_type='internal'`` and can't accidentally delete
    them without an explicit admin action; the next boot puts them back.
    """
    repo = TableRegistryRepository(conn)
    for table in INTERNAL_TABLES:
        try:
            repo.register(
                id=table.registry_id,
                name=table.display_name,
                description=table.description,
                source_type="internal",
                bucket=None,
                source_table=table.source_table,
                query_mode="internal",
                profile_after_sync=False,
                registered_by="system_seed",
            )
        except Exception:
            # Logged but not fatal — startup must continue even if the
            # registry insert glitches (e.g. on a half-migrated DB).
            logger.exception(
                "ensure_internal_tables_registered: failed to register %s",
                table.registry_id,
            )
