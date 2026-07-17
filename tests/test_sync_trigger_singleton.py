"""POST /api/sync/trigger's job-queue dedup + the in-`_run_sync` singleton
guard it wraps.

wave-2B: the trigger handler no longer fast-fails on `_sync_lock.locked()`
(that lock is process-local and invisible to the worker process that now
actually runs `_run_sync` off the job queue — see the module docstring in
`app/api/sync.py`). "Already in progress" is now determined by the
`data-refresh` job queue's own idempotency dedup (shared `"sync"` key with
the scheduler's cadence-driven trigger). These tests cover:

- `_run_sync`'s own in-process singleton guard (defense in depth) and its
  now-honest return value (`True`/`False`/`None`).
- The trigger handler's enqueue + dedup behavior via a fake `jobs_repo()`.
"""

from unittest.mock import patch

import pytest

from app.api import sync as sync_module


@pytest.fixture(autouse=True)
def reset_sync_lock():
    """Make sure each test starts with a free lock — and never leaves one
    held even if an assertion fires mid-test. Also wipe the trigger-hold
    timestamp so an earlier test's ``_recent_trigger_at`` doesn't
    silently pin the next test's ``/api/sync/status`` at locked=True."""
    if sync_module._sync_lock.locked():
        sync_module._sync_lock.release()
    sync_module._recent_trigger_at = 0.0
    yield
    if sync_module._sync_lock.locked():
        sync_module._sync_lock.release()
    sync_module._recent_trigger_at = 0.0


def test_run_sync_skips_when_lock_held(capsys):
    """When `_sync_lock` is already held, `_run_sync` no-ops with a log
    line instead of starting a second extractor subprocess, and returns
    `None` (not a failure — the in-flight run produces its own outcome)."""
    sync_module._sync_lock.acquire()

    # Patch the heavy parts so a successful path would otherwise execute.
    # Reaching any of them while the lock is held would be the bug.
    with (
        patch("app.api.sync.subprocess.run") as run_mock,
        patch("app.instance_config.get_data_source_type") as src_mock,
    ):
        result = sync_module._run_sync(tables=None)

    assert not run_mock.called, "extractor subprocess must not run when lock is held"
    assert not src_mock.called, "_run_sync must short-circuit before reading config"
    assert result is None

    captured = capsys.readouterr()
    assert "another sync is already in flight" in captured.err


def test_run_sync_releases_lock_on_exception():
    """Even if the body throws, the lock must release so the next sync can
    run, and the fatal path is reported honestly via a `False` return (so
    `app.worker.kinds._run_data_refresh` can raise and the job records a
    failure instead of always finalizing 'done').

    `_run_sync` imports `get_data_source_type` lazily inside the body, so
    we patch the source module rather than the re-export in `app.api.sync`.
    """
    with patch(
        "app.instance_config.get_data_source_type",
        side_effect=RuntimeError("boom"),
    ):
        result = sync_module._run_sync(tables=None)

    assert result is False
    assert not sync_module._sync_lock.locked()


# ---- Trigger endpoint: job-queue enqueue + dedup ---------------------------


class _FakeJobsRepo:
    """Stands in for `src.repositories.jobs_repo()` — mirrors the real
    `JobsRepository.enqueue()` dedup contract (matching kind +
    idempotency_key with status queued/running returns the existing row
    unchanged) without touching a real DuckDB/Postgres connection, so the
    trigger handler's branch logic (new job vs. deduped job) can be tested
    deterministically."""

    def __init__(self, existing_job: dict | None = None):
        self.existing_job = existing_job
        self.enqueue_calls: list[dict] = []
        self._next_id = 0

    def list(self, *, kind=None, status=None, limit=50):
        job = self.existing_job
        if job is not None and job["kind"] == kind and job["status"] == status:
            return [job]
        return []

    def enqueue(self, kind, payload, *, idempotency_key=None, **kwargs):
        self.enqueue_calls.append({"kind": kind, "payload": payload, "idempotency_key": idempotency_key})
        job = self.existing_job
        if (
            job is not None
            and job["kind"] == kind
            and job.get("idempotency_key") == idempotency_key
            and job["status"] in ("queued", "running")
        ):
            return job
        self._next_id += 1
        return {
            "id": f"new-job-{self._next_id}",
            "kind": kind,
            "status": "queued",
            "idempotency_key": idempotency_key,
            "payload_json": payload,
        }


def _make_client():
    """Stand up a minimal FastAPI app exposing the sync router with auth
    bypassed. Returns the TestClient."""
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from app.auth.access import require_admin

    app = FastAPI()
    app.include_router(sync_module.router)
    app.dependency_overrides[require_admin] = lambda: {"id": "test", "email": "t@e"}
    return TestClient(app)


def test_trigger_enqueues_new_job_and_returns_job_id():
    """No in-flight `data-refresh` job → a new one is enqueued and its
    `job_id` is surfaced in the (still-200, same-shape) response."""
    fake_repo = _FakeJobsRepo(existing_job=None)
    client = _make_client()
    with patch("app.api.sync.jobs_repo", lambda: fake_repo):
        resp = client.post("/api/sync/trigger")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "triggered"
    assert body["job_id"] == "new-job-1"
    assert fake_repo.enqueue_calls == [
        {"kind": "data-refresh", "payload": {"tables": None, "source": None}, "idempotency_key": "sync"}
    ]


def test_trigger_dedups_to_existing_job_returns_409_with_job_id():
    """A `data-refresh` job already queued under the shared `"sync"` key →
    the handler must NOT report a fresh trigger. 409 is kept (CLI +
    admin web UI both branch on it today), now carrying `job_id` of the
    existing job so callers can poll it instead of assuming a new run
    started."""
    existing = {
        "id": "existing-job-1",
        "kind": "data-refresh",
        "status": "queued",
        "idempotency_key": "sync",
        "payload_json": {"tables": None, "source": None},
    }
    fake_repo = _FakeJobsRepo(existing_job=existing)
    client = _make_client()
    with patch("app.api.sync.jobs_repo", lambda: fake_repo):
        resp = client.post("/api/sync/trigger")

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "sync_already_in_progress"
    assert detail["job_id"] == "existing-job-1"
    # No second job created — enqueue() deduped, same as the real repo.
    assert len(fake_repo.enqueue_calls) == 1


def test_trigger_dedups_when_existing_job_is_running():
    """Same dedup path, but the in-flight job has already been claimed by
    a worker (`status='running'`) rather than still `'queued'`."""
    existing = {
        "id": "existing-job-2",
        "kind": "data-refresh",
        "status": "running",
        "idempotency_key": "sync",
        "payload_json": {"tables": None, "source": None},
    }
    fake_repo = _FakeJobsRepo(existing_job=existing)
    client = _make_client()
    with patch("app.api.sync.jobs_repo", lambda: fake_repo):
        resp = client.post("/api/sync/trigger")

    assert resp.status_code == 409
    assert resp.json()["detail"]["job_id"] == "existing-job-2"


def test_second_trigger_during_queued_dedups_to_same_job_id():
    """End-to-end shape of the dedup contract: trigger once (get job_id X),
    trigger again while it's still queued (get 409 with the SAME job_id
    X) — mirrors the real enqueue()'s behavior across two calls sharing
    one fake repo instance."""
    fake_repo = _FakeJobsRepo(existing_job=None)
    client = _make_client()
    with patch("app.api.sync.jobs_repo", lambda: fake_repo):
        first = client.post("/api/sync/trigger")
        assert first.status_code == 200
        job_id = first.json()["job_id"]

        # Simulate the job now being visible as in-flight (what a second,
        # real HTTP call would see once the row is committed).
        fake_repo.existing_job = {
            "id": job_id,
            "kind": "data-refresh",
            "status": "queued",
            "idempotency_key": "sync",
            "payload_json": {"tables": None, "source": None},
        }

        second = client.post("/api/sync/trigger")

    assert second.status_code == 409
    assert second.json()["detail"]["job_id"] == job_id


# ---- body shape acceptance -------------------------------------------------


@pytest.mark.parametrize(
    "body,expected_tables",
    [
        (None, None),  # no body
        ([], []),  # empty array
        (["kbc_job"], ["kbc_job"]),  # bare array
        (["a", "b", "c"], ["a", "b", "c"]),
        ({"tables": None}, None),  # explicit null
        ({"tables": []}, []),
        ({"tables": ["kbc_job"]}, ["kbc_job"]),  # object form
        ({"tables": ["a", "b"], "extra": "x"}, ["a", "b"]),  # extra keys ignored
    ],
)
def test_trigger_accepts_both_body_shapes(body, expected_tables):
    """Both ``["x", "y"]`` and ``{"tables": ["x", "y"]}`` (and `null` /
    no body) reach the enqueued job's payload with the same `tables`
    value. Lets older clients (raw array) and newer ones (object matching
    the response payload shape) both work."""
    fake_repo = _FakeJobsRepo(existing_job=None)
    client = _make_client()
    with patch("app.api.sync.jobs_repo", lambda: fake_repo):
        if body is None:
            resp = client.post("/api/sync/trigger")
        else:
            resp = client.post("/api/sync/trigger", json=body)
    assert resp.status_code == 200, resp.text
    assert fake_repo.enqueue_calls == [
        {"kind": "data-refresh", "payload": {"tables": expected_tables, "source": None}, "idempotency_key": "sync"}
    ]


@pytest.mark.parametrize(
    "bad_body",
    [
        "kbc_job",  # bare string
        42,  # number
        {"tables": "kbc_job"},  # tables as string, not array
        {"tables": [1, 2, 3]},  # tables entries not strings
        [1, 2, 3],  # array of ints
        [{"id": "x"}],  # array of objects
    ],
)
def test_trigger_rejects_malformed_bodies(bad_body):
    """Anything that isn't a list-of-strings, an object with a
    list-of-strings under `tables`, or null/missing returns 422 with a
    structured detail — never silently treated as 'sync everything', and
    never reaches the job queue."""
    fake_repo = _FakeJobsRepo(existing_job=None)
    client = _make_client()
    with patch("app.api.sync.jobs_repo", lambda: fake_repo):
        resp = client.post("/api/sync/trigger", json=bad_body)
    assert resp.status_code == 422, resp.text
    assert not fake_repo.enqueue_calls


# ---- /api/sync/status (auto-upgrade defer probe) --------------------------


def test_sync_status_unlocked_returns_locked_false():
    """Default state: no sync running → ``{"locked": false}``. No auth
    required (host-side cron probes from outside the auth boundary)."""
    from fastapi.testclient import TestClient
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(sync_module.router)
    client = TestClient(app)

    if sync_module._sync_lock.locked():
        sync_module._sync_lock.release()

    resp = client.get("/api/sync/status")
    assert resp.status_code == 200
    assert resp.json() == {"locked": False}


def test_sync_status_locked_returns_locked_true():
    """Held lock → ``{"locked": true}``. agnes-auto-upgrade.sh greps for
    `"locked":true` to defer the recreate, so the wire format must be
    exactly this shape."""
    from fastapi.testclient import TestClient
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(sync_module.router)
    client = TestClient(app)

    sync_module._sync_lock.acquire()
    try:
        resp = client.get("/api/sync/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"locked": True}
    finally:
        sync_module._sync_lock.release()


def test_sync_status_does_not_require_auth():
    """No `require_admin` / `get_current_user` dependency — the host's
    cron has no PAT and shouldn't need one for a status check."""
    from fastapi.testclient import TestClient
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(sync_module.router)
    client = TestClient(app)
    # No auth headers at all.
    resp = client.get("/api/sync/status")
    assert resp.status_code == 200


def test_sync_status_trigger_hold_window_reports_locked_after_trigger():
    """Race-protection: even when ``_sync_lock`` is NOT yet held (the
    worker hasn't claimed the enqueued job yet), a recent
    ``_recent_trigger_at`` timestamp within ``_TRIGGER_HOLD_SEC`` must
    report ``{"locked": true}``. Without this, an auto-upgrade defer probe
    firing in the window between the trigger handler's 200 response and
    the worker's claim would see locked=False and SIGKILL the spawning
    extractor."""
    import time as _time

    if sync_module._sync_lock.locked():
        sync_module._sync_lock.release()
    # Stamp a fresh trigger time, then immediately probe.
    sync_module._recent_trigger_at = _time.monotonic()
    try:
        from fastapi.testclient import TestClient
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(sync_module.router)
        client = TestClient(app)
        resp = client.get("/api/sync/status")
        assert resp.status_code == 200
        assert resp.json() == {"locked": True}, (
            "trigger-hold window not honored — auto-upgrade defer probe would race the worker's job claim"
        )
    finally:
        sync_module._recent_trigger_at = 0.0


def test_sync_status_trigger_hold_window_expires():
    """Trigger-hold reports locked only for ``_TRIGGER_HOLD_SEC`` — a
    stale timestamp past the window must NOT pin the probe at True
    forever. Without expiry, a single trigger would block all
    auto-upgrades indefinitely."""
    if sync_module._sync_lock.locked():
        sync_module._sync_lock.release()
    # Stamp a timestamp ``_TRIGGER_HOLD_SEC + 5`` seconds in the past.
    import time as _time

    sync_module._recent_trigger_at = _time.monotonic() - sync_module._TRIGGER_HOLD_SEC - 5
    try:
        from fastapi.testclient import TestClient
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(sync_module.router)
        client = TestClient(app)
        resp = client.get("/api/sync/status")
        assert resp.json() == {"locked": False}
    finally:
        sync_module._recent_trigger_at = 0.0
