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

    Also evicts stale internal-source rows whose id no longer matches
    ``INTERNAL_TABLES`` — used when an internal table is renamed
    (e.g. agnes_usage → agnes_telemetry). Without this the old row
    would linger in /catalog forever.
    """
    repo = TableRegistryRepository(conn)
    canonical_ids = {t.registry_id for t in INTERNAL_TABLES}
    placeholders = ",".join("?" for _ in canonical_ids) if canonical_ids else "''"
    try:
        conn.execute(
            f"DELETE FROM table_registry "
            f"WHERE source_type = 'internal' AND id NOT IN ({placeholders})",
            list(canonical_ids),
        )
    except Exception:
        logger.exception(
            "ensure_internal_tables_registered: stale-row cleanup failed; "
            "renamed internal tables may still appear under their old ids"
        )
    for table in INTERNAL_TABLES:
        try:
            repo.register(
                id=table.registry_id,
                name=table.display_name,
                description=table.description,
                source_type="internal",
                # `bucket` is the grouping key /catalog uses for accordion
                # category headers — displayed verbatim, so a more
                # readable string than the lowercase "agnes" goes
                # straight onto the page. The three internal tables
                # land under "Agnes Internal" on Data Packages instead
                # of the catch-all "default".
                bucket="Agnes Internal",
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
