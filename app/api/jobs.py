"""REST surface for the wave-2B durable job queue (spec §3.3, Task 5).

Exposes the ``jobs`` table (``src/repositories/jobs.py`` / ``jobs_pg.py``,
routed through the ``jobs_repo()`` factory so both backends work
identically) for enqueueing and inspecting worker-runtime jobs:

  - ``POST /api/jobs``           — enqueue a job. 202 ``{"job": {...}}``.
  - ``GET  /api/jobs/{job_id}``  — fetch one job. 404 if unknown.
  - ``GET  /api/jobs``           — list jobs, optionally filtered by
    ``status``/``kind``, capped by ``limit``.

Gate: ``Depends(require_admin)`` on every endpoint — no special-casing for
the scheduler is needed here. ``get_current_user`` (``app/auth/dependencies.py``)
already resolves a valid ``SCHEDULER_API_TOKEN`` bearer into the synthetic
``scheduler@system.local`` user, which is a member of the ``Admin`` group
(``app/auth/scheduler_token.py``), so ``require_admin`` accepts it exactly
like a human admin's session token. This is the same dual-accept pattern
used by the existing scheduler-driven endpoints in ``app/api/admin.py``
(e.g. ``run-session-collector``, ``run-corporate-memory``) — none of them
special-case the token either.

Enqueueing validates ``kind`` against the process-wide ``JOB_KINDS``
registry (``app/worker/registry.py``), populated by
``register_all_kinds()`` (``app/worker/kinds.py``) before the worker loop
starts. The registry is intentionally NOT populated defensively here if
empty — in a test process that never ran the app lifespan, ``JOB_KINDS``
is empty by design, and the enqueue tests register their own fake kinds
via ``register_kind()`` before hitting this endpoint, exactly like the
worker-runtime tests do.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.auth.access import require_admin
from app.auth.dependencies import _get_db
from app.job_correlation import stamp_request_id
from src.repositories import audit_repo, jobs_repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

_MAX_LIST_LIMIT = 500


class EnqueueJobRequest(BaseModel):
    kind: str
    payload: Dict[str, Any] = {}
    idempotency_key: Optional[str] = None


def _audit(actor_id: str, action: str, resource: str, params: Optional[Dict[str, Any]] = None) -> None:
    try:
        audit_repo().log(user_id=actor_id, action=action, resource=resource, params=params)
    except Exception:
        logger.warning("audit log failed for %s/%s", action, resource)


def _serialize(row: Dict[str, Any]) -> Dict[str, Any]:
    """Render a repo job row for the API — ``payload_json`` (the DB column
    name, already decoded to a dict by the repo) surfaces as ``payload``
    to mirror the request field name; timestamp columns are isoformatted."""

    def _iso(v: Any) -> Optional[str]:
        return v.isoformat() if v is not None else None

    return {
        "id": row["id"],
        "kind": row["kind"],
        "payload": row.get("payload_json") or {},
        "status": row["status"],
        "priority": row.get("priority", 0),
        "run_after": _iso(row.get("run_after")),
        "attempts": row.get("attempts", 0),
        "max_attempts": row.get("max_attempts", 3),
        "lease_expires_at": _iso(row.get("lease_expires_at")),
        "leased_by": row.get("leased_by"),
        "idempotency_key": row.get("idempotency_key"),
        "error": row.get("error"),
        "created_at": _iso(row.get("created_at")),
        "started_at": _iso(row.get("started_at")),
        "finished_at": _iso(row.get("finished_at")),
    }


@router.post("", status_code=202)
async def enqueue_job(
    payload: EnqueueJobRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Enqueue a job. 400 if ``kind`` isn't registered in ``JOB_KINDS``.

    Dedup: passing an ``idempotency_key`` that matches an existing
    queued/running job of the caller returns that job unchanged instead of
    inserting a duplicate (see ``JobsRepository.enqueue`` docstring).
    """
    from app.worker.registry import JOB_KINDS

    if payload.kind not in JOB_KINDS:
        registered = sorted(JOB_KINDS) or ["(none registered)"]
        raise HTTPException(
            status_code=400,
            detail=f"unknown_job_kind: {payload.kind!r}. Registered kinds: {registered}",
        )

    job = jobs_repo().enqueue(
        payload.kind,
        stamp_request_id(payload.payload),
        idempotency_key=payload.idempotency_key,
    )
    _audit(
        user["id"],
        "job.enqueue",
        f"job:{job['id']}",
        {"kind": payload.kind, "idempotency_key": payload.idempotency_key},
    )
    return {"job": _serialize(job)}


@router.get("/{job_id}")
async def get_job(
    job_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    job = jobs_repo().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job_not_found")
    return {"job": _serialize(job)}


@router.get("")
async def list_jobs(
    status: Optional[str] = Query(None, description="Filter by job status (queued|running|done|failed)"),
    kind: Optional[str] = Query(None, description="Filter by job kind"),
    limit: int = Query(50, ge=1, le=_MAX_LIST_LIMIT, description="Max rows to return"),
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    rows = jobs_repo().list(status=status, kind=kind, limit=limit)
    return {"jobs": [_serialize(r) for r in rows]}
