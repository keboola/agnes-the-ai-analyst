"""Integration tests for /api/admin/db/* endpoints.

Spec: docs/superpowers/specs/2026-05-27-db-backend-state-machine-design.md
"""
from __future__ import annotations

import json


def test_get_db_state_default_duckdb(seeded_app, monkeypatch):
    """Fresh-install default: backend=duckdb, no url, only side_car reachable."""
    # Point the state-machine overlay at the e2e_env DATA_DIR (no overlay file
    # exists yet → read_backend_state() returns (DUCKDB, None)).
    data_dir = seeded_app["env"]["data_dir"]
    monkeypatch.setattr(
        "src.db_state_machine._OVERLAY_PATH",
        data_dir / "state" / "instance.yaml",
    )

    client = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = client.get(
        "/api/admin/db/state",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["backend"] == "duckdb"
    assert body["url_redacted"] is None
    assert body["allowed_transitions"] == ["side_car"]
    assert body["current_job_id"] is None


def test_get_db_state_requires_admin(seeded_app):
    """Non-admin token is rejected with 403."""
    client = seeded_app["client"]
    token = seeded_app["analyst_token"]
    r = client.get(
        "/api/admin/db/state",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code in (401, 403), r.text


def test_redact_url_replaces_password():
    """`_redact_url` masks the password segment in postgresql:// URLs.

    A full integration test against a side_car overlay would also flip the
    repository factory to Postgres mode (see src/repositories/__init__.py
    ::users_repo) and fail auth on a missing psycopg2 / live PG. Redaction
    is a pure helper — unit-test it directly.
    """
    from app.api.db_state import _redact_url

    assert _redact_url(None) is None
    assert _redact_url("") is None
    assert (
        _redact_url("postgresql://agnes:supersecret@127.0.0.1:5432/agnes")
        == "postgresql://agnes:****@127.0.0.1:5432/agnes"
    )
    # No password component → unchanged.
    assert _redact_url("postgresql://127.0.0.1/agnes") == "postgresql://127.0.0.1/agnes"


def test_get_db_state_surfaces_running_job(seeded_app, monkeypatch):
    """A db-jobs/*.json with status=running surfaces as current_job_id."""
    data_dir = seeded_app["env"]["data_dir"]
    monkeypatch.setattr(
        "src.db_state_machine._OVERLAY_PATH",
        data_dir / "state" / "instance.yaml",
    )

    jobs_dir = data_dir / "state" / "db-jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (jobs_dir / "abc123.json").write_text(
        json.dumps({"job_id": "abc123", "status": "running"})
    )
    # Add a completed job to confirm it's ignored.
    (jobs_dir / "old.json").write_text(
        json.dumps({"job_id": "old", "status": "completed"})
    )

    client = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = client.get(
        "/api/admin/db/state",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["current_job_id"] == "abc123"


def _patch_state_paths(monkeypatch, data_dir):
    """Point the state-machine module's overlay + lock paths at the test DATA_DIR.

    Module-level constants are computed at import time from DATA_DIR — tests
    that switch DATA_DIR per-fixture must patch them explicitly.
    """
    monkeypatch.setattr(
        "src.db_state_machine._OVERLAY_PATH",
        data_dir / "state" / "instance.yaml",
    )
    monkeypatch.setattr(
        "src.db_state_machine._LOCK_PATH",
        data_dir / "state" / "db-migration.lock",
    )


def test_post_migrate_starts_job(seeded_app, monkeypatch):
    """POST /migrate returns 202 + job_id, spawns the migrator subprocess."""
    data_dir = seeded_app["env"]["data_dir"]
    _patch_state_paths(monkeypatch, data_dir)

    spawned: list[list[str]] = []

    def fake_popen(cmd, *args, **kwargs):
        spawned.append(cmd)

        class FakeProc:
            pid = 12345

        return FakeProc()

    monkeypatch.setattr("app.api.db_state.subprocess.Popen", fake_popen)

    client = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = client.post(
        "/api/admin/db/migrate",
        json={"target": "side_car"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert "job_id" in body
    assert body["status"] == "running"
    # Subprocess invoked exactly once, target on the command line.
    assert len(spawned) == 1
    assert "side_car" in spawned[0]


def test_post_migrate_rejects_invalid_transition(seeded_app, monkeypatch):
    """duckdb → cloud (skipping side-car) rejected with 400."""
    data_dir = seeded_app["env"]["data_dir"]
    _patch_state_paths(monkeypatch, data_dir)

    client = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = client.post(
        "/api/admin/db/migrate",
        json={"target": "cloud", "cloud_url": "postgresql://u:p@h/d"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400, r.text
    assert "not allowed" in r.json()["detail"].lower()


def test_post_migrate_409_when_in_progress(seeded_app, monkeypatch):
    """Migrate attempt while the migration flock is held returns 409.

    Held manually rather than via a prior POST: the first POST would write
    ``database.backend = side_car_in_progress`` to instance.yaml, which
    flips the repository factory to PG mode → ``get_current_user`` on the
    second request fails with a missing-URL RuntimeError before the lock
    check runs. Acquiring the lock directly exercises the 409 path without
    touching the overlay.
    """
    data_dir = seeded_app["env"]["data_dir"]
    _patch_state_paths(monkeypatch, data_dir)

    from src.db_state_machine import MigrationLock

    client = seeded_app["client"]
    token = seeded_app["admin_token"]

    with MigrationLock():
        r = client.post(
            "/api/admin/db/migrate",
            json={"target": "side_car"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 409, r.text
        assert "in progress" in r.json()["detail"].lower()


def test_get_job_returns_status(seeded_app, monkeypatch):
    """GET /job/{id} returns persisted JSON shape."""
    data_dir = seeded_app["env"]["data_dir"]
    _patch_state_paths(monkeypatch, data_dir)

    jobs_dir = data_dir / "state" / "db-jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (jobs_dir / "abc.json").write_text(json.dumps({
        "schema_version": 1, "job_id": "abc", "status": "success",
        "source_backend": "duckdb", "target_backend": "side_car",
        "started_at": "2026-05-27T16:00:00+00:00",
        "completed_at": "2026-05-27T16:02:00+00:00",
        "current_step": "flip_backend", "progress_pct": 100,
        "summary": {"tables_migrated": 28, "rows_total": 1234},
        "error": None,
    }))

    client = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = client.get(
        "/api/admin/db/job/abc",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "success"
    assert data["summary"]["tables_migrated"] == 28


def test_get_job_404_unknown(seeded_app, monkeypatch):
    """Unknown job_id returns 404."""
    data_dir = seeded_app["env"]["data_dir"]
    _patch_state_paths(monkeypatch, data_dir)

    client = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = client.get(
        "/api/admin/db/job/nonexistent",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404, r.text


def test_post_cancel_marks_job_cancelled(seeded_app, monkeypatch):
    """Cancel a running job — status → cancelled, state reverted."""
    data_dir = seeded_app["env"]["data_dir"]
    _patch_state_paths(monkeypatch, data_dir)

    jobs_dir = data_dir / "state" / "db-jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (jobs_dir / "abc.json").write_text(json.dumps({
        "schema_version": 1, "job_id": "abc", "status": "running",
        "source_backend": "duckdb", "target_backend": "side_car",
        "started_at": "2026-05-27T16:00:00+00:00", "completed_at": None,
        "current_step": "data_copy", "progress_pct": 50,
        "summary": None, "error": None,
    }))

    client = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = client.post(
        "/api/admin/db/cancel/abc",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["cancelled"] is True

    data = json.loads((jobs_dir / "abc.json").read_text())
    assert data["status"] == "cancelled"


def test_post_cancel_409_after_flip_backend(seeded_app, monkeypatch):
    """Cancel after flip_backend step is rejected (past point-of-no-return)."""
    data_dir = seeded_app["env"]["data_dir"]
    _patch_state_paths(monkeypatch, data_dir)

    jobs_dir = data_dir / "state" / "db-jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (jobs_dir / "abc.json").write_text(json.dumps({
        "schema_version": 1, "job_id": "abc", "status": "running",
        "source_backend": "duckdb", "target_backend": "side_car",
        "started_at": "2026-05-27T16:00:00+00:00", "completed_at": None,
        "current_step": "flip_backend", "progress_pct": 95,
        "summary": None, "error": None,
    }))

    client = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = client.post(
        "/api/admin/db/cancel/abc",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 409, r.text
