"""Tests for request-id -> job-id log correlation (three-plane wave-2D,
Task 4 — ``app/job_correlation.py``).

Three layers, mirroring the module's docstring:

1. Unit tests for ``stamp_request_id``/``bind_request_id``/``unbind_request_id``
   directly against ``app.logging_config.request_id_var`` — no HTTP, no
   worker, just the contextvar plumbing.
2. An API-layer test: enqueueing a job through ``POST /api/jobs`` (inside a
   real request, so the request-id middleware has bound one) must stamp
   ``_enqueued_by_request`` into the stored payload, matching the response's
   ``X-Request-ID`` header.
3. A worker-layer test: running a job whose payload carries
   ``_enqueued_by_request`` must make that id observable via
   ``request_id_var.get()`` from *inside* the handler (which runs on a
   worker thread via ``asyncio.to_thread`` — context propagation is exactly
   what's being verified). A job with no such key must run to completion
   exactly as before (no exception, no id bound).
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. unit tests — app/job_correlation.py
# ---------------------------------------------------------------------------


class TestStampRequestId:
    def test_stamps_current_request_id_when_bound(self):
        from app.job_correlation import ENQUEUED_BY_REQUEST_KEY, stamp_request_id
        from app.logging_config import request_id_var

        token = request_id_var.set("req-abc-123")
        try:
            stamped = stamp_request_id({"a": 1})
        finally:
            request_id_var.reset(token)

        assert stamped == {"a": 1, ENQUEUED_BY_REQUEST_KEY: "req-abc-123"}

    def test_noop_when_no_request_id_bound(self):
        from app.job_correlation import ENQUEUED_BY_REQUEST_KEY, stamp_request_id
        from app.logging_config import request_id_var

        assert request_id_var.get() is None  # nothing bound in this test's context
        payload = {"a": 1}
        stamped = stamp_request_id(payload)
        assert stamped == {"a": 1}
        assert ENQUEUED_BY_REQUEST_KEY not in stamped

    def test_does_not_mutate_the_original_payload(self):
        from app.job_correlation import ENQUEUED_BY_REQUEST_KEY, stamp_request_id
        from app.logging_config import request_id_var

        token = request_id_var.set("req-xyz")
        try:
            original = {"a": 1}
            stamped = stamp_request_id(original)
        finally:
            request_id_var.reset(token)

        assert ENQUEUED_BY_REQUEST_KEY not in original
        assert stamped is not original


class TestBindUnbindRequestId:
    def test_binds_present_string_key(self):
        from app.job_correlation import bind_request_id, unbind_request_id
        from app.logging_config import request_id_var

        tok = bind_request_id({"_enqueued_by_request": "req-1"})
        try:
            assert request_id_var.get() == "req-1"
        finally:
            unbind_request_id(tok)
        assert request_id_var.get() is None

    @pytest.mark.parametrize(
        "payload_json",
        [
            None,
            {},
            {"_enqueued_by_request": None},
            {"_enqueued_by_request": 12345},
            {"_enqueued_by_request": ""},
            "not-a-dict",
        ],
    )
    def test_missing_or_malformed_key_binds_nothing(self, payload_json):
        from app.job_correlation import bind_request_id, unbind_request_id
        from app.logging_config import request_id_var

        assert request_id_var.get() is None
        tok = bind_request_id(payload_json)
        assert tok is None
        assert request_id_var.get() is None
        unbind_request_id(tok)  # must be a safe no-op
        assert request_id_var.get() is None


# ---------------------------------------------------------------------------
# 2. API layer — POST /api/jobs stamps the enqueuing request's id
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_job_kinds_registry():
    """The registry is a process-wide module dict — isolate each test."""
    from app.worker.registry import JOB_KINDS

    JOB_KINDS.clear()
    yield
    JOB_KINDS.clear()


@pytest.fixture
def fake_kind():
    from app.worker.registry import LIGHT_LANE, JobKind, register_kind

    register_kind(JobKind(name="fake-kind", handler=lambda payload: None, lane=LIGHT_LANE))
    return "fake-kind"


class TestEnqueueStampsRequestId:
    def test_enqueue_via_api_stamps_the_request_id(self, seeded_app, fake_kind):
        resp = seeded_app["client"].post(
            "/api/jobs",
            json={"kind": fake_kind, "payload": {"x": 1}},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 202, resp.text

        rid = resp.headers["x-request-id"]
        assert rid

        job = resp.json()["job"]
        assert job["payload"]["_enqueued_by_request"] == rid
        assert job["payload"]["x"] == 1

    def test_enqueue_honors_caller_supplied_x_request_id_header(self, seeded_app, fake_kind):
        resp = seeded_app["client"].post(
            "/api/jobs",
            json={"kind": fake_kind},
            headers={**_auth(seeded_app["admin_token"]), "X-Request-ID": "caller-supplied-id"},
        )
        assert resp.status_code == 202, resp.text
        assert resp.headers["x-request-id"] == "caller-supplied-id"
        job = resp.json()["job"]
        assert job["payload"]["_enqueued_by_request"] == "caller-supplied-id"


# ---------------------------------------------------------------------------
# 3. worker layer — running a job binds/unbinds request_id_var around the
#    handler, observable even though the handler runs on a to_thread worker
# ---------------------------------------------------------------------------


@pytest.fixture
def worker_db(tmp_path, monkeypatch):
    """Fresh system.duckdb under a tmp DATA_DIR, closed after the test.
    Mirrors the fixture of the same name in ``tests/test_worker_runtime.py``."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AGNES_DB_URL", raising=False)
    from src.db import close_system_db, get_system_db

    get_system_db()
    yield
    close_system_db()


async def _run_and_cancel(coro, duration_s: float) -> None:
    task = asyncio.create_task(coro)
    await asyncio.sleep(duration_s)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert task.done()


class TestWorkerBindsRequestId:
    def test_handler_observes_bound_request_id(self, worker_db):
        from app.logging_config import request_id_var
        from app.worker.registry import LIGHT_LANE, JobKind, register_kind
        from app.worker.runtime import worker_loop
        from src.repositories import jobs_repo

        observed: list[str | None] = []

        def handler(payload: dict) -> None:
            observed.append(request_id_var.get())

        register_kind(JobKind(name="rid_test", handler=handler, lane=LIGHT_LANE, lease_seconds=30))

        repo = jobs_repo()
        repo.enqueue("rid_test", {"_enqueued_by_request": "req-worker-42"})

        asyncio.run(_run_and_cancel(worker_loop(worker_id="test-worker", poll_interval_s=0.05), 0.4))

        assert observed == ["req-worker-42"]
        # Must not leak into this test's own context after the run.
        assert request_id_var.get() is None

    def test_missing_request_id_key_runs_job_fine_with_no_binding(self, worker_db):
        from app.logging_config import request_id_var
        from app.worker.registry import LIGHT_LANE, JobKind, register_kind
        from app.worker.runtime import worker_loop
        from src.repositories import jobs_repo

        observed: list[str | None] = []

        def handler(payload: dict) -> None:
            observed.append(request_id_var.get())

        register_kind(JobKind(name="no_rid_test", handler=handler, lane=LIGHT_LANE, lease_seconds=30))

        repo = jobs_repo()
        job = repo.enqueue("no_rid_test", {})  # no _enqueued_by_request key at all

        asyncio.run(_run_and_cancel(worker_loop(worker_id="test-worker", poll_interval_s=0.05), 0.4))

        assert observed == [None]
        row = repo.get(job["id"])
        assert row["status"] == "done"

    def test_malformed_request_id_key_does_not_break_job_execution(self, worker_db):
        from app.logging_config import request_id_var
        from app.worker.registry import LIGHT_LANE, JobKind, register_kind
        from app.worker.runtime import worker_loop
        from src.repositories import jobs_repo

        observed: list[str | None] = []

        def handler(payload: dict) -> None:
            observed.append(request_id_var.get())

        register_kind(JobKind(name="bad_rid_test", handler=handler, lane=LIGHT_LANE, lease_seconds=30))

        repo = jobs_repo()
        job = repo.enqueue("bad_rid_test", {"_enqueued_by_request": 999})  # malformed: not a string

        asyncio.run(_run_and_cancel(worker_loop(worker_id="test-worker", poll_interval_s=0.05), 0.4))

        assert observed == [None]
        row = repo.get(job["id"])
        assert row["status"] == "done"
