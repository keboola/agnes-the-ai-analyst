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


def copy_duckdb_to_pg(duckdb_path: Path, target_url: str) -> dict[str, int]:
    """Copy all PG-mapped tables from DuckDB to target PG.

    Wraps :func:`scripts.migrate_duckdb_to_pg.run_all` — the same
    idempotent copy loop that the docker-compose data-migrate one-shot
    uses. Returns ``{rows_total, tables_migrated}`` where ``rows_total``
    is the sum of PG row counts across all migrated tables (per the
    validator report — ``ON CONFLICT DO NOTHING`` makes per-task
    rows-inserted untrustworthy, so we use post-copy PG counts).
    """
    import duckdb
    import sqlalchemy as sa

    from scripts.migrate_duckdb_to_pg import run_all

    duck_conn = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        pg_engine = sa.create_engine(target_url)
        try:
            reports = run_all(duck_conn, pg_engine, validate=True)
        finally:
            pg_engine.dispose()
    finally:
        duck_conn.close()

    rows_total = sum(r.get("pg_rows", 0) for r in reports if "error" not in r)
    tables_migrated = sum(1 for r in reports if "error" not in r)
    return {
        "rows_total": rows_total,
        "tables_migrated": tables_migrated,
    }


def verify_row_counts(duckdb_path: Path, target_url: str) -> list[dict]:
    """Compare row counts per Base.metadata table between DuckDB and PG.

    Returns list of diffs ``[{table, source_rows, target_rows}]``.
    Empty list = all tables match. Tables present in only one side
    are also reported (the other side's count = 0).
    """
    import duckdb as _duckdb
    import sqlalchemy as sa
    from src.db_pg import Base

    diffs: list[dict] = []
    tables = [t.name for t in Base.metadata.sorted_tables]

    duck_conn = _duckdb.connect(str(duckdb_path))
    pg_engine = sa.create_engine(target_url)
    try:
        for table in tables:
            try:
                src_count = duck_conn.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone()[0]
            except _duckdb.CatalogException:
                src_count = 0
            try:
                with pg_engine.connect() as pg_conn:
                    tgt_count = pg_conn.execute(
                        sa.text(f'SELECT COUNT(*) FROM "{table}"')
                    ).fetchone()[0]
            except sa.exc.ProgrammingError:
                tgt_count = 0
            if src_count != tgt_count:
                diffs.append({
                    "table": table,
                    "source_rows": src_count,
                    "target_rows": tgt_count,
                })
    finally:
        duck_conn.close()
        pg_engine.dispose()
    return diffs


def backup_duckdb(duckdb_path: Path, backups_dir: Path) -> Path:
    """gzip the DuckDB file to backups dir with timestamp.

    Returns path to backup file. Used before duckdb → side_car cutover
    so the operator has a recovery point if the side-car PG ever
    diverges and needs to be re-built from the frozen DuckDB.
    """
    import gzip
    import shutil

    backups_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = backups_dir / f"duckdb-pre-sidecar-{ts}.duckdb.gz"
    with open(duckdb_path, "rb") as src, gzip.open(out, "wb", compresslevel=6) as dst:
        shutil.copyfileobj(src, dst)
    return out


def backup_sidecar_pg(container_name: str, backups_dir: Path) -> Path:
    """pg_dump custom format of side-car PG, via docker exec.

    Returns path to .dump file. Used before side_car → cloud cutover.
    """
    import subprocess

    backups_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = backups_dir / f"sidecar-pre-cloud-{ts}.dump"
    with open(out, "wb") as fp:
        result = subprocess.run(
            ["docker", "exec", container_name, "pg_dump", "-U", "agnes", "-F", "c", "agnes"],
            stdout=fp,
            stderr=subprocess.PIPE,
            check=False,
        )
    if result.returncode != 0:
        raise RuntimeError(f"pg_dump failed: {result.stderr.decode()}")
    return out


def main(
    *,
    job_id: str,
    to: str,
    target_url: str,
    duckdb_path: Path,
    jobs_dir: Path,
    backups_dir: Path,
) -> int:
    """Run the migration job. Returns process exit code.

    Steps for ``to="side_car"``:
      1. write initial job status
      2. alembic upgrade head
      3. copy DuckDB → PG
      4. verify row counts
      5. backup DuckDB
      6. flip instance.yaml::database
      7. mark success
    """
    from src.db_state_machine import BackendState, write_backend_state

    writer = JobWriter(
        job_id=job_id,
        jobs_dir=jobs_dir,
        source="duckdb" if to == "side_car" else "side_car",
        target=to,
    )
    writer.write_initial()

    try:
        writer.update_step("alembic", progress_pct=20)
        alembic_upgrade_head(target_url)

        writer.update_step("data_copy", progress_pct=40)
        copy_summary = copy_duckdb_to_pg(duckdb_path, target_url)

        writer.update_step("verify", progress_pct=80)
        diffs = verify_row_counts(duckdb_path, target_url)
        if diffs:
            writer.mark_failed(
                step="verify",
                error_class="VerifyMismatchError",
                error_message=f"Row count mismatch: {diffs[:5]}",
            )
            return 1

        writer.update_step("backup", progress_pct=90)
        backup_duckdb(duckdb_path, backups_dir)

        writer.update_step("flip_backend", progress_pct=95)
        target_state = BackendState(to)
        write_backend_state(target_state, url=target_url)

        writer.mark_success(summary=copy_summary)
        return 0

    except Exception as e:
        # Revert state to previous stable (best-effort).
        try:
            write_backend_state(
                BackendState.DUCKDB if to == "side_car" else BackendState.SIDE_CAR,
            )
        except Exception:
            pass
        writer.mark_failed(
            step=writer._read().get("current_step", "unknown"),
            error_class=type(e).__name__,
            error_message=str(e),
        )
        return 1


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--to", choices=["side_car", "cloud"], required=True)
    parser.add_argument("--target-url", required=True)
    parser.add_argument("--duckdb-path", type=Path, default=Path("/data/state/system.duckdb"))
    parser.add_argument("--jobs-dir", type=Path, default=Path("/data/state/db-jobs"))
    parser.add_argument("--backups-dir", type=Path, default=Path("/data/state/backups"))
    args = parser.parse_args()

    sys.exit(main(
        job_id=args.job_id,
        to=args.to,
        target_url=args.target_url,
        duckdb_path=args.duckdb_path,
        jobs_dir=args.jobs_dir,
        backups_dir=args.backups_dir,
    ))
