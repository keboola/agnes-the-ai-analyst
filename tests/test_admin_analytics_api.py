"""Tests for POST /api/admin/analytics/migrate (wave-2G Task 6).

Covers:

- 401 unauthenticated / 403 non-admin.
- `to` validation (must be "ducklake" or "legacy").
- `to="ducklake"`: prerequisite validation runs; 400 with the full problem
  list on failure (no job enqueued), 202 + enqueued job on success.
- `to="legacy"`: prerequisite validation is skipped entirely (legacy has
  none).
- A second in-flight call dedupes onto the same job and returns 409.

This endpoint calls `jobs_repo().enqueue()` directly (not through
`POST /api/jobs`), so — unlike `tests/test_jobs_api.py` — it needs no
`JOB_KINDS` registration to succeed; kind validation is a concern of the
generic `/api/jobs` endpoint only.
"""

from __future__ import annotations

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _no_real_prerequisite_probe(monkeypatch):
    """Default: prerequisites pass without touching any real DuckLake
    extension/Postgres — individual tests override this via monkeypatch
    when they want to exercise the failure path."""
    monkeypatch.setattr(
        "src.ducklake_session.validate_ducklake_migration_prerequisites",
        lambda: [],
    )


class TestMigrateAuth:
    def test_unauthenticated_returns_401(self, seeded_app):
        resp = seeded_app["client"].post("/api/admin/analytics/migrate", json={"to": "legacy"})
        assert resp.status_code == 401

    def test_non_admin_returns_403(self, seeded_app):
        resp = seeded_app["client"].post(
            "/api/admin/analytics/migrate",
            json={"to": "legacy"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403


class TestMigrateValidation:
    def test_invalid_target_returns_400(self, seeded_app):
        resp = seeded_app["client"].post(
            "/api/admin/analytics/migrate",
            json={"to": "bogus"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 400
        assert "bogus" in resp.json()["detail"]

    def test_ducklake_prerequisites_failure_returns_400_and_lists_problems(self, seeded_app, monkeypatch):
        monkeypatch.setattr(
            "src.ducklake_session.validate_ducklake_migration_prerequisites",
            lambda: ["the ducklake extension could not be loaded", "catalog unreachable"],
        )
        resp = seeded_app["client"].post(
            "/api/admin/analytics/migrate",
            json={"to": "ducklake"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"]
        assert detail["error"] == "ducklake_prerequisites_failed"
        assert detail["problems"] == [
            "the ducklake extension could not be loaded",
            "catalog unreachable",
        ]

    def test_ducklake_prerequisites_failure_does_not_enqueue_a_job(self, seeded_app, monkeypatch):
        monkeypatch.setattr(
            "src.ducklake_session.validate_ducklake_migration_prerequisites",
            lambda: ["broken"],
        )
        before = seeded_app["client"].get("/api/jobs", headers=_auth(seeded_app["admin_token"])).json()
        seeded_app["client"].post(
            "/api/admin/analytics/migrate",
            json={"to": "ducklake"},
            headers=_auth(seeded_app["admin_token"]),
        )
        after = seeded_app["client"].get("/api/jobs", headers=_auth(seeded_app["admin_token"])).json()
        assert len(after["jobs"]) == len(before["jobs"])

    def test_legacy_target_never_calls_prerequisite_validation(self, seeded_app, monkeypatch):
        def _explode():
            raise AssertionError("validate_ducklake_migration_prerequisites must not run for to=legacy")

        monkeypatch.setattr("src.ducklake_session.validate_ducklake_migration_prerequisites", _explode)

        resp = seeded_app["client"].post(
            "/api/admin/analytics/migrate",
            json={"to": "legacy"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 202, resp.text


class TestMigrateEnqueue:
    def test_ducklake_success_enqueues_job_and_returns_202(self, seeded_app):
        resp = seeded_app["client"].post(
            "/api/admin/analytics/migrate",
            json={"to": "ducklake"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["status"] == "triggered"
        assert body["to"] == "ducklake"
        assert body["job_id"]
        assert "restart" in body["message"]

        job_resp = seeded_app["client"].get(f"/api/jobs/{body['job_id']}", headers=_auth(seeded_app["admin_token"]))
        assert job_resp.status_code == 200
        job = job_resp.json()["job"]
        assert job["kind"] == "analytics-migrate"
        assert job["payload"]["to"] == "ducklake"

    def test_legacy_success_message_mentions_materialized_tables(self, seeded_app):
        resp = seeded_app["client"].post(
            "/api/admin/analytics/migrate",
            json={"to": "legacy"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["to"] == "legacy"
        assert "re-materialized" in body["message"] or "materialize" in body["message"]

    def test_second_concurrent_call_dedupes_and_returns_409(self, seeded_app):
        first = seeded_app["client"].post(
            "/api/admin/analytics/migrate",
            json={"to": "ducklake"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert first.status_code == 202, first.text
        first_job_id = first.json()["job_id"]

        second = seeded_app["client"].post(
            "/api/admin/analytics/migrate",
            json={"to": "legacy"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert second.status_code == 409, second.text
        assert second.json()["detail"]["job_id"] == first_job_id
