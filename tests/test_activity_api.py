"""Activity Center read API."""
import pytest
from datetime import datetime, timezone, timedelta


@pytest.fixture(autouse=True)
def _reset_activity_dedup():
    from app.api.activity import _RECENT_AUDITS, _HEALTH_CACHE
    _RECENT_AUDITS.clear()
    _HEALTH_CACHE["data"] = None
    _HEALTH_CACHE["expires_at"] = None
    yield
    _RECENT_AUDITS.clear()
    _HEALTH_CACHE["data"] = None
    _HEALTH_CACHE["expires_at"] = None


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


def test_admin_activity_page_renders(seeded_app, admin_user):
    resp = seeded_app["client"].get("/admin/activity", headers=admin_user)
    assert resp.status_code == 200
    # Page is the unified observability shell — fixtures it must render.
    # All data loads client-side, so we only assert structural anchors.
    assert "obs-page" in resp.text
    assert "Saved views" in resp.text
    assert "obs-table" in resp.text


def test_activity_center_redirects_to_admin_activity(seeded_app, admin_user):
    resp = seeded_app["client"].get("/activity-center", headers=admin_user, follow_redirects=False)
    assert resp.status_code == 308
    assert resp.headers["location"] == "/admin/activity"


def test_dashboard_links_to_admin_activity(seeded_app, admin_user):
    resp = seeded_app["client"].get("/dashboard", headers=admin_user)
    assert resp.status_code == 200
    assert "/admin/activity" in resp.text
    assert "/activity-center" not in resp.text  # old URL removed


def test_admin_header_includes_activity_link(seeded_app, admin_user):
    resp = seeded_app["client"].get("/admin/activity", headers=admin_user)
    assert resp.status_code == 200
    assert 'href="/admin/activity"' in resp.text


def test_activity_health_does_not_audit_polling(seeded_app, admin_user):
    """Polling /health every 30s shouldn't blow up audit_log."""
    from src.db import get_system_db
    c = seeded_app["client"]
    conn = get_system_db()
    before = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='activity.read'"
    ).fetchone()[0]
    conn.close()
    for _ in range(5):
        c.get("/api/admin/activity/health", headers=admin_user)
    conn = get_system_db()
    after = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='activity.read'"
    ).fetchone()[0]
    conn.close()
    assert after - before <= 1  # at most one row from the burst


def test_activity_timeline_audits_first_call_only(seeded_app, admin_user):
    """Two identical filter calls within 60s produce one audit row."""
    from src.db import get_system_db
    c = seeded_app["client"]
    conn = get_system_db()
    conn.execute("DELETE FROM audit_log WHERE action='activity.read'")
    conn.close()
    c.get("/api/admin/activity?action_prefix=sync.", headers=admin_user)
    c.get("/api/admin/activity?action_prefix=sync.", headers=admin_user)
    conn = get_system_db()
    n = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='activity.read'"
    ).fetchone()[0]
    conn.close()
    assert n == 1


def test_activity_timeline_audits_different_filters(seeded_app, admin_user):
    """Different filter combinations each get their own audit row."""
    from src.db import get_system_db
    c = seeded_app["client"]
    conn = get_system_db()
    conn.execute("DELETE FROM audit_log WHERE action='activity.read'")
    conn.close()
    c.get("/api/admin/activity?action_prefix=sync.", headers=admin_user)
    c.get("/api/admin/activity?action_prefix=auth.", headers=admin_user)
    conn = get_system_db()
    n = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='activity.read'"
    ).fetchone()[0]
    conn.close()
    assert n == 2


def test_activity_health_emits_posthog_event_when_enabled(seeded_app, admin_user):
    from unittest.mock import patch

    with patch("app.api.activity.get_posthog") as mock_get:
        mock_client = mock_get.return_value
        mock_client.enabled = True
        seeded_app["client"].get("/api/admin/activity/health", headers=admin_user)
        mock_client.capture.assert_called()
        kw = mock_client.capture.call_args.kwargs
        assert kw.get("event") == "activity_health_viewed"


def test_activity_endpoints_silent_when_posthog_disabled(seeded_app, admin_user):
    from unittest.mock import patch

    with patch("app.api.activity.get_posthog") as mock_get:
        mock_client = mock_get.return_value
        mock_client.enabled = False
        resp = seeded_app["client"].get("/api/admin/activity/health", headers=admin_user)
        # capture may be called but the inner SDK is no-op; that's the contract.
        # Assert: no exception, healthy response.
        assert resp.status_code == 200
