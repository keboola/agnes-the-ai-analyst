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
    # DuckDB can cut over to either side-car PG or straight to cloud.
    assert body["allowed_transitions"] == ["side_car", "cloud"]
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


def test_post_migrate_queues_pending_job(seeded_app, monkeypatch):
    """POST /migrate writes a pending job + flag for the host applier.

    The endpoint no longer spawns the migrator itself — that runs from
    the host because DuckDB's in-process file lock cannot be released
    deterministically while the uvicorn worker is alive (verified on
    agnes-dev across multiple in-process release attempts).
    """
    import json
    data_dir = seeded_app["env"]["data_dir"]
    _patch_state_paths(monkeypatch, data_dir)

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
    assert body["status"] == "pending"

    # Job intent persisted with everything the host applier needs.
    job_path = data_dir / "state" / "db-jobs" / f"{body['job_id']}.json"
    job = json.loads(job_path.read_text())
    assert job["status"] == "pending"
    assert job["source_backend"] == "duckdb"
    assert job["target_backend"] == "side_car"
    assert "postgres:5432" in job["target_url"]
    assert job["schema_version"] == 1

    # Flag flipped to the side-car-enabled lifecycle.
    flag = (data_dir / "state" / "db-state-target.flag").read_text()
    assert flag == "side-car-enabled"


def test_post_migrate_does_not_spawn_subprocess(seeded_app, monkeypatch):
    """Regression: the endpoint MUST NOT shell out to the migrator.

    The host applier owns subprocess execution now. If anything inside
    the handler reaches for subprocess.Popen / os.execv / os.system,
    we're back to the in-process DuckDB lock conflict.
    """
    import subprocess as _sp
    data_dir = seeded_app["env"]["data_dir"]
    _patch_state_paths(monkeypatch, data_dir)

    def fail(*a, **kw):
        raise AssertionError("endpoint must not spawn the migrator itself")

    monkeypatch.setattr(_sp, "Popen", fail)
    monkeypatch.setattr(_sp, "run", fail)

    client = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = client.post(
        "/api/admin/db/migrate",
        json={"target": "side_car"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 202, r.text


def test_post_migrate_rejects_invalid_transition(seeded_app, monkeypatch):
    """duckdb → duckdb (self-loop) rejected with 400.

    DuckDB has [SIDE_CAR, CLOUD] in its allowed list — never itself.
    Asserting via the self-loop keeps the test in DuckDB mode (no
    instance.yaml write) so the test harness doesn't switch to PG
    mode mid-test and try to connect to ``postgres:5432``.
    """
    data_dir = seeded_app["env"]["data_dir"]
    _patch_state_paths(monkeypatch, data_dir)

    client = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = client.post(
        "/api/admin/db/migrate",
        json={"target": "duckdb"},
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


def test_cancel_reverts_to_source_backend_duckdb_to_cloud(seeded_app, monkeypatch):
    """Cancelling a DUCKDB → CLOUD migration must revert to DUCKDB (the source).

    The previous bug computed the revert from ``target_backend == 'side_car'``
    and so picked SIDE_CAR for target=cloud — leaving the state machine in
    SIDE_CAR even though the app never ran there (B1).
    """
    data_dir = seeded_app["env"]["data_dir"]
    _patch_state_paths(monkeypatch, data_dir)

    # Leave overlay absent (default DUCKDB) so use_pg() stays False during
    # auth and the test client can authenticate without a live Postgres.
    # The cancel endpoint writes the revert value itself — we verify that.

    jobs_dir = data_dir / "state" / "db-jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    job_id = "job-cancel-duckdb-cloud"
    (jobs_dir / f"{job_id}.json").write_text(json.dumps({
        "schema_version": 1,
        "job_id": job_id,
        "status": "running",
        "source_backend": "duckdb",
        "target_backend": "cloud",
        "current_step": "data_copy",   # pre point-of-no-return
        "started_at": "2026-05-28T10:00:00Z",
        "completed_at": None,
        "progress_pct": 30,
        "summary": None,
        "error": None,
    }))

    client = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = client.post(
        f"/api/admin/db/cancel/{job_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["cancelled"] is True

    # Verify state machine reverted to DUCKDB (the SOURCE), not SIDE_CAR.
    from src.db_state_machine import BackendState, read_backend_state
    state, _url = read_backend_state()
    assert state == BackendState.DUCKDB, f"expected DUCKDB after cancel, got {state.value}"


def test_cancel_reverts_to_source_backend_cloud_to_side_car(seeded_app, monkeypatch):
    """Cancelling a CLOUD → SIDE_CAR migration reverts to CLOUD (the source)
    and preserves the cloud URL that was live when the migration kicked off.

    URL-preservation is tested via a raw overlay pre-written with
    backend=duckdb so the test client can authenticate (use_pg() stays False).
    After cancel the endpoint writes backend=cloud while the pre-seeded URL
    carries through via the Ellipsis sentinel in write_backend_state.
    """
    import yaml
    data_dir = seeded_app["env"]["data_dir"]
    _patch_state_paths(monkeypatch, data_dir)

    # Pre-seed the overlay with the source URL but keep backend=duckdb so the
    # test-client HTTP requests use the DuckDB repository (no live PG needed).
    # In production the *_in_progress write preserves this URL via the
    # Ellipsis sentinel (B4); here we replicate that end-state directly.
    overlay = data_dir / "state" / "instance.yaml"
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(yaml.safe_dump({
        "database": {
            "backend": "duckdb",
            "url": "postgresql+psycopg://cloud:pw@cloudhost/agnes",
        }
    }))

    jobs_dir = data_dir / "state" / "db-jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    job_id = "job-cancel-cloud-side"
    (jobs_dir / f"{job_id}.json").write_text(json.dumps({
        "schema_version": 1,
        "job_id": job_id,
        "status": "running",
        "source_backend": "cloud",
        "target_backend": "side_car",
        "current_step": "data_copy",
        "started_at": "2026-05-28T10:00:00Z",
        "completed_at": None,
        "progress_pct": 30,
        "summary": None,
        "error": None,
    }))

    client = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = client.post(
        f"/api/admin/db/cancel/{job_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["cancelled"] is True

    from src.db_state_machine import BackendState, read_backend_state
    state, url = read_backend_state()
    assert state == BackendState.CLOUD, f"expected CLOUD after cancel, got {state.value}"
    assert url == "postgresql+psycopg://cloud:pw@cloudhost/agnes", \
        "source URL must be preserved on cancel"


def test_cancel_reverts_to_source_backend_side_car_to_cloud(seeded_app, monkeypatch):
    """Cancelling a SIDE_CAR → CLOUD migration reverts to SIDE_CAR with the
    side_car URL preserved.
    """
    import yaml
    data_dir = seeded_app["env"]["data_dir"]
    _patch_state_paths(monkeypatch, data_dir)

    # Same URL-preservation technique: park the side_car URL in the overlay
    # under backend=duckdb so use_pg() stays False during auth.
    overlay = data_dir / "state" / "instance.yaml"
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(yaml.safe_dump({
        "database": {
            "backend": "duckdb",
            "url": "postgresql+psycopg://x:y@postgres:5432/agnes",
        }
    }))

    jobs_dir = data_dir / "state" / "db-jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    job_id = "job-cancel-side-cloud"
    (jobs_dir / f"{job_id}.json").write_text(json.dumps({
        "schema_version": 1,
        "job_id": job_id,
        "status": "running",
        "source_backend": "side_car",
        "target_backend": "cloud",
        "current_step": "data_copy",
        "started_at": "2026-05-28T10:00:00Z",
        "completed_at": None,
        "progress_pct": 30,
        "summary": None,
        "error": None,
    }))

    client = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = client.post(
        f"/api/admin/db/cancel/{job_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["cancelled"] is True

    from src.db_state_machine import BackendState, read_backend_state
    state, url = read_backend_state()
    assert state == BackendState.SIDE_CAR, f"expected SIDE_CAR after cancel, got {state.value}"
    assert url == "postgresql+psycopg://x:y@postgres:5432/agnes", \
        "source URL must be preserved on cancel"


def test_cancel_writes_sentinel_for_migrator(seeded_app, monkeypatch):
    """POST /cancel writes <job_id>.cancel beside the job JSON. The
    migrator subprocess polls this file at step boundaries (B2)."""
    data_dir = seeded_app["env"]["data_dir"]
    _patch_state_paths(monkeypatch, data_dir)

    jobs_dir = data_dir / "state" / "db-jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)

    job_id = "job-sentinel-test"
    job = {
        "schema_version": 1,
        "job_id": job_id,
        "status": "running",
        "source_backend": "duckdb",
        "target_backend": "side_car",
        "current_step": "data_copy",
        "started_at": "2026-05-28T10:00:00Z",
        "completed_at": None,
        "progress_pct": 30,
        "summary": None,
        "error": None,
    }
    (jobs_dir / f"{job_id}.json").write_text(json.dumps(job))

    client = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = client.post(
        f"/api/admin/db/cancel/{job_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text

    sentinel = jobs_dir / f"{job_id}.cancel"
    assert sentinel.exists(), "cancel endpoint must touch <job_id>.cancel for migrator"


# ---------------------------------------------------------------------------
# Task 2.4 — URL alias detection (BLOCKER B7)
# ---------------------------------------------------------------------------


def test_normalize_pg_url_default_port():
    """Default port 5432 inferred when absent."""
    from app.api.db_state import _normalize_pg_url
    assert _normalize_pg_url("postgresql+psycopg://x:y@host/agnes") == ("host", 5432, "agnes")
    assert _normalize_pg_url("postgresql+psycopg://x:y@host:5432/agnes") == ("host", 5432, "agnes")


def test_normalize_pg_url_case_insensitive_host_and_db():
    """Hosts and DB names are case-insensitive in PG conventions."""
    from app.api.db_state import _normalize_pg_url
    assert _normalize_pg_url("postgresql+psycopg://x:y@Host/Agnes") == ("host", 5432, "agnes")


def test_normalize_pg_url_ignores_credentials():
    """User/password are irrelevant to whether two URLs point at the
    same physical DB."""
    from app.api.db_state import _normalize_pg_url
    a = _normalize_pg_url("postgresql+psycopg://reader:r@host:5432/agnes")
    b = _normalize_pg_url("postgresql+psycopg://writer:w@host:5432/agnes")
    assert a == b


def test_normalize_pg_url_ignores_driver_choice():
    """``postgresql://`` and ``postgresql+psycopg://`` are the same
    target — driver picks the client library, not the DB."""
    from app.api.db_state import _normalize_pg_url
    a = _normalize_pg_url("postgresql://x:y@host/agnes")
    b = _normalize_pg_url("postgresql+psycopg://x:y@host/agnes")
    assert a == b


def test_urls_alias_default_port_omission():
    """B7 repro: omitted default port. String equality says no,
    alias says yes."""
    from app.api.db_state import _urls_alias
    a = "postgresql+psycopg://agnes:pw@cloud-sql-host/agnes"
    b = "postgresql+psycopg://agnes:pw@cloud-sql-host:5432/agnes"
    assert a != b  # string equal would let this through
    assert _urls_alias(a, b) is True  # alias check catches it


def test_urls_alias_different_database_returns_false():
    """Two PGs on same host different DB are NOT aliases."""
    from app.api.db_state import _urls_alias
    a = "postgresql+psycopg://x:y@host:5432/agnes"
    b = "postgresql+psycopg://x:y@host:5432/different-db"
    assert _urls_alias(a, b) is False


def test_urls_alias_different_host_returns_false():
    """Different hosts → different DBs."""
    from app.api.db_state import _urls_alias
    a = "postgresql+psycopg://x:y@host-a:5432/agnes"
    b = "postgresql+psycopg://x:y@host-b:5432/agnes"
    assert _urls_alias(a, b) is False


# ---------------------------------------------------------------------------
# Task 2.5 — _current_job_id includes pending (BLOCKER B8)
# ---------------------------------------------------------------------------


def test_current_job_id_returns_pending_jobs(tmp_path, monkeypatch):
    """_current_job_id must include status=pending (B8). Otherwise the
    GET /state response is None during the ~30s window between POST
    /migrate and the host applier picking up the job — UI shows 'no
    migration' while the state machine says *_in_progress."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    import json as _json
    jobs_dir = tmp_path / "state" / "db-jobs"
    jobs_dir.mkdir(parents=True)
    (jobs_dir / "job-pending.json").write_text(_json.dumps({
        "job_id": "job-pending",
        "status": "pending",
        "source_backend": "duckdb",
        "target_backend": "side_car",
    }))

    from app.api.db_state import _current_job_id
    assert _current_job_id() == "job-pending"


def test_current_job_id_returns_running_jobs(tmp_path, monkeypatch):
    """The original contract still holds — running jobs are surfaced."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    import json as _json
    jobs_dir = tmp_path / "state" / "db-jobs"
    jobs_dir.mkdir(parents=True)
    (jobs_dir / "job-running.json").write_text(_json.dumps({
        "job_id": "job-running",
        "status": "running",
        "source_backend": "duckdb",
        "target_backend": "side_car",
    }))

    from app.api.db_state import _current_job_id
    assert _current_job_id() == "job-running"


def test_current_job_id_ignores_terminal_jobs(tmp_path, monkeypatch):
    """Success / failed / cancelled MUST NOT be reported as current —
    those are history."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    import json as _json
    jobs_dir = tmp_path / "state" / "db-jobs"
    jobs_dir.mkdir(parents=True)
    for status in ("success", "failed", "cancelled"):
        (jobs_dir / f"job-{status}.json").write_text(_json.dumps({
            "job_id": f"job-{status}",
            "status": status,
        }))

    from app.api.db_state import _current_job_id
    assert _current_job_id() is None


def test_current_job_id_prefers_running_over_pending(tmp_path, monkeypatch):
    """When both exist (an out-of-order pickup window where one job is
    already running and another is queued behind it — should never
    happen because the lock prevents it, but the predicate ordering
    should be deterministic), running takes priority over pending so
    the UI shows the active work."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    import json as _json
    jobs_dir = tmp_path / "state" / "db-jobs"
    jobs_dir.mkdir(parents=True)
    (jobs_dir / "job-running.json").write_text(_json.dumps({
        "job_id": "job-running",
        "status": "running",
    }))
    (jobs_dir / "job-pending.json").write_text(_json.dumps({
        "job_id": "job-pending",
        "status": "pending",
    }))

    from app.api.db_state import _current_job_id
    assert _current_job_id() == "job-running"


def test_migrate_holds_lock_until_pending_json_durable(seeded_app, monkeypatch):
    """Regression: the migration flock MUST be held until the pending
    job JSON is on disk (B8). Otherwise a peer POST /migrate could
    sneak in between the state write and the JSON write, see no current
    job, and start a second migration onto the same in-progress state.

    Test approach: monkeypatch ``write_backend_state`` to capture
    whether the lock file is locked at the moment of the state write.
    Then assert the JSON is on disk after the request returns. The
    behaviour we're locking in is: while the request is mid-flight,
    the lock is held; by the time it returns 202, the JSON is durable."""
    import fcntl
    import json as _json

    data_dir = seeded_app["env"]["data_dir"]
    _patch_state_paths(monkeypatch, data_dir)
    monkeypatch.setenv("DATA_DIR", str(data_dir))

    # Capture lock-state during the state write.
    locked_during_write = {"yes": False}
    from src import db_state_machine
    original_write = db_state_machine.write_backend_state

    def watching_write(*args, **kwargs):
        # Try to acquire the SAME lock non-blocking by opening a fresh fd —
        # mimics a concurrent peer caller. fcntl.flock is per-fd (on macOS/
        # Linux in-process flock is per open-file-description), so a new fd
        # gets its own independent lock state.
        lock_path = db_state_machine._LOCK_PATH
        try:
            with open(lock_path, "w") as fd:
                try:
                    fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    # If we got here, the lock is NOT being held — bug.
                    locked_during_write["yes"] = False
                    fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
                except BlockingIOError:
                    locked_during_write["yes"] = True
        except FileNotFoundError:
            # Lock file not yet created — not held.
            locked_during_write["yes"] = False
        return original_write(*args, **kwargs)

    monkeypatch.setattr("src.db_state_machine.write_backend_state", watching_write)

    client = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = client.post(
        "/api/admin/db/migrate",
        json={"target": "side_car"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 202, resp.text

    # 1. Pending JSON is durable after the request returns.
    body = resp.json()
    job_id = body["job_id"]
    job_path = data_dir / "state" / "db-jobs" / f"{job_id}.json"
    assert job_path.exists(), "pending job JSON must be on disk after 202 response"
    job = _json.loads(job_path.read_text())
    assert job["status"] == "pending"

    # 2. The lock WAS held during the state-machine write.
    assert locked_during_write["yes"], (
        "flock must be held while in_progress state is written (B8)"
    )


def test_migrate_rejects_alias_url_with_400(seeded_app, monkeypatch):
    """End-to-end: POST /migrate where source and target alias the same PG
    database must return 400 (B7).

    Scenario: SIDE_CAR → CLOUD where the supplied cloud_url points at the
    same Postgres as the sidecar's URL (different user + default port
    omitted → string-unequal but alias-equal).  The pre-existing string
    equality check would have silently passed this request; the new alias
    check must block it.

    ``read_backend_state`` is patched at the endpoint module boundary so
    the app still authenticates against DuckDB (no live Postgres needed).
    """
    from src.db_state_machine import BackendState

    data_dir = seeded_app["env"]["data_dir"]
    _patch_state_paths(monkeypatch, data_dir)

    # Sidecar source URL — explicit port, credential user=agnes.
    sidecar_url = "postgresql+psycopg://agnes:pw@postgres:5432/agnes"

    # Patch the endpoint's read_backend_state so it sees SIDE_CAR + the
    # sidecar URL.  Auth machinery still runs against DuckDB (the fixture
    # didn't write an overlay, so use_pg() returns False throughout).
    monkeypatch.setattr(
        "app.api.db_state.read_backend_state",
        lambda: (BackendState.SIDE_CAR, sidecar_url),
    )

    client = seeded_app["client"]
    token = seeded_app["admin_token"]

    # cloud_url aliases the sidecar URL: same host/port/db, different
    # credentials, default port omitted → not string-equal, but alias-equal.
    aliasing_cloud_url = "postgresql+psycopg://reader:r@postgres/agnes"
    assert sidecar_url != aliasing_cloud_url  # string equality misses this

    resp = client.post(
        "/api/admin/db/migrate",
        json={"target": "cloud", "cloud_url": aliasing_cloud_url},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"].lower()
    assert "alias" in detail or "same" in detail


# ---------------------------------------------------------------------------
# Task 2.6 — GET /job redacts credentials (H1)
# ---------------------------------------------------------------------------


def test_get_job_redacts_target_url(seeded_app, monkeypatch):
    """H1 — GET /job/{id} must redact the password in target_url
    before returning the JSON. Raw file on disk keeps the unredacted
    URL (the applier needs it)."""
    data_dir = seeded_app["env"]["data_dir"]
    _patch_state_paths(monkeypatch, data_dir)

    jobs_dir = data_dir / "state" / "db-jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    job_id = "job-redact-target"
    (jobs_dir / f"{job_id}.json").write_text(json.dumps({
        "job_id": job_id,
        "status": "running",
        "source_backend": "duckdb",
        "target_backend": "side_car",
        "target_url": "postgresql+psycopg://agnes:supersecret@postgres:5432/agnes",
        "current_step": "data_copy",
        "progress_pct": 30,
    }))

    client = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = client.get(
        f"/api/admin/db/job/{job_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "supersecret" not in body["target_url"]
    assert "****" in body["target_url"]
    # Other fields are unchanged.
    assert body["status"] == "running"
    assert body["progress_pct"] == 30


def test_get_job_redacts_source_url(seeded_app, monkeypatch):
    """For PG -> PG transitions the source_url also carries the
    live DB password and must be redacted (H1)."""
    data_dir = seeded_app["env"]["data_dir"]
    _patch_state_paths(monkeypatch, data_dir)

    jobs_dir = data_dir / "state" / "db-jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    job_id = "job-redact-source"
    (jobs_dir / f"{job_id}.json").write_text(json.dumps({
        "job_id": job_id,
        "status": "running",
        "source_backend": "side_car",
        "target_backend": "cloud",
        "source_url": "postgresql+psycopg://x:sidecarpw@postgres:5432/agnes",
        "target_url": "postgresql+psycopg://y:cloudpw@cloud-host/agnes",
        "current_step": "data_copy",
    }))

    client = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = client.get(
        f"/api/admin/db/job/{job_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "sidecarpw" not in body["source_url"]
    assert "cloudpw" not in body["target_url"]


def test_get_job_handles_missing_url_keys(seeded_app, monkeypatch):
    """Jobs that don't carry a source_url (DuckDB -> PG) must not
    error from a redact-missing-key call."""
    data_dir = seeded_app["env"]["data_dir"]
    _patch_state_paths(monkeypatch, data_dir)

    jobs_dir = data_dir / "state" / "db-jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    job_id = "job-no-source"
    (jobs_dir / f"{job_id}.json").write_text(json.dumps({
        "job_id": job_id,
        "status": "running",
        "target_url": "postgresql+psycopg://agnes:pw@host/agnes",
        # no source_url
    }))

    client = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = client.get(
        f"/api/admin/db/job/{job_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "source_url" not in body or body.get("source_url") is None


def test_get_job_disk_file_is_unredacted(seeded_app, monkeypatch):
    """The raw JSON on disk MUST stay unredacted — the host applier
    subprocess reads it to invoke the migrator and needs the real
    password (H1). Only the HTTP response body is redacted."""
    data_dir = seeded_app["env"]["data_dir"]
    _patch_state_paths(monkeypatch, data_dir)

    jobs_dir = data_dir / "state" / "db-jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    job_id = "job-disk-intact"
    original = {
        "job_id": job_id,
        "status": "running",
        "target_url": "postgresql+psycopg://agnes:secretvalue@host/agnes",
    }
    job_file = jobs_dir / f"{job_id}.json"
    job_file.write_text(json.dumps(original))

    client = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = client.get(
        f"/api/admin/db/job/{job_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text

    # File on disk is untouched.
    on_disk = json.loads(job_file.read_text())
    assert on_disk["target_url"] == "postgresql+psycopg://agnes:secretvalue@host/agnes"
