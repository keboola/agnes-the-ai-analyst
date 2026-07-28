"""POST /api/sync/trigger must write to audit_log (closes coverage gap).

Uses canonical fixtures (Conventions section): seeded_app["client"] + admin_user
headers + get_system_db() for direct DB access.
"""
import pytest
from src.db import get_system_db


def test_sync_trigger_writes_audit_log(seeded_app, admin_user):
    c = seeded_app["client"]
    conn = get_system_db()
    before = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='sync.trigger'"
    ).fetchone()[0]
    conn.close()

    resp = c.post("/api/sync/trigger", headers=admin_user)
    assert resp.status_code in (200, 202)

    conn = get_system_db()
    after = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='sync.trigger'"
    ).fetchone()[0]
    row = conn.execute(
        "SELECT user_id, action, result FROM audit_log WHERE action='sync.trigger' ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert after == before + 1
    assert row[0] is not None        # user_id captured
    assert row[1] == "sync.trigger"
    assert row[2] in ("success", "error.in_progress", "error.locked")


def test_sync_trigger_does_not_5xx_if_audit_write_fails(seeded_app, admin_user, monkeypatch):
    """Resilience rule (Conventions): a failed audit write must NOT crash
    the wrapped business request — log + swallow + continue."""
    import duckdb
    from src.repositories.audit import AuditRepository

    def boom(*args, **kwargs):
        raise duckdb.IOException("simulated DB-locked")

    monkeypatch.setattr(AuditRepository, "log", boom)
    c = seeded_app["client"]
    resp = c.post("/api/sync/trigger", headers=admin_user)
    # The sync trigger itself must still respond — audit failure is invisible.
    assert resp.status_code in (200, 202, 409)
