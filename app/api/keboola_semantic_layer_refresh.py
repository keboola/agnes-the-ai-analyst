"""Keboola semantic layer refresh — owner of the sync_semantic_layer() call path.

POST /api/admin/run-keboola-semantic-layer-refresh — called by the
scheduler container (auth: shared scheduler token resolves to a synthetic
admin user, same mechanism as app/api/bq_metadata_refresh.py) on the
SCHEDULER_KEBOOLA_SEMANTIC_LAYER_REFRESH_INTERVAL cadence. Also callable by
a real admin on demand.

Single-flight guarded (mirrors app/api/bq_metadata_refresh.py): a second
concurrent call while a sync is in flight gets 409 already_running instead
of racing a second Metastore fetch + upsert/prune pass against the same
metric_definitions rows.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.auth.access import require_admin
from connectors.keboola.semantic_layer import (
    MasterTokenRequiredError,
    sync_semantic_layer,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_refresh_lock = asyncio.Lock()
_refresh_state: dict[str, Any] = {"run_id": None, "started_at": None}


@router.post("/api/admin/run-keboola-semantic-layer-refresh")
async def run_keboola_semantic_layer_refresh(
    user: dict = Depends(require_admin),
):
    """Sync the configured Keboola project's semantic layer into
    metric_definitions. See connectors/keboola/semantic_layer.py for the
    mapping/prune logic.
    """
    if _refresh_lock.locked():
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "already_running",
                "run_id": _refresh_state.get("run_id"),
                "started_at": _refresh_state.get("started_at"),
                "hint": "A refresh is already in flight; this caller is a no-op.",
            },
        )

    async with _refresh_lock:
        run_id = uuid.uuid4().hex[:8]
        started_at = datetime.now(timezone.utc).isoformat()
        _refresh_state["run_id"] = run_id
        _refresh_state["started_at"] = started_at
        try:
            result = await asyncio.to_thread(sync_semantic_layer)
        except MasterTokenRequiredError as e:
            raise HTTPException(status_code=400, detail=str(e))
        finally:
            _refresh_state["run_id"] = None
            _refresh_state["started_at"] = None

    logger.info(
        "keboola semantic layer refresh: run_id=%s status=%s created_or_updated=%s "
        "pruned=%s skipped_unresolved_table=%s skipped_foreign_alias=%s "
        "skipped_embedded_comment=%s",
        run_id,
        result.get("status"),
        result.get("created_or_updated"),
        result.get("pruned"),
        result.get("skipped_unresolved_table"),
        result.get("skipped_foreign_alias"),
        result.get("skipped_embedded_comment"),
    )
    return {**result, "run_id": run_id, "started_at": started_at}
