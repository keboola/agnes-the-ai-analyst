"""Activity Center read API."""
from datetime import datetime, timezone, timedelta


def test_activity_timeline_requires_admin(seeded_app, analyst_user):
    """Non-admin user gets 403."""
    resp = seeded_app["client"].get("/api/admin/activity", headers=analyst_user)
    assert resp.status_code in (401, 403)


def test_activity_timeline_returns_recent_rows(seeded_app, admin_user):
    """Seeded audit_log rows appear in the response."""
    from src.db import get_system_db
    from src.repositories.audit import AuditRepository
    conn = get_system_db()
    AuditRepository(conn).log(user_id="u1", action="test.activity", result="success")
    conn.close()

    resp = seeded_app["client"].get("/api/admin/activity", headers=admin_user)
    assert resp.status_code == 200
    data = resp.json()
    assert "rows" in data
    assert "next_cursor" in data
    assert any(r["action"] == "test.activity" for r in data["rows"])


def test_activity_timeline_supports_filters(seeded_app, admin_user):
    from src.db import get_system_db
    from src.repositories.audit import AuditRepository
    conn = get_system_db()
    repo = AuditRepository(conn)
    repo.log(action="sync.trigger")
    repo.log(action="auth.login")
    conn.close()

    resp = seeded_app["client"].get("/api/admin/activity?action_prefix=sync.", headers=admin_user)
    assert resp.status_code == 200
    actions = {r["action"] for r in resp.json()["rows"]}
    assert "sync.trigger" in actions
    assert "auth.login" not in actions


def test_activity_health_returns_pulse(seeded_app, admin_user):
    resp = seeded_app["client"].get("/api/admin/activity/health", headers=admin_user)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("green", "yellow", "red")
    assert "fields" in data
    assert "sentence" in data
    field_keys = {f["key"] for f in data["fields"]}
    assert "scheduler" in field_keys
    assert "sync_24h" in field_keys
    assert "active_users_today" in field_keys


def test_activity_sync_returns_recent(seeded_app, admin_user):
    import uuid
    from src.db import get_system_db
    now = datetime.now(timezone.utc)
    conn = get_system_db()
    conn.execute(
        "INSERT INTO sync_history (id, table_id, synced_at, rows, duration_ms, status, error) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [str(uuid.uuid4()), "t_test", now, 42, 1500, "ok", None]
    )
    conn.close()
    resp = seeded_app["client"].get("/api/admin/activity/sync", headers=admin_user)
    assert resp.status_code == 200
    data = resp.json()
    assert "rows" in data
    assert any(r["table_id"] == "t_test" for r in data["rows"])
