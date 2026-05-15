"""Tests for ``POST /api/sync/pull-confirm`` (Phase 7, Task 7.6)."""

from __future__ import annotations

import pytest

from src.db import get_system_db


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _usage_events_for(event_type: str, user_id: str) -> list[dict]:
    """props live on the friction_tags JSON column (see Section 9 spec —
    rename tracked as a follow-up)."""
    conn = get_system_db()
    rows = conn.execute(
        """SELECT event_type, user_id, friction_tags FROM usage_events
           WHERE event_type = ? AND user_id = ?
           ORDER BY occurred_at""",
        [event_type, user_id],
    ).fetchall()
    conn.close()
    import json as _json

    return [
        {"event_type": r[0], "user_id": r[1],
         "props": _json.loads(r[2]) if r[2] else {}}
        for r in rows
    ]


class TestPullConfirm:
    def test_minimal_payload_records_event(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            "/api/sync/pull-confirm",
            json={"duration_ms": 1234, "errors": 0},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 200
        assert resp.json() == {"recorded": True}

        events = _usage_events_for("sync.pull_completed", "analyst1")
        assert len(events) >= 1
        last = events[-1]
        assert last["props"]["duration_ms"] == 1234

    def test_full_payload_records_per_type_counts(self, seeded_app):
        c = seeded_app["client"]
        body = {
            "duration_ms": 5000,
            "direct_tables": {"added": 1, "updated": 2, "removed": 0},
            "data_packages": {"added": 3, "updated": 0, "removed": 1},
            "memory_domains": {"added": 0, "updated": 1, "removed": 0},
            "errors": 0,
        }
        resp = c.post(
            "/api/sync/pull-confirm",
            json=body,
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        events = _usage_events_for("sync.pull_completed", "admin1")
        last = events[-1]
        assert last["props"]["direct_tables_added"] == 1
        assert last["props"]["direct_tables_updated"] == 2
        assert last["props"]["data_packages_added"] == 3
        assert last["props"]["data_packages_removed"] == 1
        assert last["props"]["memory_domains_updated"] == 1

    def test_unauthenticated_rejected(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post("/api/sync/pull-confirm", json={"errors": 0})
        # No bearer token → 401.
        assert resp.status_code in (401, 403)

    def test_telemetry_failure_does_not_break_response(self, seeded_app, monkeypatch):
        """Even if usage_events insert raises, the endpoint returns 200."""
        from src.repositories import usage as usage_module

        original = usage_module.UsageRepository.emit_server_event

        def _boom(self, *a, **kw):
            raise RuntimeError("telemetry off-by-one")

        monkeypatch.setattr(
            usage_module.UsageRepository, "emit_server_event", _boom
        )
        c = seeded_app["client"]
        resp = c.post(
            "/api/sync/pull-confirm",
            json={"errors": 0},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 200
        # Restore.
        monkeypatch.setattr(
            usage_module.UsageRepository, "emit_server_event", original
        )
