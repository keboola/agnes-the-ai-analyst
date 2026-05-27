"""Unit tests for db_state_migrator subprocess orchestrator."""
from __future__ import annotations
import json
from pathlib import Path
import pytest

from scripts.db_state_migrator import JobStatus, JobWriter


def test_job_writer_writes_initial_status(tmp_path):
    jobs_dir = tmp_path / "db-jobs"
    jobs_dir.mkdir()
    writer = JobWriter(job_id="abc123", jobs_dir=jobs_dir, source="duckdb", target="side_car")
    writer.write_initial()

    path = jobs_dir / "abc123.json"
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["job_id"] == "abc123"
    assert data["status"] == "running"
    assert data["source_backend"] == "duckdb"
    assert data["target_backend"] == "side_car"
    assert data["progress_pct"] == 0
    assert data["started_at"] is not None
    assert data["completed_at"] is None


def test_job_writer_update_step(tmp_path):
    jobs_dir = tmp_path / "db-jobs"
    jobs_dir.mkdir()
    writer = JobWriter(job_id="abc123", jobs_dir=jobs_dir, source="duckdb", target="side_car")
    writer.write_initial()
    writer.update_step("alembic", progress_pct=25)

    data = json.loads((jobs_dir / "abc123.json").read_text())
    assert data["current_step"] == "alembic"
    assert data["progress_pct"] == 25


def test_job_writer_mark_success(tmp_path):
    jobs_dir = tmp_path / "db-jobs"
    jobs_dir.mkdir()
    writer = JobWriter(job_id="abc123", jobs_dir=jobs_dir, source="duckdb", target="side_car")
    writer.write_initial()
    writer.mark_success(summary={"tables_migrated": 28, "rows_total": 12345})

    data = json.loads((jobs_dir / "abc123.json").read_text())
    assert data["status"] == "success"
    assert data["completed_at"] is not None
    assert data["summary"]["tables_migrated"] == 28


def test_job_writer_mark_failed(tmp_path):
    jobs_dir = tmp_path / "db-jobs"
    jobs_dir.mkdir()
    writer = JobWriter(job_id="abc123", jobs_dir=jobs_dir, source="duckdb", target="side_car")
    writer.write_initial()
    writer.mark_failed(step="data_copy", error_class="OperationalError", error_message="connection terminated")

    data = json.loads((jobs_dir / "abc123.json").read_text())
    assert data["status"] == "failed"
    assert data["error"]["step"] == "data_copy"
    assert data["error"]["class"] == "OperationalError"


def test_backup_duckdb_creates_gzipped_copy(tmp_path):
    """Backup writes gzip'd DuckDB to backups dir; original untouched."""
    import duckdb
    from src.db import _ensure_schema
    from scripts.db_state_migrator import backup_duckdb

    src = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(src))
    _ensure_schema(conn)
    conn.close()

    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()

    out = backup_duckdb(src, backups_dir)
    assert out.exists()
    assert out.name.startswith("duckdb-pre-sidecar-")
    assert out.suffix == ".gz"
    assert src.exists()  # original preserved
