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


def test_jobwriter_check_cancel_requested_false_by_default(tmp_path):
    """No sentinel file present → method returns False."""
    from scripts.db_state_migrator import JobWriter
    w = JobWriter(job_id="job-cancel-test", jobs_dir=tmp_path, source="duckdb", target="side_car")
    w.write_initial()
    assert w.check_cancel_requested() is False


def test_jobwriter_check_cancel_requested_true_when_sentinel_exists(tmp_path):
    """Touch <job>.cancel → method returns True. This is the signal
    POST /api/admin/db/cancel/<id> writes (B2). Cooperative — the
    migrator checks at step boundaries."""
    from scripts.db_state_migrator import JobWriter
    w = JobWriter(job_id="job-cancel-test", jobs_dir=tmp_path, source="duckdb", target="side_car")
    w.write_initial()
    (tmp_path / "job-cancel-test.cancel").touch()
    assert w.check_cancel_requested() is True


def test_jobwriter_cancel_path(tmp_path):
    """cancel_path property is <jobs_dir>/<job_id>.cancel."""
    from scripts.db_state_migrator import JobWriter
    w = JobWriter(job_id="my-job", jobs_dir=tmp_path, source="duckdb", target="cloud")
    assert w.cancel_path == tmp_path / "my-job.cancel"


def test_main_aborts_at_step_boundary_when_cancel_sentinel_present(tmp_path, monkeypatch):
    """Drop sentinel before main() runs. main() must detect at the
    first post-write_initial step boundary, mark_cancelled, return 0.
    No PG calls happen because alembic step short-circuits.

    The previous bug let main() drive all the way to flip_backend
    while the cancel endpoint flipped status to cancelled — then the
    success write at line 628 overwrote the cancellation."""
    import duckdb
    from src.db import _ensure_schema
    from scripts.db_state_migrator import main

    duck_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(duck_path))
    _ensure_schema(conn)
    conn.close()

    jobs_dir = tmp_path / "db-jobs"
    backups_dir = tmp_path / "backups"
    jobs_dir.mkdir()

    # Sentinel present BEFORE main() is called — so the first
    # boundary check after write_initial trips it.
    (jobs_dir / "job-cancel-mid.cancel").touch()

    # Patch alembic to a no-op so we don't need a real PG target;
    # the sentinel check fires BEFORE the alembic call anyway, so
    # this only matters as a belt-and-braces guard.
    monkeypatch.setattr(
        "scripts.db_state_migrator.alembic_upgrade_head",
        lambda url: None,
    )

    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)

    rc = main(
        job_id="job-cancel-mid",
        to="side_car",
        target_url="postgresql+psycopg://x:y@z/q",
        duckdb_path=duck_path,
        jobs_dir=jobs_dir,
        backups_dir=backups_dir,
    )
    assert rc == 0, "cancellation is not a process failure"
    import json
    job = json.loads((jobs_dir / "job-cancel-mid.json").read_text())
    assert job["status"] == "cancelled"
    assert "step" in job["error"]


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


def test_jobwriter_status_file_is_0600(tmp_path):
    """H2 — every JobWriter status update must leave the file at 0600.
    The migrator updates this file ~6 times per job (write_initial,
    update_step x5, mark_success); each write goes through ._write
    via tmp + os.replace + chmod."""
    import os, stat
    from scripts.db_state_migrator import JobWriter
    w = JobWriter(job_id="job-mode-test", jobs_dir=tmp_path, source="duckdb", target="side_car")
    w.write_initial()
    p = tmp_path / "job-mode-test.json"
    mode = stat.S_IMODE(os.stat(p).st_mode)
    assert mode == 0o600, f"expected 0600 after write_initial, got {oct(mode)}"

    w.update_step("alembic", progress_pct=20)
    mode = stat.S_IMODE(os.stat(p).st_mode)
    assert mode == 0o600, f"expected 0600 after update_step, got {oct(mode)}"

    w.mark_success(summary={"rows_total": 0, "tables_migrated": 0})
    mode = stat.S_IMODE(os.stat(p).st_mode)
    assert mode == 0o600, f"expected 0600 after mark_success, got {oct(mode)}"
