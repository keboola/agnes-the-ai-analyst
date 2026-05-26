"""Seed the internal-source rows in ``table_registry``.

Idempotent — re-applies on every boot so operators can't accidentally
drift the canonical name/description, and stale rows are evicted when an
internal table is renamed (e.g. agnes_usage → agnes_telemetry).
"""

from __future__ import annotations

import logging

from connectors.internal.access import INTERNAL_TABLES
from src.repositories import table_registry_repo

logger = logging.getLogger(__name__)


def ensure_internal_tables_registered() -> None:
    """Insert / refresh the internal-source rows in ``table_registry``.

    Safe to call on every boot. Operators see these in /admin/tables
    flagged as ``source_type='internal'`` and can't accidentally delete
    them without an explicit admin action; the next boot puts them back.

    Also evicts stale internal-source rows whose id no longer matches
    ``INTERNAL_TABLES`` — used when an internal table is renamed.
    Without this the old row would linger in /catalog forever.
    """
    repo = table_registry_repo()
    canonical_ids = {t.registry_id for t in INTERNAL_TABLES}

    try:
        existing_internal = repo.list_by_source("internal")
        for row in existing_internal:
            rid = row.get("id")
            if rid and rid not in canonical_ids:
                repo.unregister(rid)
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
            logger.exception(
                "ensure_internal_tables_registered: failed to register %s",
                table.registry_id,
            )
