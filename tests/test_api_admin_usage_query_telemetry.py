"""GET /api/admin/telemetry/summary — query-telemetry facets (#410).

The on-demand aggregation slice: the /summary endpoint also aggregates the
``query.remote`` / ``query.local`` / ``snapshot.create`` audit_log rows that
``app/api/query.py`` and ``app/api/v2_scan.py`` already emit, surfacing:

- query_telemetry.top_tables   — table_id, queries, scan_bytes (ranked by queries)
- query_telemetry.frequency    — per-day per-table remote/local split
- query_telemetry.total_scan_bytes
- query_telemetry.remote_queries / local_queries / snapshot_creates

These tests seed audit_log directly (no HTTP round-trip needed to produce the
rows) and assert the aggregation shape.
"""
import json
import uuid
from datetime import datetime, timezone, timedelta


def _ins(conn, *, action, resource, params, ts=None, result="success"):
    conn.execute(
        """INSERT INTO audit_log (id, timestamp, user_id, action, resource, params, result)
           VALUES (?, ?, 'u1', ?, ?, ?, ?)""",
        [
            str(uuid.uuid4()),
            ts or datetime.now(timezone.utc),
            action,
            resource,
            json.dumps(params) if params is not None else None,
            result,
        ],
    )


def _seed_query_audit(conn):
    """Seed a deterministic mix of query.remote / query.local / snapshot.create.

    orders  : 3 remote (bytes 100,200,300), 1 local       -> 4 queries, scan 600
    sessions: 1 remote (bytes 50), 1 local                -> 2 queries, scan 50
    adhoc   : 1 local (no table)                           -> excluded from top_tables
    plus 1 snapshot.create on orders (bytes 1000)
    """
    now = datetime.now(timezone.utc)
    # orders — remote x3
    _ins(conn, action="query.remote", resource="table:kbc.orders",
         params={"bytes_scanned": 100}, ts=now - timedelta(hours=1))
    _ins(conn, action="query.remote", resource="table:kbc.orders",
         params={"bytes_scanned": 200}, ts=now - timedelta(hours=2))
    _ins(conn, action="query.remote", resource="table:kbc.orders",
         params={"bytes_scanned": 300}, ts=now - timedelta(hours=3))
    # orders — local x1
    _ins(conn, action="query.local", resource="table:kbc.orders",
         params={"bytes_scanned": None}, ts=now - timedelta(hours=4))
    # sessions — remote x1, local x1
    _ins(conn, action="query.remote", resource="table:kbc.sessions",
         params={"bytes_scanned": 50}, ts=now - timedelta(hours=5))
    _ins(conn, action="query.local", resource="table:kbc.sessions",
         params={}, ts=now - timedelta(hours=6))
    # adhoc local (no table resource) — counted in totals, not in top_tables
    _ins(conn, action="query.local", resource="adhoc",
         params={}, ts=now - timedelta(hours=7))
    # snapshot.create on orders (resource may carry an :as: suffix)
    _ins(conn, action="snapshot.create", resource="table:kbc.orders:as:o_recent",
         params={"bytes_scanned": 1000, "rows_written": 10}, ts=now - timedelta(hours=1))


def test_top_tables_ranked_by_query_count(seeded_app, admin_user):
    from src.db import get_system_db
    conn = get_system_db()
    _seed_query_audit(conn)
    conn.close()

    data = seeded_app["client"].get(
        "/api/admin/telemetry/summary?window=7d", headers=admin_user
    ).json()
    qt = data["query_telemetry"]
    tables = qt["top_tables"]
    # orders (4 queries + 1 snapshot = 5) ranks above sessions (2)
    assert tables[0]["table_id"] == "kbc.orders"
    assert tables[0]["queries"] == 5
    assert tables[1]["table_id"] == "kbc.sessions"
    assert tables[1]["queries"] == 2
    # descending by queries
    counts = [t["queries"] for t in tables]
    assert counts == sorted(counts, reverse=True)
    # adhoc (no table) is excluded from the per-table ranking
    assert all(t["table_id"] != "adhoc" for t in tables)


def test_scan_bytes_summed_per_table_and_total(seeded_app, admin_user):
    from src.db import get_system_db
    conn = get_system_db()
    _seed_query_audit(conn)
    conn.close()

    qt = seeded_app["client"].get(
        "/api/admin/telemetry/summary?window=7d", headers=admin_user
    ).json()["query_telemetry"]
    by_id = {t["table_id"]: t for t in qt["top_tables"]}
    # orders: 100+200+300 (remote queries) + 1000 (snapshot) = 1600
    assert by_id["kbc.orders"]["scan_bytes"] == 1600
    # sessions: 50
    assert by_id["kbc.sessions"]["scan_bytes"] == 50
    # grand total across everything = 1650
    assert qt["total_scan_bytes"] == 1650


def test_remote_local_split(seeded_app, admin_user):
    from src.db import get_system_db
    conn = get_system_db()
    _seed_query_audit(conn)
    conn.close()

    qt = seeded_app["client"].get(
        "/api/admin/telemetry/summary?window=7d", headers=admin_user
    ).json()["query_telemetry"]
    # 4 remote (orders x3 + sessions x1), 3 local (orders, sessions, adhoc)
    assert qt["remote_queries"] == 4
    assert qt["local_queries"] == 3
    assert qt["snapshot_creates"] == 1
    # per-table split present on orders: 3 remote, 1 local
    orders = next(t for t in qt["top_tables"] if t["table_id"] == "kbc.orders")
    assert orders["remote"] == 3
    assert orders["local"] == 1


def test_frequency_per_day_per_table(seeded_app, admin_user):
    from src.db import get_system_db
    conn = get_system_db()
    _seed_query_audit(conn)
    conn.close()

    qt = seeded_app["client"].get(
        "/api/admin/telemetry/summary?window=7d", headers=admin_user
    ).json()["query_telemetry"]
    freq = qt["frequency"]
    assert isinstance(freq, list) and freq
    sample = freq[0]
    assert set(sample) >= {"day", "table_id", "remote", "local"}
    # all rows seeded "today" → orders day-bucket has 3 remote + 1 local
    orders_today = [r for r in freq if r["table_id"] == "kbc.orders"]
    assert sum(r["remote"] for r in orders_today) == 3
    assert sum(r["local"] for r in orders_today) == 1


def test_empty_window_returns_empty_facets(seeded_app, admin_user):
    """No query.* rows in window => empty query_telemetry facets, zeroed totals."""
    from src.db import get_system_db
    conn = get_system_db()
    # Seed a row OUTSIDE the 7d window so the table isn't empty globally.
    _ins(conn, action="query.remote", resource="table:kbc.old",
         params={"bytes_scanned": 999},
         ts=datetime.now(timezone.utc) - timedelta(days=40))
    conn.close()

    qt = seeded_app["client"].get(
        "/api/admin/telemetry/summary?window=7d", headers=admin_user
    ).json()["query_telemetry"]
    assert qt["top_tables"] == []
    assert qt["frequency"] == []
    assert qt["total_scan_bytes"] == 0
    assert qt["remote_queries"] == 0
    assert qt["local_queries"] == 0
    assert qt["snapshot_creates"] == 0


def test_all_window_includes_old_rows(seeded_app, admin_user):
    from src.db import get_system_db
    conn = get_system_db()
    _ins(conn, action="query.remote", resource="table:kbc.old",
         params={"bytes_scanned": 999},
         ts=datetime.now(timezone.utc) - timedelta(days=40))
    conn.close()

    qt = seeded_app["client"].get(
        "/api/admin/telemetry/summary?window=all", headers=admin_user
    ).json()["query_telemetry"]
    assert qt["total_scan_bytes"] == 999
    assert any(t["table_id"] == "kbc.old" for t in qt["top_tables"])


def test_query_telemetry_admin_only(seeded_app, analyst_user):
    resp = seeded_app["client"].get(
        "/api/admin/telemetry/summary?window=7d", headers=analyst_user
    )
    assert resp.status_code in (401, 403)
