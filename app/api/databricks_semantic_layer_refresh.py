"""Databricks semantic layer refresh — owner of the sync_semantic_layer() call path.

POST /api/admin/run-databricks-semantic-layer-refresh — called by the
scheduler container (auth: shared scheduler token resolves to a synthetic
admin user, same mechanism as app/api/keboola_semantic_layer_refresh.py) on
the SCHEDULER_DATABRICKS_SEMANTIC_LAYER_REFRESH_INTERVAL cadence. Also
callable by a real admin on demand.

Single-flight guarded (mirrors the Keboola sibling): a second concurrent
call while a sync is in flight gets 409 already_running instead of racing a
second warehouse fetch + upsert/prune pass against the same
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
from connectors.databricks.semantic_layer import sync_semantic_layer

logger = logging.getLogger(__name__)
router = APIRouter()

_refresh_lock = asyncio.Lock()
_refresh_state: dict[str, Any] = {
    "run_id": None,
    "started_at": None,
    "last_completed_at": None,
    "last_status": None,
    "last_result": None,
}


def get_last_refresh_summary() -> dict[str, Any]:
    """Read accessor for the admin UI — the last completed run's summary."""
    return {
        "last_completed_at": _refresh_state.get("last_completed_at"),
        "last_status": _refresh_state.get("last_status"),
        "last_result": _refresh_state.get("last_result"),
    }


# Error codes `sync_semantic_layer` attaches to a returned {"status": "error"}
# and the HTTP status each deserves — 400 when the admin can fix it, 502 when
# the upstream is genuinely unreachable or broken. Unmapped codes stay 502.
_ERROR_CODE_STATUS = {
    "credentials_not_configured": 400,
    "upstream_client_error": 400,
    "upstream_error": 502,
}


def _status_for_error_code(code: Any) -> int:
    return _ERROR_CODE_STATUS.get(code, 502) if isinstance(code, str) else 502


def _record_completion(status: str, result: Any) -> None:
    _refresh_state["last_completed_at"] = datetime.now(timezone.utc).isoformat()
    _refresh_state["last_status"] = status
    _refresh_state["last_result"] = result


@router.post("/api/admin/run-databricks-semantic-layer-refresh")
async def run_databricks_semantic_layer_refresh(
    user: dict = Depends(require_admin),
):
    """Sync the configured Databricks workspace's Unity Catalog metric views
    into metric_definitions. See connectors/databricks/semantic_layer.py for
    the mapping/prune logic.

    409 if a sync is already in flight. 400 when the sync fails for a reason
    the admin controls — Databricks not configured, or a request the
    workspace refuses (4xx). 502 only when the upstream is unreachable or
    answers 5xx.
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
        except Exception as e:
            _record_completion("error", str(e))
            raise
        else:
            # Config/upstream failures arrive as a returned {"status": "error"}
            # dict rather than an exception — record and surface them the same
            # way, or the admin UI shows a false "OK" after a failed sync.
            if result.get("status") == "error":
                message = result.get("error", "Databricks semantic layer sync failed")
                _record_completion("error", message)
                raise HTTPException(status_code=_status_for_error_code(result.get("code")), detail=message)
            _record_completion("ok", result)
        finally:
            _refresh_state["run_id"] = None
            _refresh_state["started_at"] = None

    logger.info(
        "databricks semantic layer refresh: run_id=%s status=%s created_or_updated=%s "
        "pruned=%s metric_views_seen=%s skipped_unparseable=%s skipped_conflict=%s",
        run_id,
        result.get("status"),
        result.get("created_or_updated"),
        result.get("pruned"),
        result.get("metric_views_seen"),
        result.get("skipped_unparseable"),
        result.get("skipped_conflict"),
    )
    return {**result, "run_id": run_id, "started_at": started_at}
