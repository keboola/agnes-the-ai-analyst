"""Admin endpoint: DuckLake analytics-backend migration (wave-2G Task 6).

``POST /api/admin/analytics/migrate`` is the operator-facing entry point for
moving the analytics query surface between the ``legacy`` (rebuilt-and-swapped
``server.duckdb``) and ``ducklake`` backends — see
``src/analytics_backend.py`` for backend resolution and
``docs/superpowers/plans/2026-07-19-three-plane-wave2g-ducklake.md`` for the
wave plan this closes out.

**Why this is a validate-then-enqueue endpoint, not a config-flipping one.**
``analytics.backend`` is operator-owned config (``instance.yaml`` /
``AGNES_ANALYTICS_BACKEND``), resolved once per process and cached for its
lifetime (``src.analytics_backend.analytics_backend``) — it is read at boot,
not hot-reloaded. This endpoint therefore never writes config; its job is:

1. **Validate** (``to="ducklake"`` only) that the target backend can actually
   be populated/queried in this environment —
   :func:`src.ducklake_session.validate_ducklake_migration_prerequisites`
   (extension loadable, catalog reachable, auto-repairs a missing catalog
   database on an existing Postgres volume where the init-script never ran).
   Fails loud with the full list of problems if any check fails, and does
   NOT enqueue anything in that case.
2. **Enqueue** an ``analytics-migrate`` job
   (``app/worker/kinds.py::_run_analytics_migrate`` →
   ``SyncOrchestrator.migrate_to_backend``) that rebuilds the EXPLICITLY
   named target backend from the on-disk extracts tree — the extracts tree
   is the distribution artifact + rollback truth for both backends, so
   this never re-extracts from the source system, in either direction.
3. **Instruct** the operator (in the response body) to flip
   ``analytics.backend`` in config and restart every role process once the
   job completes — that is the step that actually switches the live
   query-serving plane over.

``to="legacy"`` is the rollback path: no ducklake-specific prerequisites (the
legacy backend has none), same enqueue-then-instruct shape, rebuilding from
the same extracts tree. Materialized-SQL tables are not re-materialized by
either direction — they re-materialize on their own scheduler cadence.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.access import require_admin
from src.audit_helpers import client_kind_from_user
from src.repositories import audit_repo, jobs_repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/analytics", tags=["admin-analytics"])

# Single idempotency key regardless of direction: a to=ducklake rebuild and
# a to=legacy rebuild must never run concurrently against the same extracts
# tree / DuckLake catalog, so one in-flight migrate job of either direction
# dedupes a second request rather than racing it.
_ANALYTICS_MIGRATE_IDEMPOTENCY_KEY = "analytics-migrate"

_VALID_TARGETS = ("ducklake", "legacy")


class AnalyticsMigrateRequest(BaseModel):
    """Body for ``POST /api/admin/analytics/migrate``."""

    to: str


@router.post("/migrate", status_code=202)
def migrate_analytics_backend(
    payload: AnalyticsMigrateRequest,
    user: dict = Depends(require_admin),
) -> dict:
    """Validate prerequisites (``to="ducklake"`` only) then enqueue an
    ``analytics-migrate`` job that rebuilds the named target backend from
    the on-disk extracts tree. See the module docstring for the full
    operator flow (this call never flips config).

    Returns 202 with ``{status, to, job_id, message}`` on success — poll
    ``GET /api/jobs/{job_id}`` (or ``agnes admin jobs show <job_id>``) for
    progress. 400 with the full list of unmet prerequisites when
    ``to="ducklake"`` and this environment can't populate/query a DuckLake
    catalog yet. 409 (with the in-flight ``job_id``) when a migration is
    already running.
    """
    target = (payload.to or "").strip().lower()
    if target not in _VALID_TARGETS:
        raise HTTPException(
            status_code=400,
            detail=f"`to` must be one of {_VALID_TARGETS!r}, got {payload.to!r}",
        )

    if target == "ducklake":
        from src.ducklake_session import validate_ducklake_migration_prerequisites

        problems = validate_ducklake_migration_prerequisites()
        if problems:
            raise HTTPException(
                status_code=400,
                detail={"error": "ducklake_prerequisites_failed", "problems": problems},
            )

    job = jobs_repo().enqueue(
        "analytics-migrate",
        {"to": target},
        idempotency_key=_ANALYTICS_MIGRATE_IDEMPOTENCY_KEY,
    )
    already_in_progress = job["deduped"]

    try:
        audit_repo().log(
            user_id=user.get("id"),
            action="analytics.migrate",
            resource=f"backend:{target}",
            params={
                "requested_at": datetime.now(timezone.utc).isoformat(),
                "to": target,
                "job_id": job["id"],
            },
            result="error.in_progress" if already_in_progress else "success",
            client_kind=client_kind_from_user(user),
        )
    except Exception:
        logger.exception("audit_log write failed for analytics.migrate; continuing")

    if already_in_progress:
        raise HTTPException(
            status_code=409,
            detail={"error": "analytics_migrate_already_in_progress", "job_id": job["id"]},
        )

    if target == "ducklake":
        message = (
            "DuckLake rebuild enqueued from the on-disk extracts tree (job "
            f"{job['id']}). Check `agnes admin jobs show {job['id']}` or "
            "GET /api/jobs/{job_id} for progress. Once it completes, set "
            "analytics.backend: ducklake (instance.yaml) or "
            "AGNES_ANALYTICS_BACKEND=ducklake (env) on every role process "
            "and restart — analytics.backend is read once at boot, not "
            "hot-reloaded, so query serving stays on the legacy backend "
            "until every process restarts with the new config."
        )
    else:
        message = (
            "Legacy rebuild enqueued from the on-disk extracts tree (job "
            f"{job['id']}). Check `agnes admin jobs show {job['id']}` or "
            "GET /api/jobs/{job_id} for progress. Once it completes, set "
            "analytics.backend: legacy (instance.yaml) or "
            "AGNES_ANALYTICS_BACKEND=legacy (env, or unset it) on every "
            "role process and restart. Materialized-SQL tables are not "
            "re-materialized by this rebuild — they follow their own "
            "scheduler cadence."
        )

    return {"status": "triggered", "to": target, "job_id": job["id"], "message": message}
