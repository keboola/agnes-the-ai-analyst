# tests/test_db_state_cancel_duckdb_source.py
"""MED-4 — cancel_job removes url when reverting to duckdb backend."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_cancel_duckdb_source_drops_url_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When source_backend='duckdb', cancel-revert must wipe the
    target's url from instance.yaml. Leaving the postgres URL there
    creates a self-inconsistent overlay (backend=duckdb but url
    points at PG)."""
    from app.api import db_state

    # Point the jobs dir at tmp_path.
    jobs_dir = tmp_path / "db-jobs"
    jobs_dir.mkdir()
    monkeypatch.setattr(db_state, "_jobs_dir", lambda: jobs_dir)

    # Point write_backend_state's overlay at our temp instance.yaml.
    instance_yaml = tmp_path / "instance.yaml"
    monkeypatch.setattr(
        "src.db_state_machine._OVERLAY_PATH",
        instance_yaml,
    )

    # Simulate a running duckdb→side_car job + in-progress instance.yaml
    # (the state the applier would leave it in mid-flight).
    instance_yaml.write_text(
        "database:\n  backend: side_car_in_progress\n"
        "  url: postgresql+psycopg://agnes:pwd@postgres:5432/agnes\n"
    )
    job_id = "j-cancel-duckdb-1"
    (jobs_dir / f"{job_id}.json").write_text(
        json.dumps({
            "job_id": job_id,
            "status": "running",
            "source_backend": "duckdb",
            "target_backend": "side_car",
            "target_url": "postgresql+psycopg://agnes:pwd@postgres:5432/agnes",
            "current_step": "data_copy",
            "progress_pct": 40,
            "started_at": "2026-06-01T10:00:00Z",
            "completed_at": None,
            "summary": None,
            "error": None,
        })
    )

    # Call the endpoint function directly — FastAPI dependency injection
    # (require_admin) only fires when routed through the HTTP stack, so
    # no auth patching is needed for a direct call.
    out = db_state.cancel_job(job_id=job_id)

    after = instance_yaml.read_text()
    assert "backend: duckdb" in after, (
        f"MED-4: expected backend: duckdb after cancel revert; got:\n{after}"
    )
    assert "url:" not in after, (
        "MED-4: cancel revert to duckdb must drop the url key entirely; "
        f"instance.yaml content:\n{after}"
    )
    assert out["cancelled"] is True
