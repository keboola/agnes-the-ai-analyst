"""Health-check coverage for the session pipeline (#176).

GET /api/health/detailed must surface a `session_pipeline` service entry
that warns when freshly-uploaded session jsonls aren't being processed.

Heuristic:
  max(mtime of /data/user_sessions/**/*.jsonl) <=
  max(processed_at in session_extraction_state) + grace

Where grace = 2 * scheduler verification-detector cadence (default 15m).

When the assert fails, return status='warning' with an actionable
message — never 'error' (the LLM service may be down for maintenance,
not a hard failure).
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_extraction_state(processed_at: datetime, session_file: str = "/data/user_sessions/x/y.jsonl"):
    """Insert a synthetic row into session_extraction_state."""
    from src.db import get_system_db

    conn = get_system_db()
    conn.execute(
        "INSERT OR REPLACE INTO session_extraction_state "
        "(session_file, username, processed_at, items_extracted, file_hash) "
        "VALUES (?, ?, ?, ?, ?)",
        [session_file, "x", processed_at, 0, "deadbeef"],
    )
    conn.close()


def _make_session_file(env_data_dir: Path, name: str, mtime_ago_seconds: int) -> Path:
    """Create a fake session jsonl with the requested mtime offset."""
    sessions_dir = env_data_dir / "user_sessions" / "x"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    f = sessions_dir / name
    f.write_text("{}\n")
    target = time.time() - mtime_ago_seconds
    os.utime(f, (target, target))
    return f


class TestSessionPipelineHealthCheck:
    def test_no_session_files_returns_ok(self, seeded_app):
        """Empty /data/user_sessions/ is the cold-start case — not a warning."""
        c = seeded_app["client"]
        resp = c.get("/api/health/detailed", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        services = resp.json()["services"]
        assert "session_pipeline" in services
        assert services["session_pipeline"]["status"] == "ok"

    def test_session_files_recently_processed_returns_ok(self, seeded_app):
        env = seeded_app["env"]
        # Session file mtime: 1 minute ago. Processed: 30 seconds ago.
        # Within grace window → ok.
        _make_session_file(env["data_dir"], "ok.jsonl", mtime_ago_seconds=60)
        _seed_extraction_state(datetime.now(timezone.utc))

        c = seeded_app["client"]
        resp = c.get("/api/health/detailed", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        services = resp.json()["services"]
        assert services["session_pipeline"]["status"] == "ok"

    def test_old_session_files_unprocessed_returns_warning(self, seeded_app, monkeypatch):
        env = seeded_app["env"]
        # Session file mtime: 2 hours ago. Processed: 3 hours ago.
        # Way outside the 30-min grace window (2x default 15m cadence) → warning.
        _make_session_file(env["data_dir"], "old.jsonl", mtime_ago_seconds=7200)
        from datetime import timedelta
        _seed_extraction_state(datetime.now(timezone.utc) - timedelta(hours=3))

        c = seeded_app["client"]
        resp = c.get("/api/health/detailed", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        body = resp.json()
        services = body["services"]
        assert services["session_pipeline"]["status"] == "warning"
        # Actionable detail must point at the verification-detector job.
        detail = services["session_pipeline"].get("detail", "")
        assert "verification-detector" in detail or "session" in detail.lower()
        # Warning bubbles up to overall status='degraded' (existing pattern).
        assert body["status"] == "degraded"

    def test_session_files_never_processed_returns_warning(self, seeded_app):
        """Files exist but session_extraction_state is empty → warning."""
        env = seeded_app["env"]
        _make_session_file(env["data_dir"], "neverprocessed.jsonl", mtime_ago_seconds=7200)

        c = seeded_app["client"]
        resp = c.get("/api/health/detailed", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        services = resp.json()["services"]
        assert services["session_pipeline"]["status"] == "warning"


class TestSessionPipelineFIFOCheck:
    """FIFO check (#0.47.4): MAX-only comparison passes silently when
    the verification-detector skips a particular file but keeps processing
    newer ones. New code finds the oldest unprocessed file and surfaces it
    as `info` once it's older than SESSION_PIPELINE_STUCK_FILE_GRACE_SECONDS
    (default 4× verification-detector grace = 2h).
    """

    def test_fifo_check_warns_on_stuck_old_file_when_newer_was_processed(self, seeded_app):
        """Old jsonl never got processed even though a newer one was — info."""
        env = seeded_app["env"]
        # Old file (5h ago), NOT in extraction_state.
        _make_session_file(env["data_dir"], "stuck_old.jsonl", mtime_ago_seconds=5 * 3600)
        # Newer file (1min ago), processed_at=now → MAX-comparison says "ok".
        _make_session_file(env["data_dir"], "fresh.jsonl", mtime_ago_seconds=60)
        _seed_extraction_state(
            datetime.now(timezone.utc),
            session_file="x/fresh.jsonl",
        )

        c = seeded_app["client"]
        resp = c.get("/api/health/detailed", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        body = resp.json()
        services = body["services"]
        sp = services["session_pipeline"]
        assert sp["status"] == "info", f"expected info, got {sp}"
        assert "skipped" in sp["detail"]
        assert sp["stuck_file_age_seconds"] > 4 * 3600
        assert sp["stuck_file"].endswith("stuck_old.jsonl")
        # Info MUST NOT promote the headline to degraded — that's the whole
        # point of starting at info severity.
        assert body["status"] != "degraded"

    def test_fifo_check_silent_when_old_file_under_threshold(self, seeded_app):
        """Old file is 1h old (< 2h default threshold) — should return ok."""
        env = seeded_app["env"]
        _make_session_file(env["data_dir"], "recent_unprocessed.jsonl", mtime_ago_seconds=3600)
        _make_session_file(env["data_dir"], "fresh.jsonl", mtime_ago_seconds=60)
        _seed_extraction_state(
            datetime.now(timezone.utc),
            session_file="x/fresh.jsonl",
        )

        c = seeded_app["client"]
        resp = c.get("/api/health/detailed", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        services = resp.json()["services"]
        assert services["session_pipeline"]["status"] == "ok"

    def test_fifo_check_silent_when_no_unprocessed_files(self, seeded_app):
        """All FS jsonls are in extraction_state — should return ok."""
        env = seeded_app["env"]
        _make_session_file(env["data_dir"], "processed.jsonl", mtime_ago_seconds=60)
        _seed_extraction_state(
            datetime.now(timezone.utc),
            session_file="x/processed.jsonl",
        )

        c = seeded_app["client"]
        resp = c.get("/api/health/detailed", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        services = resp.json()["services"]
        assert services["session_pipeline"]["status"] == "ok"

    def test_fifo_check_threshold_env_override(self, seeded_app, monkeypatch):
        """SESSION_PIPELINE_STUCK_FILE_GRACE_SECONDS=60 → 5min-old file triggers info."""
        monkeypatch.setenv("SESSION_PIPELINE_STUCK_FILE_GRACE_SECONDS", "60")
        env = seeded_app["env"]
        _make_session_file(env["data_dir"], "five_min_old.jsonl", mtime_ago_seconds=300)
        _make_session_file(env["data_dir"], "fresh.jsonl", mtime_ago_seconds=10)
        _seed_extraction_state(
            datetime.now(timezone.utc),
            session_file="x/fresh.jsonl",
        )

        c = seeded_app["client"]
        resp = c.get("/api/health/detailed", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        body = resp.json()
        services = body["services"]
        sp = services["session_pipeline"]
        assert sp["status"] == "info", f"expected info, got {sp}"
        assert sp["stuck_file"].endswith("five_min_old.jsonl")
        assert body["status"] != "degraded"
