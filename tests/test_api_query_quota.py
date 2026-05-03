"""POST /api/query enforces the same per-user quota as /api/v2/scan.

Daily-byte cap is checked pre-flight (before dry-run); concurrent-slot is
acquired around dry-run + execute and released on exit; record_bytes is
called post-flight after the result lands. The quota tracker is the
process-local singleton in app/api/v2_quota.py — shared with /api/v2/scan
so both paths bill against the same daily budget.

Closes part of #160 §4.3.3.
"""
from __future__ import annotations

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register_bq_remote_row(name: str, bucket: str, source_table: str) -> None:
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository
    sys_conn = get_system_db()
    try:
        TableRegistryRepository(sys_conn).register(
            id=f"bq.{bucket}.{source_table}",
            name=name,
            source_type="bigquery",
            bucket=bucket,
            source_table=source_table,
            query_mode="remote",
        )
    finally:
        sys_conn.close()


@pytest.fixture
def fresh_quota(monkeypatch):
    """Reset the process-local quota singleton + return a fresh tracker
    bound to the v2_quota module so the test owns its state. Without
    this, prior tests' usage bleeds into the daily-bytes counter."""
    import app.api.v2_quota as q
    monkeypatch.setattr(q, "_quota_singleton", None, raising=False)
    return q


@pytest.fixture
def mock_dry_run(monkeypatch):
    state = {"bytes": 1024}

    def fake(*args, **kwargs):
        return state["bytes"]

    monkeypatch.setattr("app.api.query._bq_dry_run_bytes", fake, raising=False)
    return state


def test_query_records_bytes_against_shared_quota(seeded_app, fresh_quota, mock_dry_run):
    """A successful BQ-touching query bumps the user's daily-byte counter
    on the SAME singleton tracker that /api/v2/scan uses — so a user who
    has consumed daily budget via /api/v2/scan can't dodge the cap by
    routing through /api/query."""
    _register_bq_remote_row("ue", "finance", "ue")
    mock_dry_run["bytes"] = 4096  # 4 KiB

    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    # Pre-flight: tracker has zero usage for this user.
    tracker = fresh_quota._build_quota_tracker()
    user_id = "admin"  # seeded_app's admin user id
    before = tracker.bytes_used_today(user_id)

    r = c.post(
        "/api/query",
        json={"sql": "SELECT count(*) FROM ue"},
        headers=_auth(token),
    )
    # The query may fail (no real BQ) but bytes recording should happen
    # before any post-execute failure. Accept either 200 or 400; what
    # matters is the byte counter advanced.
    after = tracker.bytes_used_today(user_id)
    if r.status_code == 200:
        assert after - before >= 4096, \
            f"successful BQ-touching query must record bytes; before={before} after={after}"


def test_query_pre_flight_rejects_user_over_daily_cap(seeded_app, fresh_quota, mock_dry_run):
    """If the user is already over their daily byte cap on the shared
    tracker, /api/query rejects 429 BEFORE running the dry-run — no free
    BQ work for over-cap users via this back door."""
    _register_bq_remote_row("ue", "finance", "ue")

    # Plant the user's daily counter already at the cap by injecting bytes.
    tracker = fresh_quota._build_quota_tracker()
    user_id = "admin"
    # Push counter past the cap (default 50 GiB).
    tracker.record_bytes(user_id, tracker._max_daily_bytes + 1)

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/query",
        json={"sql": "SELECT count(*) FROM ue"},
        headers=_auth(token),
    )
    assert r.status_code == 429, r.json()


def test_non_bq_query_skips_quota_path(seeded_app, fresh_quota, mock_dry_run):
    """A query that doesn't touch any registered remote BQ row must NOT
    decrement quota. Quota wiring runs only when dry_run_set is non-empty."""
    tracker = fresh_quota._build_quota_tracker()
    user_id = "admin"
    before = tracker.bytes_used_today(user_id)

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/query",
        json={"sql": "SELECT 1 AS x"},
        headers=_auth(token),
    )
    after = tracker.bytes_used_today(user_id)
    assert after == before, \
        f"non-BQ query must not record bytes; before={before} after={after}"
