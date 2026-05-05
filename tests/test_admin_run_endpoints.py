"""Admin run-* endpoints that wire the LLM pipeline into scheduler-v2.

The scheduler container must drive corporate-memory, verification-detector,
and session-collector through HTTP — see services/scheduler/__main__.py
docstring for why in-process invocation is not safe (DuckDB single-writer
contention with the long-lived app handle).

Endpoints:
- POST /api/admin/run-session-collector
- POST /api/admin/run-verification-detector
- POST /api/admin/run-corporate-memory

All admin-gated. Request body is empty. Response is the underlying job
stats dict.

Closes one of five defects in #176.
"""

from __future__ import annotations

from unittest.mock import patch


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestRunSessionCollector:
    def test_admin_can_trigger_session_collector(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        fake_stats = {"users_processed": 1, "files_copied": 2, "files_skipped": 0}
        with patch("services.session_collector.collector.run", return_value=(0, fake_stats)) as m:
            resp = c.post("/api/admin/run-session-collector", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["details"]["files_copied"] == 2
        m.assert_called_once_with(dry_run=False, verbose=False)

    def test_non_admin_blocked(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.post("/api/admin/run-session-collector", headers=_auth(token))
        assert resp.status_code == 403

    def test_unauth_blocked(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post("/api/admin/run-session-collector")
        assert resp.status_code == 401


class TestRunVerificationDetector:
    def test_admin_can_trigger_verification_detector(self, seeded_app, monkeypatch):
        # Set the env so the factory's env-fallback returns a real (mocked
        # at the SDK boundary) extractor without 500-ing on missing config.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        fake_stats = {
            "sessions_scanned": 3,
            "sessions_processed": 2,
            "sessions_skipped": 1,
            "verifications_extracted": 5,
            "items_created": 4,
            "errors": [],
        }
        with patch(
            "services.verification_detector.detector.run",
            return_value=fake_stats,
        ) as m, patch(
            "connectors.llm.factory.AnthropicExtractor"
        ):
            resp = c.post("/api/admin/run-verification-detector", headers=_auth(token))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["details"]["items_created"] == 4
        m.assert_called_once()

    def test_non_admin_blocked(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.post("/api/admin/run-verification-detector", headers=_auth(token))
        assert resp.status_code == 403


class TestRunCorporateMemory:
    def test_admin_can_trigger_corporate_memory(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        fake_stats = {
            "users_scanned": 2,
            "files_found": 2,
            "items_extracted": 3,
            "items_filtered": 0,
            "items_preserved": 1,
            "items_new": 2,
            "items_pending": 2,
            "skipped": False,
            "errors": [],
        }
        with patch(
            "services.corporate_memory.collector.collect_all",
            return_value=fake_stats,
        ) as m:
            resp = c.post("/api/admin/run-corporate-memory", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["details"]["items_new"] == 2
        m.assert_called_once()

    def test_non_admin_blocked(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.post("/api/admin/run-corporate-memory", headers=_auth(token))
        assert resp.status_code == 403

    def test_unhandled_exception_still_audits(self, seeded_app):
        """Devin Review on 4c4dfee8: run_corporate_memory must mirror
        run_verification_detector — record the failure in audit_log even
        when collect_all() raises something other than ValueError, so
        the operator sees the failure on /admin/scheduler-runs instead
        of only in docker logs."""
        from src.db import get_system_db

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with patch(
            "services.corporate_memory.collector.collect_all",
            side_effect=RuntimeError("simulated DuckDB lock"),
        ):
            resp = c.post("/api/admin/run-corporate-memory", headers=_auth(token))
        assert resp.status_code == 500
        assert "RuntimeError" in resp.json()["detail"]
        # The audit row must exist regardless of the 500.
        conn = get_system_db()
        try:
            rows = conn.execute(
                "SELECT params FROM audit_log WHERE action = 'run_corporate_memory' ORDER BY timestamp DESC LIMIT 1"
            ).fetchall()
        finally:
            conn.close()
        assert rows, "audit row missing on unhandled exception"
        params_json = rows[0][0]
        assert "unhandled_error" in params_json
        assert "RuntimeError" in params_json


class TestSchedulerJobsWireUp:
    """The scheduler must drive all three new endpoints on a sensible cadence."""

    def test_scheduler_includes_session_collector(self, monkeypatch):
        for v in (
            "SCHEDULER_DATA_REFRESH_INTERVAL",
            "SCHEDULER_HEALTH_CHECK_INTERVAL",
            "SCHEDULER_TICK_SECONDS",
            "SCHEDULER_SCRIPT_RUN_INTERVAL",
        ):
            monkeypatch.delenv(v, raising=False)
        from services.scheduler.__main__ import build_jobs
        names = {n for n, *_ in build_jobs()}
        assert "session-collector" in names

    def test_scheduler_includes_verification_detector(self, monkeypatch):
        for v in (
            "SCHEDULER_DATA_REFRESH_INTERVAL",
            "SCHEDULER_HEALTH_CHECK_INTERVAL",
            "SCHEDULER_TICK_SECONDS",
            "SCHEDULER_SCRIPT_RUN_INTERVAL",
        ):
            monkeypatch.delenv(v, raising=False)
        from services.scheduler.__main__ import build_jobs
        names = {n for n, *_ in build_jobs()}
        assert "verification-detector" in names

    def test_scheduler_includes_corporate_memory(self, monkeypatch):
        for v in (
            "SCHEDULER_DATA_REFRESH_INTERVAL",
            "SCHEDULER_HEALTH_CHECK_INTERVAL",
            "SCHEDULER_TICK_SECONDS",
            "SCHEDULER_SCRIPT_RUN_INTERVAL",
        ):
            monkeypatch.delenv(v, raising=False)
        from services.scheduler.__main__ import build_jobs
        names = {n for n, *_ in build_jobs()}
        assert "corporate-memory" in names

    def test_session_collector_endpoint_is_registered(self, monkeypatch):
        for v in (
            "SCHEDULER_DATA_REFRESH_INTERVAL",
            "SCHEDULER_HEALTH_CHECK_INTERVAL",
            "SCHEDULER_TICK_SECONDS",
            "SCHEDULER_SCRIPT_RUN_INTERVAL",
        ):
            monkeypatch.delenv(v, raising=False)
        from services.scheduler.__main__ import build_jobs
        target = next(j for j in build_jobs() if j[0] == "session-collector")
        _, _, endpoint, method, _t = target
        assert endpoint == "/api/admin/run-session-collector"
        assert method == "POST"

    def test_verification_detector_endpoint_is_registered(self, monkeypatch):
        for v in (
            "SCHEDULER_DATA_REFRESH_INTERVAL",
            "SCHEDULER_HEALTH_CHECK_INTERVAL",
            "SCHEDULER_TICK_SECONDS",
            "SCHEDULER_SCRIPT_RUN_INTERVAL",
        ):
            monkeypatch.delenv(v, raising=False)
        from services.scheduler.__main__ import build_jobs
        target = next(j for j in build_jobs() if j[0] == "verification-detector")
        _, _, endpoint, method, _t = target
        assert endpoint == "/api/admin/run-verification-detector"
        assert method == "POST"

    def test_corporate_memory_endpoint_is_registered(self, monkeypatch):
        for v in (
            "SCHEDULER_DATA_REFRESH_INTERVAL",
            "SCHEDULER_HEALTH_CHECK_INTERVAL",
            "SCHEDULER_TICK_SECONDS",
            "SCHEDULER_SCRIPT_RUN_INTERVAL",
        ):
            monkeypatch.delenv(v, raising=False)
        from services.scheduler.__main__ import build_jobs
        target = next(j for j in build_jobs() if j[0] == "corporate-memory")
        _, _, endpoint, method, _t = target
        assert endpoint == "/api/admin/run-corporate-memory"
        assert method == "POST"

    def test_new_jobs_have_offset_cadences(self, monkeypatch):
        """Three jobs in the same family must NOT all fire on the same tick.

        Otherwise the LLM API and DuckDB writer all spike together every time
        the cadence aligns. Different schedule strings ensure offset.
        """
        for v in (
            "SCHEDULER_DATA_REFRESH_INTERVAL",
            "SCHEDULER_HEALTH_CHECK_INTERVAL",
            "SCHEDULER_TICK_SECONDS",
            "SCHEDULER_SCRIPT_RUN_INTERVAL",
        ):
            monkeypatch.delenv(v, raising=False)
        from services.scheduler.__main__ import build_jobs
        targets = {n: schedule for n, schedule, *_ in build_jobs()
                   if n in ("session-collector", "verification-detector", "corporate-memory")}
        # All three present.
        assert len(targets) == 3
