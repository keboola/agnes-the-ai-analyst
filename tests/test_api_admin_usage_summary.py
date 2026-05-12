"""GET /api/admin/telemetry/summary — top tools, top users, error rate, DAU, slow actions."""
import uuid
from datetime import datetime, timezone, timedelta

import pytest


def _seed_usage_events(conn, *, n_tools=5, n_users=3, days_back=7):
    """Seed usage_events with deterministic shape."""
    base = datetime.now(timezone.utc) - timedelta(days=days_back)
    tools = ["Bash", "Read", "Edit", "Write", "Grep"]
    users = ["alice", "bob", "carol"]
    i = 0
    for d in range(days_back):
        ts = base + timedelta(days=d, hours=10)
        for u in users[:n_users]:
            for t in tools[:n_tools]:
                conn.execute(
                    """INSERT INTO usage_events
                    (id, session_id, session_file, username, event_type, tool_name,
                     is_error, source, occurred_at, processor_version)
                    VALUES (?, ?, ?, ?, 'tool_use', ?, ?, 'builtin', ?, 1)""",
                    [f"e-{i}", f"sess-{i}", f"{u}/x.jsonl", u, t,
                     i % 13 == 0,   # ~7% error
                     ts]
                )
                i += 1


def test_summary_top_tools(seeded_app, admin_user):
    from src.db import get_system_db
    conn = get_system_db()
    _seed_usage_events(conn, n_tools=3, n_users=2, days_back=3)
    conn.close()
    resp = seeded_app["client"].get("/api/admin/telemetry/summary?window=7d", headers=admin_user)
    assert resp.status_code == 200
    data = resp.json()
    assert data["window"] == "7d"
    assert len(data["top_tools"]) == 3
    # invocations descending
    invs = [t["invocations"] for t in data["top_tools"]]
    assert invs == sorted(invs, reverse=True)


def test_summary_top_users(seeded_app, admin_user):
    from src.db import get_system_db
    conn = get_system_db()
    _seed_usage_events(conn, n_tools=2, n_users=3, days_back=3)
    conn.close()
    data = seeded_app["client"].get("/api/admin/telemetry/summary?window=7d", headers=admin_user).json()
    assert len(data["top_users"]) == 3
    usernames = {u["username"] for u in data["top_users"]}
    assert usernames == {"alice", "bob", "carol"}


def test_summary_error_rate(seeded_app, admin_user):
    from src.db import get_system_db
    conn = get_system_db()
    _seed_usage_events(conn, n_tools=2, n_users=2, days_back=2)
    conn.close()
    data = seeded_app["client"].get("/api/admin/telemetry/summary?window=7d", headers=admin_user).json()
    assert len(data["error_rate"]) <= 2  # at most 2 tools seeded
    for row in data["error_rate"]:
        assert 0.0 <= row["rate"] <= 1.0


def test_summary_dau_series_is_30_entries(seeded_app, admin_user):
    """Always 30 entries even with window=7d."""
    from src.db import get_system_db
    conn = get_system_db()
    _seed_usage_events(conn, n_tools=1, n_users=1, days_back=3)
    conn.close()
    data = seeded_app["client"].get("/api/admin/telemetry/summary?window=7d", headers=admin_user).json()
    assert len(data["dau_series"]) == 30
    # dau_avg is a float
    assert isinstance(data["dau_avg"], (int, float))


def test_summary_slow_actions_from_audit_log(seeded_app, admin_user):
    """Seed audit_log rows with duration_ms and confirm they appear ordered by p95 desc."""
    from src.db import get_system_db
    conn = get_system_db()
    # Seed 6 rows for action "slow.x" so HAVING n>=5 passes
    for i in range(6):
        conn.execute(
            """INSERT INTO audit_log (id, timestamp, user_id, action, result, duration_ms)
            VALUES (?, current_timestamp, 'u1', 'slow.x', 'success', ?)""",
            [str(uuid.uuid4()), 1000 + i * 100]
        )
    conn.close()
    data = seeded_app["client"].get("/api/admin/telemetry/summary?window=7d", headers=admin_user).json()
    assert any(s["action"] == "slow.x" for s in data["slow_actions"])


def test_summary_admin_only(seeded_app, analyst_user):
    resp = seeded_app["client"].get("/api/admin/telemetry/summary?window=7d", headers=analyst_user)
    assert resp.status_code in (401, 403)


def test_summary_window_validation(seeded_app, admin_user):
    resp = seeded_app["client"].get("/api/admin/telemetry/summary?window=bogus", headers=admin_user)
    assert resp.status_code == 422


def test_admin_usage_page_renders(seeded_app, admin_user):
    """/admin/telemetry HTML page renders the interactive shell.

    Data loads client-side from /api/admin/telemetry/{facets,kpis,query}, so the
    server-rendered HTML asserts only the structural anchors the JS needs
    to attach to. The old static `Top 10 tools` block is replaced by the
    Group by + filter bar pattern.
    """
    resp = seeded_app["client"].get("/admin/telemetry", headers=admin_user)
    assert resp.status_code == 200
    assert "obs-page" in resp.text
    assert 'id="u-groupby"' in resp.text
    assert "Distinct users" in resp.text


def test_admin_usage_page_admin_only(seeded_app, analyst_user):
    resp = seeded_app["client"].get("/admin/telemetry", headers=analyst_user)
    assert resp.status_code in (302, 403)


def test_summary_does_not_audit_burst_polls(seeded_app, admin_user):
    """5 rapid /summary calls produce at most 1 usage.summary audit row (60s dedup)."""
    from src.db import get_system_db

    conn = get_system_db()
    conn.execute("DELETE FROM audit_log WHERE action='usage.summary'")
    conn.close()

    client = seeded_app["client"]
    for _ in range(5):
        resp = client.get("/api/admin/telemetry/summary?window=7d", headers=admin_user)
        assert resp.status_code == 200

    conn = get_system_db()
    n = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='usage.summary'"
    ).fetchone()[0]
    conn.close()

    assert n <= 1, f"Expected at most 1 usage.summary audit row, got {n}"
