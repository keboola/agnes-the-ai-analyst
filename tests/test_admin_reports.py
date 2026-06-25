"""GET /api/admin/reports/marketplace-digest — consolidated daily/weekly digest."""
import uuid
from datetime import datetime, date, timezone, timedelta

ANCHOR = date(2026, 6, 20)
PREV = ANCHOR - timedelta(days=1)


def _ts(d: date, hour: int = 10) -> datetime:
    return datetime(d.year, d.month, d.day, hour, tzinfo=timezone.utc)


def _seed(conn):
    # --- usage_events: anchor day busier than the day before ----------------
    def ev(d, user, tool, is_err):
        conn.execute(
            """INSERT INTO usage_events
               (id, session_id, session_file, username, event_type, tool_name,
                is_error, source, occurred_at, processor_version)
               VALUES (?, ?, ?, ?, 'tool_use', ?, ?, ?, ?, 1)""",
            [str(uuid.uuid4()), f"sess-{uuid.uuid4()}", f"{user}/x.jsonl", user,
             tool, is_err, "curated", _ts(d)],
        )

    # anchor: 3 users, 10 events, 1 error
    for user in ("alice", "bob", "carol"):
        for tool in ("Bash", "Read", "Edit"):
            ev(ANCHOR, user, tool, False)
    ev(ANCHOR, "alice", "Write", True)  # 10th event, the error
    # prev day: 2 users, 5 events, 0 errors
    for user in ("alice", "bob"):
        for tool in ("Bash", "Read"):
            ev(PREV, user, tool, False)
    ev(PREV, "alice", "Edit", False)

    # --- sessions -----------------------------------------------------------
    for d, sid in ((ANCHOR, "s1"), (ANCHOR, "s2"), (PREV, "s3")):
        conn.execute(
            """INSERT INTO usage_session_summary
               (session_file, session_id, username, started_at, processor_version)
               VALUES (?, ?, 'alice', ?, 1)""",
            [f"{sid}.jsonl", sid, _ts(d)],
        )

    # --- marketplace item daily rollups -------------------------------------
    def item(d, source, type_, name, count, du, err):
        conn.execute(
            """INSERT INTO usage_marketplace_item_daily
               (day, source, type, parent_plugin, name, count, distinct_users, error_count)
               VALUES (?, ?, ?, '', ?, ?, ?, ?)""",
            [d, source, type_, name, count, du, err],
        )

    item(ANCHOR, "curated", "skill", "product-analyzer", 8, 3, 1)
    item(ANCHOR, "flea", "agent", "data-bot", 4, 2, 0)
    item(PREV, "curated", "skill", "product-analyzer", 4, 2, 0)   # rising (8 > 4)
    item(PREV, "curated", "skill", "old-skill", 6, 2, 0)          # falling (0 < 6)

    # --- marketplace registry + plugins -------------------------------------
    def reg(mid, name, curator, last_synced, last_error):
        conn.execute(
            """INSERT INTO marketplace_registry
               (id, name, url, curator_name, last_synced_at, last_error)
               VALUES (?, ?, 'https://example.com/repo.git', ?, ?, ?)""",
            [mid, name, curator, last_synced, last_error],
        )

    now = datetime.now(timezone.utc)
    reg("curated-product", "Product", "Blanka", now - timedelta(hours=2), None)   # ok
    reg("curated-stale", "Marketing", "Juraj", now - timedelta(days=5), None)     # stale
    reg("curated-err", "Sales", "Carmen", now - timedelta(hours=1), "clone failed")  # error

    # Built-in marketplace: seeded locally, never git-synced (null last_synced),
    # is_builtin=TRUE. Must NOT be flagged stale, and its plugins must NOT show
    # up as zero-usage curated content.
    conn.execute(
        """INSERT INTO marketplace_registry
           (id, name, url, curator_name, last_synced_at, last_error, is_builtin)
           VALUES ('agnes-builtin', 'Built-in', '', NULL, NULL, NULL, TRUE)"""
    )

    def plug(mid, name):
        conn.execute(
            """INSERT INTO marketplace_plugins (marketplace_id, name, is_system)
               VALUES (?, ?, FALSE)""",
            [mid, name],
        )

    plug("curated-product", "product-analyzer")  # used → not in zero_usage
    plug("curated-product", "unused-skill")       # zero usage → listed
    plug("agnes-builtin", "welcome")              # built-in → excluded from zero_usage

    # --- installs (anchor day) ----------------------------------------------
    conn.execute(
        "INSERT INTO user_plugin_optouts (user_id, marketplace_id, plugin_name, opted_out_at) "
        "VALUES ('u1', 'curated-product', 'product-analyzer', ?)", [_ts(ANCHOR)])
    conn.execute(
        "INSERT INTO user_plugin_optouts (user_id, marketplace_id, plugin_name, opted_out_at) "
        "VALUES ('u2', 'curated-product', 'product-analyzer', ?)", [_ts(ANCHOR)])
    conn.execute(
        "INSERT INTO user_store_installs (user_id, entity_id, installed_at) "
        "VALUES ('u1', 'ent-1', ?)", [_ts(ANCHOR)])


def _get(client, headers, period):
    return client.get(
        f"/api/admin/reports/marketplace-digest?period={period}&date={ANCHOR.isoformat()}",
        headers=headers,
    )


def test_daily_digest_shape_and_kpis(seeded_app, admin_user):
    from src.db import get_system_db
    conn = get_system_db()
    _seed(conn)
    conn.close()

    resp = _get(seeded_app["client"], admin_user, "daily")
    assert resp.status_code == 200
    data = resp.json()

    # top-level contract
    for key in ("meta", "headline_kpis", "trend_series", "by_source", "top_items",
                "rising", "falling", "failures", "installs", "zero_usage",
                "marketplace_health"):
        assert key in data, f"missing {key}"

    assert data["meta"]["report_type"] == "daily"
    assert data["meta"]["period_start"] == ANCHOR.isoformat()
    assert len(data["trend_series"]) == 14

    k = data["headline_kpis"]
    assert k["active_users"] == {"value": 3, "prev": 2, "delta_pct": 50.0}
    assert k["invocations"] == {"value": 10, "prev": 5, "delta_pct": 100.0}
    assert k["errors"]["value"] == 1
    assert k["sessions"]["value"] == 2 and k["sessions"]["prev"] == 1
    assert k["new_installs"]["value"] == 3  # 2 curated + 1 flea


def test_daily_movers_failures_and_zero_usage(seeded_app, admin_user):
    from src.db import get_system_db
    conn = get_system_db()
    _seed(conn)
    conn.close()

    data = _get(seeded_app["client"], admin_user, "daily").json()

    # top_items ranked, product-analyzer leads with 8 invocations
    assert data["top_items"][0]["name"] == "product-analyzer"
    assert data["top_items"][0]["rank"] == 1
    assert data["top_items"][0]["invocations"] == 8
    # daily spans one day, so the per-day rollup distinct is exact
    assert data["top_items"][0]["distinct_users"] == 3

    # rising: product-analyzer 8 vs 4 → +100%
    assert any(i["name"] == "product-analyzer" and i["delta_pct"] == 100.0
               for i in data["rising"])
    # falling: old-skill dropped from 6 to 0
    assert any(i["name"] == "old-skill" for i in data["falling"])
    # failures: product-analyzer had 1 error
    assert any(f["name"] == "product-analyzer" and f["errors"] == 1
               for f in data["failures"])
    # zero_usage: unused-skill listed; used + built-in plugins excluded
    zero_names = {z["name"] for z in data["zero_usage"]}
    assert "unused-skill" in zero_names
    assert "product-analyzer" not in zero_names
    assert "welcome" not in zero_names  # built-in plugin must not be flagged

    # marketplace_health statuses derived correctly
    health = {h["id"]: h for h in data["marketplace_health"]}
    assert health["curated-product"]["sync_status"] == "ok"
    assert health["curated-stale"]["sync_status"] == "stale"
    assert health["curated-err"]["sync_status"] == "error"
    # built-in is never git-synced (null last_synced) but is healthy, not stale
    assert health["agnes-builtin"]["sync_status"] == "ok"
    assert health["curated-product"]["plugin_count"] == 2


def test_weekly_digest_window(seeded_app, admin_user):
    from src.db import get_system_db
    conn = get_system_db()
    _seed(conn)
    conn.close()

    data = _get(seeded_app["client"], admin_user, "weekly").json()
    assert data["meta"]["report_type"] == "weekly"
    assert len(data["trend_series"]) == 30
    # weekly primary spans anchor-6..anchor, so both seeded days are included
    assert data["headline_kpis"]["invocations"]["value"] == 15  # 10 + 5
    # P2: weekly never sums per-day distincts. With no live sliding-window
    # snapshot (explicit historical date), per-item distinct_users is null
    # rather than an inflated multi-day sum.
    assert data["top_items"], "expected weekly top_items"
    assert all(i["distinct_users"] is None for i in data["top_items"])


def test_digest_admin_only(seeded_app, analyst_user):
    resp = _get(seeded_app["client"], analyst_user, "daily")
    assert resp.status_code in (401, 403)


def test_digest_period_validation(seeded_app, admin_user):
    resp = seeded_app["client"].get(
        "/api/admin/reports/marketplace-digest?period=bogus", headers=admin_user)
    assert resp.status_code == 422


def test_digest_bad_date(seeded_app, admin_user):
    resp = seeded_app["client"].get(
        "/api/admin/reports/marketplace-digest?period=daily&date=not-a-date",
        headers=admin_user)
    assert resp.status_code == 422
