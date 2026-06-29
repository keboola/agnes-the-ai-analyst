# tests/test_db_state_cancel_during_verify.py
"""H1-NEW — cancel arriving between last sentinel check and flip is
either honored by the migrator (no flip) OR rejected by the API
(409 conflict, migration already committed). Never both."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_cancel_after_completed_returns_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API cancel after the migrator has written status=completed
    must return 409, not 400 or silently revert to SOURCE."""
    from app.api import db_state

    jobs_dir = tmp_path / "db-jobs"
    jobs_dir.mkdir()
    monkeypatch.setattr(db_state, "_jobs_dir", lambda: jobs_dir)
    monkeypatch.setattr(
        "src.db_state_machine._OVERLAY_PATH",
        tmp_path / "instance.yaml",
    )

    job_id = "j-h1"
    (jobs_dir / f"{job_id}.json").write_text(
        json.dumps({
            "schema_version": 1,
            "job_id": job_id,
            "status": "completed",
            "source_backend": "duckdb",
            "target_backend": "side_car",
            "started_at": "2026-06-01T10:00:00Z",
            "completed_at": "2026-06-01T10:05:00Z",
            "current_step": "flip_backend",
            "progress_pct": 100,
            "summary": {},
            "error": None,
        })
    )

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        db_state.cancel_job(job_id=job_id)
    assert exc.value.status_code == 409, (
        f"Expected 409 for completed job, got {exc.value.status_code}: {exc.value.detail}"
    )


def test_cancel_after_failed_returns_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API cancel on an already-failed job must also return 409."""
    from app.api import db_state

    jobs_dir = tmp_path / "db-jobs"
    jobs_dir.mkdir()
    monkeypatch.setattr(db_state, "_jobs_dir", lambda: jobs_dir)
    monkeypatch.setattr(
        "src.db_state_machine._OVERLAY_PATH",
        tmp_path / "instance.yaml",
    )

    job_id = "j-h1-failed"
    (jobs_dir / f"{job_id}.json").write_text(
        json.dumps({
            "schema_version": 1,
            "job_id": job_id,
            "status": "failed",
            "source_backend": "duckdb",
            "target_backend": "side_car",
            "started_at": "2026-06-01T10:00:00Z",
            "completed_at": "2026-06-01T10:03:00Z",
            "current_step": "data_copy",
            "progress_pct": 55,
            "summary": None,
            "error": {
                "step": "data_copy",
                "class": "RuntimeError",
                "message": "copy failed",
            },
        })
    )

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        db_state.cancel_job(job_id=job_id)
    assert exc.value.status_code == 409, (
        f"Expected 409 for failed job, got {exc.value.status_code}: {exc.value.detail}"
    )


def test_cancel_after_cancelled_returns_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API cancel on an already-cancelled job must return 409."""
    from app.api import db_state

    jobs_dir = tmp_path / "db-jobs"
    jobs_dir.mkdir()
    monkeypatch.setattr(db_state, "_jobs_dir", lambda: jobs_dir)
    monkeypatch.setattr(
        "src.db_state_machine._OVERLAY_PATH",
        tmp_path / "instance.yaml",
    )

    job_id = "j-h1-already-cancelled"
    (jobs_dir / f"{job_id}.json").write_text(
        json.dumps({
            "schema_version": 1,
            "job_id": job_id,
            "status": "cancelled",
            "source_backend": "duckdb",
            "target_backend": "side_car",
            "started_at": "2026-06-01T10:00:00Z",
            "completed_at": "2026-06-01T10:02:00Z",
            "current_step": "data_copy",
            "progress_pct": 45,
            "summary": None,
            "error": {
                "step": "data_copy",
                "class": "Cancelled",
                "message": "Admin cancelled migration",
            },
        })
    )

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        db_state.cancel_job(job_id=job_id)
    assert exc.value.status_code == 409, (
        f"Expected 409 for already-cancelled job, got {exc.value.status_code}: {exc.value.detail}"
    )


def test_migrator_rechecks_sentinel_before_flip(tmp_path: Path) -> None:
    """If the cancel sentinel file is present between
    ``copy_duckdb_to_pg`` and ``flip_backend``, the migrator must
    abort the flip and raise RuntimeError.
    """
    from scripts import db_state_migrator
    from src.db_state_machine import BackendState

    job_dir = tmp_path / "db-jobs"
    job_dir.mkdir()
    job_id = "j-flip-cancel"
    job_path = job_dir / f"{job_id}.json"
    job_path.write_text(
        json.dumps({
            "schema_version": 1,
            "job_id": job_id,
            "status": "running",
            "source_backend": "duckdb",
            "target_backend": "side_car",
            "started_at": "2026-06-01T10:00:00Z",
            "completed_at": None,
            "current_step": "verify",
            "progress_pct": 80,
            "summary": None,
            "error": None,
        })
    )
    # The sentinel is a sidecar .cancel file (B2 mechanism).
    sentinel = job_dir / f"{job_id}.cancel"
    sentinel.touch()

    with pytest.raises(RuntimeError) as exc:
        db_state_migrator._check_cancel_before_flip(
            job_path=job_path, target_state=BackendState.SIDE_CAR
        )
    assert "cancel" in str(exc.value).lower(), (
        f"Expected 'cancel' in error message, got: {exc.value}"
    )


def test_migrator_no_sentinel_allows_flip(tmp_path: Path) -> None:
    """With no sentinel file, ``_check_cancel_before_flip`` must return
    without raising so the flip proceeds normally."""
    from scripts import db_state_migrator
    from src.db_state_machine import BackendState

    job_dir = tmp_path / "db-jobs"
    job_dir.mkdir()
    job_id = "j-flip-ok"
    job_path = job_dir / f"{job_id}.json"
    job_path.write_text(
        json.dumps({
            "schema_version": 1,
            "job_id": job_id,
            "status": "running",
            "source_backend": "duckdb",
            "target_backend": "side_car",
            "started_at": "2026-06-01T10:00:00Z",
            "completed_at": None,
            "current_step": "verify",
            "progress_pct": 80,
            "summary": None,
            "error": None,
        })
    )
    # No sentinel file — should not raise.
    db_state_migrator._check_cancel_before_flip(
        job_path=job_path, target_state=BackendState.SIDE_CAR
    )


def test_cancel_sentinel_written_before_instance_yaml_revert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H1-NEW ordering: the cancel sentinel (.cancel sidecar file) must be
    written BEFORE write_backend_state is called. The migrator's final
    pre-flip re-check then sees the sentinel and refuses to flip."""
    from app.api import db_state

    jobs_dir = tmp_path / "db-jobs"
    jobs_dir.mkdir()
    monkeypatch.setattr(db_state, "_jobs_dir", lambda: jobs_dir)

    instance_yaml = tmp_path / "instance.yaml"
    instance_yaml.write_text(
        "database:\n  backend: side_car_in_progress\n"
        "  url: postgresql+psycopg://agnes:pwd@postgres:5432/agnes\n"
    )
    monkeypatch.setattr(
        "src.db_state_machine._OVERLAY_PATH",
        instance_yaml,
    )

    job_id = "j-ordering"
    (jobs_dir / f"{job_id}.json").write_text(
        json.dumps({
            "schema_version": 1,
            "job_id": job_id,
            "status": "running",
            "source_backend": "duckdb",
            "target_backend": "side_car",
            "started_at": "2026-06-01T10:00:00Z",
            "completed_at": None,
            "current_step": "verify",
            "progress_pct": 80,
            "summary": None,
            "error": None,
        })
    )

    sentinel_path = jobs_dir / f"{job_id}.cancel"
    write_backend_state_calls: list[str] = []

    def tracking_wbs(state, *, url=None):  # type: ignore[no-untyped-def]
        # Record that the sentinel must already exist at this point.
        assert sentinel_path.exists(), (
            "H1-NEW ordering violation: write_backend_state called before "
            "sentinel was written"
        )
        write_backend_state_calls.append(str(state))

    import src.db_state_machine as _sm
    monkeypatch.setattr(_sm, "write_backend_state", tracking_wbs)

    out = db_state.cancel_job(job_id=job_id)
    assert out["cancelled"] is True
    assert len(write_backend_state_calls) == 1, (
        "Expected exactly one write_backend_state call during cancel"
    )


def test_check_cancel_before_flip_raises_jobcancelled_not_runtimeerror(
    tmp_path: Path,
) -> None:
    """End-to-end regression: ``_check_cancel_before_flip`` must raise
    ``JobCancelled`` (a ``RuntimeError`` subclass), NOT bare ``RuntimeError``.

    The outer ``run_migration`` handler has two clauses:
        except JobCancelled → mark_cancelled
        except Exception    → mark_failed
    If the helper raises plain ``RuntimeError`` it falls through to
    ``mark_failed`` and the operator-visible status is ``failed``
    instead of ``cancelled`` — confusing the applier which inspects
    ``status=cancelled`` to distinguish cancels from real failures.
    """
    from scripts import db_state_migrator
    from scripts.db_state_migrator import JobCancelled
    from src.db_state_machine import BackendState

    job_dir = tmp_path / "db-jobs"
    job_dir.mkdir()
    job_id = "j-jobcancelled-type"
    job_path = job_dir / f"{job_id}.json"
    job_path.write_text(
        json.dumps({
            "schema_version": 1,
            "job_id": job_id,
            "status": "running",
            "source_backend": "duckdb",
            "target_backend": "side_car",
            "started_at": "2026-06-01T10:00:00Z",
            "completed_at": None,
            "current_step": "verify",
            "progress_pct": 80,
            "summary": None,
            "error": None,
        })
    )
    (job_dir / f"{job_id}.cancel").touch()

    with pytest.raises(JobCancelled) as exc:
        db_state_migrator._check_cancel_before_flip(
            job_path=job_path, target_state=BackendState.SIDE_CAR
        )
    # Step kwarg names where the cancel was honored — flip_backend.
    assert exc.value.step == "flip_backend", (
        f"Expected step='flip_backend', got step={exc.value.step!r}"
    )


def test_migrator_run_with_sentinel_marks_cancelled_not_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: when ``_check_cancel_before_flip`` fires inside the
    outer ``try`` of ``run_migration``, the ``except JobCancelled``
    clause must run and the final job JSON status must be ``cancelled``
    — NOT ``failed``. This locks in the bugfix where a bare
    ``RuntimeError`` would have routed to ``mark_failed`` instead.
    """
    from scripts import db_state_migrator
    from scripts.db_state_migrator import JobCancelled, JobWriter
    from src.db_state_machine import BackendState

    job_dir = tmp_path / "db-jobs"
    job_dir.mkdir()
    job_id = "j-e2e-cancel-status"

    # Build a writer mirroring what run_migration would build, then
    # plant a sentinel and invoke the helper from inside an outer try
    # that imitates run_migration's two-clause structure exactly.
    writer = JobWriter(
        job_id=job_id, jobs_dir=job_dir, source="duckdb", target="side_car",
    )
    writer.write_initial()
    writer.update_step("verify", progress_pct=80)
    (job_dir / f"{job_id}.cancel").touch()

    # Mirror the exact handler chain from run_migration so this test
    # would catch a future regression where someone "simplifies" the
    # helper back to bare RuntimeError.
    routed_to_cancelled = False
    routed_to_failed = False
    try:
        db_state_migrator._check_cancel_before_flip(
            job_path=writer._path,
            target_state=BackendState.SIDE_CAR,
        )
    except JobCancelled as cancel_exc:
        routed_to_cancelled = True
        writer.mark_cancelled(step=cancel_exc.step)
    except Exception as e:  # noqa: BLE001
        routed_to_failed = True
        writer.mark_failed(
            step="flip_backend",
            error_class=type(e).__name__,
            error_message=str(e),
        )

    assert routed_to_cancelled, (
        "Outer handler did NOT route to except JobCancelled — "
        "_check_cancel_before_flip likely raised bare RuntimeError again"
    )
    assert not routed_to_failed, (
        "Outer handler incorrectly routed to except Exception (mark_failed); "
        "operator-visible status would be 'failed' instead of 'cancelled'"
    )

    final = json.loads(writer._path.read_text())
    assert final["status"] == "cancelled", (
        f"Expected final status='cancelled', got {final['status']!r}"
    )
    assert final["error"]["step"] == "flip_backend"
