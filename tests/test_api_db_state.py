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
