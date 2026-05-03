"""POST /api/admin/bigquery/test-connection — admin-only health probe.

Closes the operator-side half of #160. The reporter saw
USER_PROJECT_DENIED raw in the analyst CLI (now fixed via the structured
renderer in cli/error_render.py); this endpoint lets an admin verify the
saved BQ config from /admin/server-config WITHOUT having to wait for an
analyst to hit a query failure first.

Implementation runs a minimal `SELECT 1` via the existing BqAccess
plumbing with a 10s polling timeout. On `concurrent.futures.TimeoutError`
the BQ job is best-effort cancelled (job continues running on BQ side
until BQ-side timeout if the cancel itself fails — documented caveat).
"""
from __future__ import annotations

import concurrent.futures
import logging
import time

from fastapi import APIRouter, Depends, HTTPException

from app.auth.access import require_admin
from connectors.bigquery.access import get_bq_access, BqAccessError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/bigquery", tags=["admin"])

_QUERY_TIMEOUT_SECONDS = 10.0


@router.post("/test-connection")
async def test_connection(_user: dict = Depends(require_admin)):
    """Run `SELECT 1 AS ok` against BigQuery via the configured BqAccess.

    Returns 200 with `{ok, billing_project, data_project, elapsed_ms}` on
    success. Maps known failure modes:

    - `BqAccessError(not_configured)` → 400 with the typed detail
    - `BqAccessError` (other kinds) → 502 with the typed detail
    - `concurrent.futures.TimeoutError` → 504 with `kind="timeout"` and
      best-effort `cancel_job` invoked
    """
    try:
        bq = get_bq_access()
    except BqAccessError as exc:
        # not_configured is a 400 (operator config issue, not server fault).
        status = 400 if exc.kind == "not_configured" else 502
        raise HTTPException(status_code=status, detail={
            "kind": exc.kind,
            "message": exc.message,
            **(exc.details or {}),
        })

    try:
        client = bq.client()
    except BqAccessError as exc:
        status = 400 if exc.kind == "not_configured" else 502
        raise HTTPException(status_code=status, detail={
            "kind": exc.kind,
            "message": exc.message,
            **(exc.details or {}),
        })

    started = time.monotonic()
    try:
        job = client.query("SELECT 1 AS ok")
    except BqAccessError as exc:
        status = 400 if exc.kind == "not_configured" else 502
        raise HTTPException(status_code=status, detail={
            "kind": exc.kind,
            "message": exc.message,
            **(exc.details or {}),
        })
    except Exception as exc:
        # Fall through to upstream error — covers unexpected exception
        # types from the BQ client library.
        raise HTTPException(status_code=502, detail={
            "kind": "bq_upstream_error",
            "message": str(exc),
        })

    try:
        job.result(timeout=_QUERY_TIMEOUT_SECONDS)
    except concurrent.futures.TimeoutError:
        # Best-effort cancel — the BQ job keeps running on BQ side until
        # it sees the cancel or hits BQ's own timeout. Swallow any cancel
        # failure (we already failed; layering a cancel error is noise).
        try:
            client.cancel_job(
                job.job_id,
                location=getattr(job, "location", None),
            )
        except Exception:
            logger.warning("BQ cancel_job failed for job_id=%s", job.job_id)
        raise HTTPException(status_code=504, detail={
            "kind": "timeout",
            "elapsed_ms": int(_QUERY_TIMEOUT_SECONDS * 1000),
            "hint": (
                "BigQuery did not respond in 10s. Check network and SA "
                "permissions. The job was best-effort cancelled."
            ),
        })
    except BqAccessError as exc:
        # Rare: BqAccessError surfacing from the polling loop (e.g.
        # auth_failed mid-flight).
        raise HTTPException(status_code=502, detail={
            "kind": exc.kind,
            "message": exc.message,
            **(exc.details or {}),
        })
    except Exception as exc:
        raise HTTPException(status_code=502, detail={
            "kind": "bq_upstream_error",
            "message": str(exc),
        })

    elapsed_ms = int((time.monotonic() - started) * 1000)
    return {
        "ok": True,
        "billing_project": bq.projects.billing,
        "data_project": bq.projects.data,
        "elapsed_ms": elapsed_ms,
    }
