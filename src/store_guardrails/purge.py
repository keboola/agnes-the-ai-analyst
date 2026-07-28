"""TTL-based purge of blocked-bundle bytes.

Run daily by the scheduler. Walks every submission whose status is in
the terminal-blocked set AND whose `bundle_purged_at` is still NULL AND
whose `created_at` is older than the configured TTL, removes the bundle
directory from disk, drops the linked entity row, and stamps
`bundle_purged_at` on the submission row.

The submission row + SHA256 + size are intentionally preserved so
forensic correlation across the purge horizon still works.

Backend-agnostic: repositories are resolved from the ``src.repositories``
factory (mirrors ``src/store_guardrails/reaper.py``), so the sweep reads
and writes whichever backend (DuckDB or Postgres) the deployment runs on.
The pre-fix API took a raw DuckDB ``conn`` and ran the candidate-SELECT
against it directly, which silently found nothing on Postgres-backed
instances (submission rows live in PG, the conn pointed at an empty
local DuckDB).
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

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
    *,
    ttl_days: int,
    store_dir_resolver=None,
    now: Optional[datetime] = None,
    subs_repo: Any = None,
    ents_repo: Any = None,
) -> Dict[str, Any]:
    """Remove bundle bytes for terminal-blocked submissions older than TTL.

    Args:
        ttl_days: bundles whose ``created_at < now - ttl_days`` qualify.
            ``ttl_days <= 0`` short-circuits to a no-op so operators
            can disable cleanly without ripping the scheduler job.
        store_dir_resolver: callable returning the store-dir root.
            Defaults to ``app.utils.get_store_dir`` (lazy import to keep
            this module independent of the FastAPI layer for tests).
        now: clock injection for tests; defaults to ``datetime.now(UTC)``.
        subs_repo: injectable ``store_submissions`` repo (tests); defaults
            to ``store_submissions_repo()``.
        ents_repo: injectable ``store_entities`` repo (tests); defaults to
            ``store_entities_repo()``.

    Returns dict with ``purged`` (int) and ``ids`` (list[str]) so the
    admin endpoint can emit a sensible audit row.
    """
    if ttl_days <= 0:
        return {"purged": 0, "ids": [], "skipped": True}

    if store_dir_resolver is None:
        from app.utils import get_store_dir as _get_store
        store_dir_resolver = _get_store

    if subs_repo is None:
        from src.repositories import store_submissions_repo
        subs_repo = store_submissions_repo()
    if ents_repo is None:
        from src.repositories import store_entities_repo
        ents_repo = store_entities_repo()

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=int(ttl_days))

    candidates = subs_repo.find_purge_candidates(
        statuses=list(TERMINAL_BLOCKED_STATUSES),
        older_than=cutoff,
    )

    if not candidates:
        return {"purged": 0, "ids": []}

    store_root: Path = store_dir_resolver()

    purged_ids: List[str] = []
    for sub_id, entity_id in candidates:
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
                ents_repo.delete(entity_id)
            except Exception as e:
                logger.warning(
                    "purge: failed to delete entity %s for sub=%s: %s",
                    entity_id, sub_id, e,
                )
        # mark_bundle_purged also nulls entity_id on the submission row
        # so the admin UI shows the bundle is gone without orphaning a
        # foreign-key-shaped reference to a deleted entity.
        subs_repo.mark_bundle_purged(sub_id)
        purged_ids.append(sub_id)

    return {"purged": len(purged_ids), "ids": purged_ids}
