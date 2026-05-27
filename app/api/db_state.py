"""Admin endpoints for DB backend state machine.

Spec: docs/superpowers/specs/2026-05-27-db-backend-state-machine-design.md
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

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
    """Start a backend migration job (async; poll /job/{id} for status).

    Validates the requested transition against the current state, acquires
    the non-blocking migration flock (409 if already held), writes the
    initial job file and overlay state, then spawns the migrator
    subprocess in a new session and returns 202.
    """
    from src.db_state_machine import (
        BackendState,
        InvalidTransitionError,
        MigrationInProgressError,
        MigrationLock,
        validate_transition,
        write_backend_state,
    )

    current_state, _ = read_backend_state()
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
        # Write initial job status BEFORE spawning so the lock state is
        # observable by /api/admin/db/job/{id} polling even if subprocess
        # takes a moment to start.
        from scripts.db_state_migrator import JobWriter
        writer = JobWriter(
            job_id=job_id,
            jobs_dir=data_dir / "state" / "db-jobs",
            source=current_state.value,
            target=payload.target,
        )
        writer.write_initial()

        subprocess.Popen(
            [
                sys.executable, "-m", "scripts.db_state_migrator",
                "--job-id", job_id,
                "--to", payload.target,
                "--target-url", target_url,
                "--duckdb-path", str(data_dir / "state" / "system.duckdb"),
                "--jobs-dir", str(data_dir / "state" / "db-jobs"),
                "--backups-dir", str(data_dir / "state" / "backups"),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        lock.__exit__(None, None, None)

    return {"job_id": job_id, "status": "running"}
