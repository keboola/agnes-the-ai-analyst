"""Tests for /api/jobs — the wave-2B durable job queue REST surface (Task 5).

Covers:

- 401 unauthenticated / 403 non-admin on every method.
- Scheduler shared-secret token (`SCHEDULER_API_TOKEN`) is accepted exactly
  like an admin session token — no special-casing in the endpoint, it rides
  the same `require_admin` dependency (see `app/auth/scheduler_token.py`).
- POST enqueue: 202 with the created job; unknown `kind` -> 400 listing
  registered kinds; idempotency_key dedup returns the same job unchanged.
- GET detail: 200 shape; 404 for an unknown id.
- GET list: status/kind filters, limit cap.

`JOB_KINDS` is a process-wide module dict populated at app-startup by
`register_all_kinds()` (`app/worker/kinds.py`), which never runs here since
`seeded_app`'s TestClient is built without entering the app's lifespan
context manager. Tests register their own fake kinds via `register_kind()`,
mirroring `tests/test_worker_runtime.py`.
"""

from __future__ import annotations

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def clean_job_kinds_registry():
    """The registry is a process-wide module dict — isolate each test."""
    from app.worker.registry import JOB_KINDS

    JOB_KINDS.clear()
    yield
    JOB_KINDS.clear()


@pytest.fixture
def fake_kind():
    """Register a single fake job kind, return its name."""
    from app.worker.registry import LIGHT_LANE, JobKind, register_kind

    register_kind(JobKind(name="fake-kind", handler=lambda payload: None, lane=LIGHT_LANE))
    return "fake-kind"


class TestEnqueueJob:
    def test_unauthenticated_returns_401(self, seeded_app, fake_kind):
        resp = seeded_app["client"].post("/api/jobs", json={"kind": fake_kind})
        assert resp.status_code == 401

    def test_non_admin_returns_403(self, seeded_app, fake_kind):
        resp = seeded_app["client"].post(
            "/api/jobs", json={"kind": fake_kind}, headers=_auth(seeded_app["analyst_token"])
        )
        assert resp.status_code == 403

    def test_admin_enqueue_returns_202(self, seeded_app, fake_kind):
        resp = seeded_app["client"].post(
            "/api/jobs",
            json={"kind": fake_kind, "payload": {"x": 1}},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 202, resp.text
        job = resp.json()["job"]
        assert job["kind"] == fake_kind
        assert job["status"] == "queued"
        # `_enqueued_by_request` is stamped in by `app.job_correlation.stamp_request_id`
        # (Task 4, log correlation) — the caller's payload plus that one reserved key.
        assert job["payload"]["x"] == 1
        assert job["payload"]["_enqueued_by_request"]
        assert job["attempts"] == 0
        assert job["id"]

    def test_scheduler_token_is_accepted(self, seeded_app, fake_kind, monkeypatch):
        """The scheduler's shared secret rides the same require_admin gate as
        a human admin session token — no separate code path in the endpoint."""
        token = "scheduler-shared-secret-token-min-len-32chars"
        monkeypatch.setenv("SCHEDULER_API_TOKEN", token)

        resp = seeded_app["client"].post(
            "/api/jobs",
            json={"kind": fake_kind},
            headers=_auth(token),
        )
        assert resp.status_code == 202, resp.text

    def test_unknown_kind_returns_400_listing_registered_kinds(self, seeded_app, fake_kind):
        resp = seeded_app["client"].post(
            "/api/jobs",
            json={"kind": "does-not-exist"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "does-not-exist" in detail
        assert fake_kind in detail

    def test_idempotency_key_dedups(self, seeded_app, fake_kind):
        c = seeded_app["client"]
        headers = _auth(seeded_app["admin_token"])
        first = c.post(
            "/api/jobs",
            json={"kind": fake_kind, "idempotency_key": "dedup-key-1"},
            headers=headers,
        )
        assert first.status_code == 202
        second = c.post(
            "/api/jobs",
            json={"kind": fake_kind, "idempotency_key": "dedup-key-1"},
            headers=headers,
        )
        assert second.status_code == 202
        assert first.json()["job"]["id"] == second.json()["job"]["id"]


class TestGetJob:
    def test_unauthenticated_returns_401(self, seeded_app):
        resp = seeded_app["client"].get("/api/jobs/does-not-exist")
        assert resp.status_code == 401

    def test_non_admin_returns_403(self, seeded_app):
        resp = seeded_app["client"].get("/api/jobs/does-not-exist", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 403

    def test_unknown_job_returns_404(self, seeded_app):
        resp = seeded_app["client"].get("/api/jobs/does-not-exist", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 404

    def test_admin_get_returns_job_shape(self, seeded_app, fake_kind):
        c = seeded_app["client"]
        headers = _auth(seeded_app["admin_token"])
        created = c.post("/api/jobs", json={"kind": fake_kind, "payload": {"a": "b"}}, headers=headers)
        job_id = created.json()["job"]["id"]

        resp = c.get(f"/api/jobs/{job_id}", headers=headers)
        assert resp.status_code == 200
        job = resp.json()["job"]
        assert job["id"] == job_id
        assert job["kind"] == fake_kind
        # See test_admin_enqueue_returns_202 for why `_enqueued_by_request` is present too.
        assert job["payload"]["a"] == "b"
        assert job["payload"]["_enqueued_by_request"]
        assert job["status"] == "queued"
        assert "created_at" in job


class TestListJobs:
    def test_unauthenticated_returns_401(self, seeded_app):
        resp = seeded_app["client"].get("/api/jobs")
        assert resp.status_code == 401

    def test_non_admin_returns_403(self, seeded_app):
        resp = seeded_app["client"].get("/api/jobs", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 403

    def test_admin_list_returns_jobs(self, seeded_app, fake_kind):
        c = seeded_app["client"]
        headers = _auth(seeded_app["admin_token"])
        c.post("/api/jobs", json={"kind": fake_kind}, headers=headers)
        c.post("/api/jobs", json={"kind": fake_kind}, headers=headers)

        resp = c.get("/api/jobs", headers=headers)
        assert resp.status_code == 200
        jobs = resp.json()["jobs"]
        assert len(jobs) >= 2
        assert all(j["kind"] == fake_kind for j in jobs)

    def test_filters_by_status_and_kind(self, seeded_app, fake_kind):
        from app.worker.registry import LIGHT_LANE, JobKind, register_kind

        register_kind(JobKind(name="other-kind", handler=lambda payload: None, lane=LIGHT_LANE))

        c = seeded_app["client"]
        headers = _auth(seeded_app["admin_token"])
        c.post("/api/jobs", json={"kind": fake_kind}, headers=headers)
        c.post("/api/jobs", json={"kind": "other-kind"}, headers=headers)

        resp = c.get("/api/jobs", params={"kind": fake_kind}, headers=headers)
        assert resp.status_code == 200
        jobs = resp.json()["jobs"]
        assert all(j["kind"] == fake_kind for j in jobs)

        resp = c.get("/api/jobs", params={"status": "queued"}, headers=headers)
        assert resp.status_code == 200
        assert all(j["status"] == "queued" for j in resp.json()["jobs"])

        resp = c.get("/api/jobs", params={"status": "done"}, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["jobs"] == []

    def test_limit_caps_results(self, seeded_app, fake_kind):
        c = seeded_app["client"]
        headers = _auth(seeded_app["admin_token"])
        for _ in range(3):
            c.post("/api/jobs", json={"kind": fake_kind}, headers=headers)

        resp = c.get("/api/jobs", params={"limit": 1}, headers=headers)
        assert resp.status_code == 200
        assert len(resp.json()["jobs"]) == 1
