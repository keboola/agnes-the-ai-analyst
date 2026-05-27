"""Migration subprocess orchestrator for DB backend state machine.

Invoked by app/api/db_state.py as a child subprocess; writes job
status to /data/state/db-jobs/<job_id>.json so the API endpoint can
poll. Steps for duckdb → side_car:

  1. validate connectivity
  2. alembic upgrade head on target
  3. data copy DuckDB → target (reuses scripts/migrate_duckdb_to_pg)
  4. verify row counts
  5. backup DuckDB snapshot
  6. flip instance.yaml::database

For side_car → cloud, source is the side-car PG; data step uses the
same migrator with a different source connection.

Spec: docs/superpowers/specs/2026-05-27-db-backend-state-machine-design.md
"""
from __future__ import annotations
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any


class JobStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class JobWriter:
    """Writes /data/state/db-jobs/<job_id>.json on each step transition.

    Atomic write via tmp + os.replace. Schema versioned at 1.
    """

    job_id: str
    jobs_dir: Path
    source: str
    target: str
    _started_at: str = field(default_factory=lambda: _utcnow_iso())

    @property
    def _path(self) -> Path:
        return self.jobs_dir / f"{self.job_id}.json"

    def _write(self, data: dict[str, Any]) -> None:
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        os.replace(tmp, self._path)

    def _read(self) -> dict[str, Any]:
        if self._path.exists():
            return json.loads(self._path.read_text())
        return {}

    def write_initial(self) -> None:
        data = {
            "schema_version": 1,
            "job_id": self.job_id,
            "status": JobStatus.RUNNING.value,
            "source_backend": self.source,
            "target_backend": self.target,
            "started_at": self._started_at,
            "completed_at": None,
            "current_step": "validate",
            "progress_pct": 0,
            "summary": None,
            "error": None,
        }
        self._write(data)

    def update_step(self, step: str, *, progress_pct: int) -> None:
        data = self._read()
        data["current_step"] = step
        data["progress_pct"] = progress_pct
        self._write(data)

    def update_table_progress(self, current_table: str, tables_done: int, tables_total: int) -> None:
        data = self._read()
        data["current_step"] = "data_copy"
        data["table_progress"] = {
            "current_table": current_table,
            "tables_done": tables_done,
            "tables_total": tables_total,
        }
        data["progress_pct"] = int(40 + (tables_done / tables_total) * 40)  # 40-80% range
        self._write(data)

    def mark_success(self, summary: dict[str, Any]) -> None:
        data = self._read()
        data["status"] = JobStatus.SUCCESS.value
        data["completed_at"] = _utcnow_iso()
        data["progress_pct"] = 100
        data["summary"] = summary
        self._write(data)

    def mark_failed(self, *, step: str, error_class: str, error_message: str) -> None:
        data = self._read()
        data["status"] = JobStatus.FAILED.value
        data["completed_at"] = _utcnow_iso()
        data["error"] = {
            "step": step,
            "class": error_class,
            "message": error_message,
        }
        self._write(data)

    def mark_cancelled(self, *, step: str) -> None:
        data = self._read()
        data["status"] = JobStatus.CANCELLED.value
        data["completed_at"] = _utcnow_iso()
        data["error"] = {"step": step, "class": "Cancelled", "message": "Admin cancelled migration"}
        self._write(data)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def alembic_upgrade_head(target_url: str) -> None:
    """Run ``alembic upgrade head`` against ``target_url``.

    Idempotent — alembic itself is a no-op when already at head.
    Raises RuntimeError on failure.
    """
    import subprocess

    repo_root = Path(__file__).resolve().parent.parent
    env = {**os.environ, "DATABASE_URL": target_url}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(repo_root / "alembic.ini"), "upgrade", "head"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(repo_root),
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"alembic upgrade head failed (exit {result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
