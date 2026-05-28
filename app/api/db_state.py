"""Admin endpoints for DB backend state machine.

Spec: docs/superpowers/specs/2026-05-27-db-backend-state-machine-design.md
"""
from __future__ import annotations
import json
import os
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.engine.url import make_url

from app.auth.access import require_admin
from src.db_state_machine import (
    allowed_transitions,
    read_backend_state,
)

router = APIRouter(prefix="/api/admin/db", tags=["admin-db"])


def _jobs_dir() -> Path:
    """Resolve the migration-jobs directory from DATA_DIR at call time.

    Reads ``DATA_DIR`` dynamically so tests that monkeypatch the env var
    on each fixture pick up the correct path.
    """
    return Path(os.environ.get("DATA_DIR", "/data")) / "state" / "db-jobs"


def _normalize_pg_url(url: str) -> tuple[str, int, str]:
    """Normalize a Postgres URL down to ``(host, port, database)``.

    Used to detect alias URLs that compare unequal as strings but point
    at the same physical Postgres database (B7). The comparison ignores
    user/password (credentials don't change which DB you're talking to)
    and the SQLAlchemy driver prefix (``postgresql://`` vs
    ``postgresql+psycopg://`` etc.). Host names and database names are
    lower-cased — Postgres treats them case-insensitively by convention,
    and our deployment never relies on case-distinct hosts.
    """
    parsed = make_url(url)
    host = (parsed.host or "").lower()
    port = parsed.port or 5432
    database = (parsed.database or "").lower()
    return (host, port, database)


def _urls_alias(a: str, b: str) -> bool:
    """True iff two Postgres URLs point at the same physical database.

    See :func:`_normalize_pg_url` for the normalization rules. Used by
    the migrate endpoint to reject "migrate onto self" attempts (B7).
    """
    return _normalize_pg_url(a) == _normalize_pg_url(b)


def _redact_url(url: str | None) -> str | None:
    """Replace password in ``postgresql://user:PASS@host`` with ``****``."""
    if not url:
        return None
    return re.sub(r"(://[^:]+:)[^@]+(@)", r"\1****\2", url)


def _current_job_id() -> str | None:
    """Return ``job_id`` of any currently-running migration job, else None."""
    jobs_dir = _jobs_dir()
    if not jobs_dir.exists():
        return None
    for path in jobs_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("status") == "running":
            return data.get("job_id")
    return None


@router.get("/state", dependencies=[Depends(require_admin)])
def get_db_state() -> dict:
    """Current backend + allowed transitions + in-progress job (if any)."""
    state, url = read_backend_state()
    return {
        "backend": state.value,
        "url_redacted": _redact_url(url),
        "allowed_transitions": [t.value for t in allowed_transitions(state)],
        "current_job_id": _current_job_id(),
    }


class MigrateRequest(BaseModel):
    """Body for ``POST /api/admin/db/migrate``."""
    target: str  # "side_car" or "cloud"
    cloud_url: str | None = None  # required when target=cloud


@router.post("/migrate", status_code=202, dependencies=[Depends(require_admin)])
def start_migration(payload: MigrateRequest) -> dict:
    """Queue a backend migration job for the host applier daemon.

    The endpoint does NOT execute the migration — it only writes the
    intent. The ``agnes-state-applier`` host daemon picks up the
    pending job within ~30s, stops the app container (releasing the
    DuckDB file lock — see the docstring at the top of
    ``scripts/ops/agnes-state-applier.sh`` for why in-process release
    isn't viable), runs the migrator subprocess on the host, then
    restarts the app on the new backend.

    Effects of this call:
      1. Validates the transition against the current state.
      2. Acquires the non-blocking migration flock (409 if held).
      3. Writes ``/data/state/instance.yaml::backend = *_in_progress``.
      4. Writes ``/data/state/db-jobs/<job_id>.json`` with
         ``status="pending"`` plus the target URL + backend so the
         applier has everything it needs to invoke the migrator.
      5. Writes ``/data/state/db-state-target.flag`` — the lifecycle
         signal the applier polls on.

    Returns 202 with ``{job_id, status: "pending"}``. Clients poll
    ``GET /api/admin/db/job/{id}`` for progress; the applier overwrites
    the same file with running → success / failed.
    """
    import json as _json
    from src.db_state_machine import (
        BackendState,
        InvalidTransitionError,
        MigrationInProgressError,
        MigrationLock,
        validate_transition,
        write_backend_state,
    )

    current_state, current_url = read_backend_state()
    try:
        target_state = BackendState(payload.target)
    except ValueError:
        raise HTTPException(400, detail=f"Unknown target: {payload.target}")

    try:
        validate_transition(current_state, target_state)
    except InvalidTransitionError as e:
        raise HTTPException(400, detail=str(e))

    if payload.target == "cloud" and not payload.cloud_url:
        raise HTTPException(400, detail="cloud_url required for target=cloud")

    # Resolve target URL.
    if payload.target == "side_car":
        password = os.environ.get("POSTGRES_PASSWORD", "agnes")
        target_url = f"postgresql+psycopg://agnes:{password}@postgres:5432/agnes"
    else:
        target_url = payload.cloud_url

    # Source URL — only present when source is a PG backend. The
    # applier passes it to the migrator's --source-url.
    source_url = current_url if current_state in (
        BackendState.SIDE_CAR, BackendState.CLOUD
    ) else None

    # Reject same-DB cycles — would silently put two readers on the
    # same physical Postgres after the cutover, which is data-loss
    # destructive once the source side is wiped. The alias check
    # normalizes credentials, default port, and driver prefix so that
    # cosmetic URL differences cannot bypass the guard (B7).
    if source_url and _urls_alias(source_url, target_url):
        raise HTTPException(
            400,
            detail="source and target URL alias the same Postgres database — refusing to migrate onto self",
        )

    job_id = str(uuid.uuid4())

    # Acquire lock — non-blocking; 409 if a peer already holds it.
    try:
        lock = MigrationLock()
        lock.__enter__()
    except MigrationInProgressError:
        existing = _current_job_id()
        raise HTTPException(
            409,
            detail=f"Migration already in progress: job {existing}",
        )

    try:
        in_progress = (
            BackendState.SIDE_CAR_IN_PROGRESS if payload.target == "side_car"
            else BackendState.CLOUD_IN_PROGRESS
        )
        write_backend_state(in_progress)

        data_dir = Path(os.environ.get("DATA_DIR", "/data"))
        jobs_dir = data_dir / "state" / "db-jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)

        # Pending job payload — the applier reads target_url +
        # source_backend + target_backend to compose the migrator
        # invocation, and overwrites this file with running/success/
        # failed as the migrator progresses.
        intent = {
            "job_id": job_id,
            "schema_version": 1,
            "status": "pending",
            "source_backend": current_state.value,
            "target_backend": payload.target,
            "target_url": target_url,
            "source_url": source_url,
            "progress_pct": 0,
            "current_step": "queued",
        }
        job_path = jobs_dir / f"{job_id}.json"
        tmp_path = job_path.with_suffix(".json.tmp")
        tmp_path.write_text(_json.dumps(intent, indent=2))
        os.replace(tmp_path, job_path)

        # Flag tells the applier WHICH compose lifecycle to settle on.
        flag_target = (
            "side-car-enabled" if payload.target == "side_car"
            else "cloud-only"
        )
        flag_path = data_dir / "state" / "db-state-target.flag"
        flag_path.parent.mkdir(parents=True, exist_ok=True)
        flag_tmp = flag_path.with_suffix(".flag.tmp")
        flag_tmp.write_text(flag_target)
        os.replace(flag_tmp, flag_path)
    finally:
        lock.__exit__(None, None, None)

    return {"job_id": job_id, "status": "pending"}


@router.get("/job/{job_id}", dependencies=[Depends(require_admin)])
def get_job(job_id: str) -> dict:
    """Return migration job status (poll target for POST /migrate clients)."""
    path = _jobs_dir() / f"{job_id}.json"
    if not path.exists():
        raise HTTPException(404, detail=f"Unknown job_id: {job_id}")
    return json.loads(path.read_text())


@router.post("/cancel/{job_id}", dependencies=[Depends(require_admin)])
def cancel_job(job_id: str) -> dict:
    """Cancel a running migration before point-of-no-return."""
    path = _jobs_dir() / f"{job_id}.json"
    if not path.exists():
        raise HTTPException(404, detail=f"Unknown job_id: {job_id}")

    data = json.loads(path.read_text())
    if data["status"] != "running":
        raise HTTPException(
            400, detail=f"Job is {data['status']}; cannot cancel non-running job"
        )
    if data["current_step"] in ("flip_backend", "app_restart", "verify_health"):
        raise HTTPException(
            409,
            detail="Past point-of-no-return (step >= flip_backend); manual recovery required"
        )

    # Signal the migrator subprocess (B2). The sentinel file is a
    # cooperative cancellation marker — the migrator polls for it at
    # step boundaries and raises JobCancelled when it observes the
    # file. We write the sentinel BEFORE flipping the job JSON status
    # so a slow migrator that polls slightly later still sees the
    # signal.
    sentinel = _jobs_dir() / f"{job_id}.cancel"
    sentinel.touch()

    from datetime import datetime, timezone
    data["status"] = "cancelled"
    data["completed_at"] = datetime.now(timezone.utc).isoformat()
    data["error"] = {
        "step": data["current_step"],
        "class": "Cancelled",
        "message": "Admin cancelled migration",
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, path)

    # Revert state machine to the source backend captured when the
    # migration kicked off.  The URL was preserved across the *_in_progress
    # write (B4), so a no-url write here keeps the live source URL.
    from src.db_state_machine import BackendState, write_backend_state
    write_backend_state(BackendState(data["source_backend"]))

    return {"cancelled": True}
