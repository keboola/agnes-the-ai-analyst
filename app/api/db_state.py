"""Admin endpoints for DB backend state machine.

Spec: docs/superpowers/specs/2026-05-27-db-backend-state-machine-design.md
"""
from __future__ import annotations
import json
import os
import re
from pathlib import Path

from fastapi import APIRouter, Depends

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
