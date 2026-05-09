"""Stuck-review reaper (#7 from PR #233 review).

A submission stuck at ``status='pending_llm'`` past the configured
grace gets flipped to ``review_error`` so admin can retry. Sweeps
every 15 min via scheduler.

Tests:
* row older than grace → flipped + audit written
* row younger than grace → untouched
* no pending_llm rows → no-op
* grace_seconds=0 → reaper short-circuits
* idempotent: running twice doesn't double-flip
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from src import db as src_db
from src.repositories.store_entities import StoreEntitiesRepository
from src.repositories.store_submissions import StoreSubmissionsRepository
from src.repositories.users import UserRepository
from src.store_guardrails.reaper import reap_stuck_llm_reviews


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    src_db._system_db_conn = None
    src_db._system_db_path = None
    c = src_db.get_system_db()
    yield c
    c.close()


def _seed_pending(conn, name: str, age_seconds: int) -> str:
    """Stage a pending_llm submission whose created_at is `age_seconds`
    in the past. Returns the submission id."""
    UserRepository(conn).create(
        id=f"u-{name}", email=f"{name}@x.com", name=name,
    )
    StoreEntitiesRepository(conn).create(
        id=f"e-{name}", owner_user_id=f"u-{name}", owner_username=name,
        type="skill", name=name, description="x", category=None,
        version="1.0.0", file_size=10, visibility_status="pending",
    )
    sub_id = StoreSubmissionsRepository(conn).create(
        submitter_id=f"u-{name}", submitter_email=f"{name}@x.com",
        type="skill", name=name, version="1.0.0",
        status="pending_llm", entity_id=f"e-{name}",
        inline_checks={"manifest": {"status": "pass"}},
    )
    # Backdate created_at — repo timestamps with NOW(), so we override.
    backdated = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    conn.execute(
        "UPDATE store_submissions SET created_at = ? WHERE id = ?",
        [backdated, sub_id],
    )
    return sub_id


class TestReaper:
    def test_reaps_old_pending_llm(self, conn):
        sub_id = _seed_pending(conn, "old-stuck", age_seconds=3600)
        result = reap_stuck_llm_reviews(conn, grace_seconds=1800)
        assert result["reaped"] == 1

        sub = StoreSubmissionsRepository(conn).get(sub_id)
        assert sub["status"] == "review_error"
        assert (sub["llm_findings"] or {}).get("error") == "timeout_or_crash"

        audits = conn.execute(
            "SELECT params FROM audit_log "
            "WHERE resource = ? AND action = 'store.submission.review_error'",
            [f"store_submission:{sub_id}"],
        ).fetchall()
        assert audits, "missing reaper audit row"

    def test_skips_recent_pending_llm(self, conn):
        sub_id = _seed_pending(conn, "fresh", age_seconds=60)  # 1 min old
        result = reap_stuck_llm_reviews(conn, grace_seconds=1800)
        assert result["reaped"] == 0

        sub = StoreSubmissionsRepository(conn).get(sub_id)
        assert sub["status"] == "pending_llm"

    def test_grace_zero_short_circuits(self, conn):
        sub_id = _seed_pending(conn, "old-but-disabled", age_seconds=10000)
        result = reap_stuck_llm_reviews(conn, grace_seconds=0)
        assert result["skipped"] is True

        sub = StoreSubmissionsRepository(conn).get(sub_id)
        assert sub["status"] == "pending_llm"

    def test_idempotent(self, conn):
        sub_id = _seed_pending(conn, "twice", age_seconds=3600)
        first = reap_stuck_llm_reviews(conn, grace_seconds=1800)
        second = reap_stuck_llm_reviews(conn, grace_seconds=1800)
        assert first["reaped"] == 1
        assert second["reaped"] == 0

        sub = StoreSubmissionsRepository(conn).get(sub_id)
        assert sub["status"] == "review_error"

    def test_no_pending_rows_is_noop(self, conn):
        result = reap_stuck_llm_reviews(conn, grace_seconds=1800)
        assert result["reaped"] == 0
        assert result["skipped"] is False

    def test_does_not_flip_other_statuses(self, conn):
        """Approved / blocked / overridden rows older than grace must
        not be touched — the reaper is scoped to pending_llm only."""
        UserRepository(conn).create(id="u1", email="u1@x.com", name="u1")
        StoreEntitiesRepository(conn).create(
            id="e1", owner_user_id="u1", owner_username="u1",
            type="skill", name="approved-old", description="x",
            category=None, version="1.0.0", file_size=10,
            visibility_status="approved",
        )
        sub_id = StoreSubmissionsRepository(conn).create(
            submitter_id="u1", submitter_email="u1@x.com",
            type="skill", name="approved-old", version="1.0.0",
            status="approved", entity_id="e1",
            inline_checks={"manifest": {"status": "pass"}},
        )
        backdated = datetime.now(timezone.utc) - timedelta(hours=24)
        conn.execute(
            "UPDATE store_submissions SET created_at = ? WHERE id = ?",
            [backdated, sub_id],
        )

        result = reap_stuck_llm_reviews(conn, grace_seconds=1800)
        assert result["reaped"] == 0

        sub = StoreSubmissionsRepository(conn).get(sub_id)
        assert sub["status"] == "approved"
