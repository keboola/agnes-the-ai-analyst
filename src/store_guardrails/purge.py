"""TTL-based purge of blocked-bundle bytes.

Run daily by the scheduler. Walks every submission whose status is in
the terminal-blocked set AND whose `bundle_purged_at` is still NULL AND
whose `created_at` is older than the configured TTL, removes the bundle
directory from disk, drops the linked entity row, and stamps
`bundle_purged_at` on the submission row.

The submission row + SHA256 + size are intentionally preserved so
forensic correlation across the purge horizon still works.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import duckdb

from src.repositories.store_entities import StoreEntitiesRepository
from src.repositories.store_submissions import StoreSubmissionsRepository

logger = logging.getLogger(__name__)


# Statuses considered "terminal blocked" — bundle is no longer needed to
# serve the user, but admins can still want it for forensics. Excludes
# `approved` (live entity, never purge), `overridden` (admin already
# decided to publish), and `pending_*` (still in review).
#
# Inline-tier failures on the upload path are hard-rejected and never
# create rows here. The only writer of `blocked_inline` post-v30 is
# `admin_rescan_store_submission` — an admin-initiated rescan that
# re-fails inline produces a `blocked_inline` row pointing at the
# already-quarantined bundle. Sweeping these here matches operator
# expectation: an admin Rescan should not cause a previously-purged
# bundle to outlive its TTL just because the verdict changed.
TERMINAL_BLOCKED_STATUSES = (
    "blocked_inline",
    "blocked_llm",
    "review_error",
)


def purge_blocked_bundles(
    conn: duckdb.DuckDBPyConnection,
    *,
    ttl_days: int,
    store_dir_resolver=None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Remove bundle bytes for terminal-blocked submissions older than TTL.

    Args:
        conn: DuckDB system handle.
        ttl_days: bundles whose ``created_at < now - ttl_days`` qualify.
            ``ttl_days <= 0`` short-circuits to a no-op so operators
            can disable cleanly without ripping the scheduler job.
        store_dir_resolver: callable returning the store-dir root.
            Defaults to ``app.utils.get_store_dir`` (lazy import to keep
            this module independent of the FastAPI layer for tests).
        now: clock injection for tests; defaults to ``datetime.now(UTC)``.

    Returns dict with ``purged`` (int) and ``ids`` (list[str]) so the
    admin endpoint can emit a sensible audit row.
    """
    if ttl_days <= 0:
        return {"purged": 0, "ids": [], "skipped": True}

    if store_dir_resolver is None:
        from app.utils import get_store_dir as _get_store
        store_dir_resolver = _get_store

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=int(ttl_days))

    placeholders = ",".join("?" for _ in TERMINAL_BLOCKED_STATUSES)
    rows = conn.execute(
        f"""SELECT id, entity_id FROM store_submissions
            WHERE status IN ({placeholders})
              AND bundle_purged_at IS NULL
              AND created_at < ?""",
        list(TERMINAL_BLOCKED_STATUSES) + [cutoff],
    ).fetchall()

    if not rows:
        return {"purged": 0, "ids": []}

    subs = StoreSubmissionsRepository(conn)
    ents = StoreEntitiesRepository(conn)
    store_root: Path = store_dir_resolver()

    purged_ids: List[str] = []
    for sub_id, entity_id in rows:
        if entity_id:
            entity_dir = store_root / entity_id
            try:
                if entity_dir.exists():
                    shutil.rmtree(entity_dir, ignore_errors=True)
            except OSError as e:
                logger.warning(
                    "purge: failed to rmtree %s for sub=%s: %s",
                    entity_dir, sub_id, e,
                )
            try:
                ents.delete(entity_id)
            except Exception as e:
                logger.warning(
                    "purge: failed to delete entity %s for sub=%s: %s",
                    entity_id, sub_id, e,
                )
        # mark_bundle_purged also nulls entity_id on the submission row
        # so the admin UI shows the bundle is gone without orphaning a
        # foreign-key-shaped reference to a deleted entity.
        subs.mark_bundle_purged(sub_id)
        purged_ids.append(sub_id)

    return {"purged": len(purged_ids), "ids": purged_ids}
