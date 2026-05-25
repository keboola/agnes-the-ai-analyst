"""Cache warmup framework — populates catalog/schema/metadata caches at
container startup so the first analyst hits warm caches.

Bounded concurrency (4 by default). Exposes:
  - GET /api/admin/cache-warmup/status — JSON snapshot
  - POST /api/admin/cache-warmup/run — manual trigger (idempotent)
  - GET /api/admin/cache-warmup/stream — Server-Sent Events
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends
from sse_starlette.sse import EventSourceResponse

from app.auth.access import require_admin

from src.repositories import (
    table_registry_repo,
)
logger = logging.getLogger(__name__)
router = APIRouter()


@dataclass
class WarmupRowState:
    table_id: str
    status: Literal["pending", "warming", "fresh", "error"]
    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: int | None = None
    error: str | None = None
    last_warmed_at: str | None = None


@dataclass
class WarmupRunState:
    run_id: str
    trigger: Literal["startup", "manual", "registry_change"]
    started_at: str
    completed_at: str | None = None
    total: int = 0
    completed: int = 0
    failed: int = 0
    rows: dict[str, WarmupRowState] = field(default_factory=dict)
    _subscribers: list[asyncio.Queue] = field(default_factory=list, repr=False)


WARMUP_STATE: WarmupRunState | None = None
_RUN_LOCK = asyncio.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def maybe_schedule_startup_warmup() -> None:
    """Called from app/main.py FastAPI startup event."""
    if os.environ.get("AGNES_SKIP_CACHE_WARMUP") == "1":
        logger.info("cache warmup skipped (AGNES_SKIP_CACHE_WARMUP=1)")
        return
    try:
        asyncio.create_task(_warm_catalog_caches_bg(trigger="startup"))
    except RuntimeError:
        logger.warning("no running event loop — startup warmup skipped")


async def _warm_catalog_caches_bg(
    trigger: str = "startup", state: WarmupRunState | None = None,
) -> None:
    """Walk registry, warm metadata + schema caches for every remote row.

    If `state` is provided, use it (caller has already published it on
    WARMUP_STATE). Otherwise build a fresh state and assign WARMUP_STATE.
    """
    global WARMUP_STATE
    if state is None:
        async with _RUN_LOCK:
            # Re-check inside the lock — another caller might have completed
            # a run while we were waiting.
            if WARMUP_STATE and WARMUP_STATE.completed_at is None:
                return
            state = WarmupRunState(
                run_id=uuid4().hex[:8],
                trigger=trigger,
                started_at=_now_iso(),
            )
            WARMUP_STATE = state

    run_id = state.run_id
    rows = _list_remote_rows()
    state.total = len(rows)
    for r in rows:
        state.rows[r["id"]] = WarmupRowState(
            table_id=r["id"], status="pending",
        )
    _broadcast(state, {"event": "start", "data": {
        "run_id": run_id, "trigger": trigger, "total": state.total,
    }})

    sem = asyncio.Semaphore(int(os.environ.get("AGNES_WARMUP_CONCURRENCY", "4")))
    await asyncio.gather(
        *(_warm_one(r, state, sem) for r in rows), return_exceptions=True,
    )

    state.completed_at = _now_iso()
    _broadcast(state, {"event": "complete", "data": {
        "run_id": run_id, "total": state.total,
        "completed": state.completed, "failed": state.failed,
    }})
    logger.info(
        "cache warmup complete: run_id=%s total=%d ok=%d fail=%d",
        run_id, state.total, state.completed, state.failed,
    )


def _list_remote_rows() -> list[dict]:
    """Snapshot of registry rows that need a warmup pass."""
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository
    conn = get_system_db()
    rows = table_registry_repo().list_all()
    return [
        r for r in rows
        if r.get("query_mode") == "remote" and r.get("source_type") == "bigquery"
    ]


async def _warm_one(
    row: dict, state: WarmupRunState, sem: asyncio.Semaphore,
) -> None:
    async with sem:
        rs = state.rows[row["id"]]
        rs.status = "warming"
        rs.started_at = _now_iso()
        _broadcast(state, {"event": "row", "data": asdict(rs)})
        t0 = time.monotonic()
        try:
            await asyncio.to_thread(_warm_metadata_sync, row)
            await asyncio.to_thread(_warm_schema_sync, row)
            rs.status = "fresh"
            rs.last_warmed_at = _now_iso()
            state.completed += 1
        except Exception as e:
            rs.status = "error"
            rs.error = str(e)
            state.failed += 1
            logger.warning("cache warmup row=%s failed: %s", row["id"], e)
        finally:
            rs.completed_at = _now_iso()
            rs.duration_ms = int((time.monotonic() - t0) * 1000)
            _broadcast(state, {"event": "row", "data": asdict(rs)})


def _warm_metadata_sync(row: dict) -> None:
    """Refresh the persistent ``bq_metadata_cache`` row.

    Pre-0.50 this called ``v2_catalog._size_hint_for_row`` to populate
    an in-memory TTL cache. The in-memory cache is gone — metadata now
    lives in DuckDB, owned by ``app/api/bq_metadata_refresh.refresh_one``
    (the same primitive the scheduler-driven refresh uses).
    """
    from app.api.bq_metadata_refresh import refresh_one
    from src.db import get_system_db
    refresh_one(get_system_db(), row)


def _warm_schema_sync(row: dict) -> None:
    """Trigger schema cache populate via build_schema_uncached."""
    from app.api.v2_schema import build_schema_uncached
    from connectors.bigquery.access import get_bq_access
    from src.db import get_system_db
    bq = get_bq_access()
    build_schema_uncached(get_system_db(), row["id"], bq=bq, row=row)


async def warm_one_table(table_id: str) -> None:
    """Single-row re-warm — invoked by `invalidate_for_table` after a
    registry change. Does NOT update WARMUP_STATE (small change shouldn't
    overwrite the last full run's status); just refreshes the caches."""
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository
    conn = get_system_db()
    row = table_registry_repo().get(table_id)
    if not row or row.get("query_mode") != "remote":
        return
    try:
        await asyncio.to_thread(_warm_metadata_sync, row)
        await asyncio.to_thread(_warm_schema_sync, row)
    except Exception as e:
        logger.warning("single-row warmup failed for %s: %s", table_id, e)


def _broadcast(state: WarmupRunState, event: dict) -> None:
    """Send an event to every SSE subscriber. Dead queues are pruned."""
    dead = []
    for q in state._subscribers:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        state._subscribers.remove(q)


def _serialize_state(state: WarmupRunState) -> dict:
    return {
        "run_id": state.run_id,
        "trigger": state.trigger,
        "started_at": state.started_at,
        "completed_at": state.completed_at,
        "total": state.total,
        "completed": state.completed,
        "failed": state.failed,
        "rows": {tid: asdict(rs) for tid, rs in state.rows.items()},
    }


# ─── Endpoints ────────────────────────────────────────────────────────


@router.get("/api/admin/cache-warmup/status")
async def warmup_status(user: dict = Depends(require_admin)):
    if WARMUP_STATE is None:
        return {"state": "never_run"}
    return _serialize_state(WARMUP_STATE)


@router.post("/api/admin/cache-warmup/run")
async def warmup_run(user: dict = Depends(require_admin)):
    global WARMUP_STATE
    if WARMUP_STATE and WARMUP_STATE.completed_at is None:
        return {"run_id": WARMUP_STATE.run_id, "status": "already_running"}
    state = WarmupRunState(
        run_id=uuid4().hex[:8],
        trigger="manual",
        started_at=_now_iso(),
    )
    WARMUP_STATE = state
    asyncio.create_task(_warm_catalog_caches_bg(state=state))
    return {"run_id": state.run_id, "status": "started"}


@router.get("/api/admin/cache-warmup/stream")
async def warmup_stream(user: dict = Depends(require_admin)):
    async def gen():
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        if WARMUP_STATE is None:
            yield {"event": "idle", "data": json.dumps({"state": "never_run"})}
            return
        WARMUP_STATE._subscribers.append(q)
        yield {"event": "snapshot", "data": json.dumps(_serialize_state(WARMUP_STATE))}
        try:
            while True:
                ev = await asyncio.wait_for(q.get(), timeout=30.0)
                yield {"event": ev["event"], "data": json.dumps(ev["data"])}
                if ev["event"] == "complete":
                    return
        except asyncio.TimeoutError:
            return
        finally:
            if WARMUP_STATE and q in WARMUP_STATE._subscribers:
                WARMUP_STATE._subscribers.remove(q)

    return EventSourceResponse(gen())
