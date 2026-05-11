"""AuditRepository v40 — new kwargs (params_before, client_ip, client_kind,
correlation_id) round-trip; legacy callers compile-time-unbroken."""
import json

import duckdb
import pytest
from src.db import _ensure_schema as init_database
from src.repositories.audit import AuditRepository


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "test.duckdb"
    c = duckdb.connect(str(db_path))
    init_database(c)
    yield c
    c.close()


def test_log_accepts_new_kwargs(conn):
    repo = AuditRepository(conn)
    entry_id = repo.log(
        user_id="u1",
        action="registry.update",
        resource="table:web_sessions",
        params={"after": {"cron": "*/15 * * * *"}},
        params_before={"cron": "0 */1 * * *"},
        client_ip="10.0.0.42",
        client_kind="web",
        correlation_id="corr-123",
    )
    row = conn.execute("SELECT params_before, client_ip, client_kind, correlation_id FROM audit_log WHERE id=?", [entry_id]).fetchone()
    assert json.loads(row[0]) == {"cron": "0 */1 * * *"}  # JSON content round-trip
    assert row[1] == "10.0.0.42"
    assert row[2] == "web"
    assert row[3] == "corr-123"


def test_log_legacy_signature_still_works(conn):
    """The original kwargs-only call site (used by 30+ existing endpoints)
    must keep working unchanged."""
    repo = AuditRepository(conn)
    entry_id = repo.log(user_id="u1", action="auth.login")
    row = conn.execute("SELECT user_id, action, params_before FROM audit_log WHERE id=?", [entry_id]).fetchone()
    assert row == ("u1", "auth.login", None)


from datetime import datetime, timezone, timedelta


def _seed(conn, rows: list[dict]):
    """Insert audit_log rows with explicit timestamps."""
    repo = AuditRepository(conn)
    ids = []
    for r in rows:
        entry_id = repo.log(
            user_id=r.get("user_id"),
            action=r.get("action", "test.x"),
            resource=r.get("resource"),
            params=r.get("params"),
            result=r.get("result"),
        )
        if "ts" in r:
            conn.execute("UPDATE audit_log SET timestamp=? WHERE id=?", [r["ts"], entry_id])
        ids.append(entry_id)
    return ids


def test_query_filter_by_time_range(conn):
    repo = AuditRepository(conn)
    now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    _seed(conn, [
        {"action": "a.1", "ts": now - timedelta(hours=2)},
        {"action": "a.2", "ts": now - timedelta(minutes=30)},
        {"action": "a.3", "ts": now - timedelta(minutes=5)},
    ])
    rows, _ = repo.query(since=now - timedelta(hours=1), until=now)
    assert {r["action"] for r in rows} == {"a.2", "a.3"}


def test_query_filter_by_action_prefix(conn):
    repo = AuditRepository(conn)
    _seed(conn, [
        {"action": "sync.trigger"},
        {"action": "sync.complete"},
        {"action": "auth.login"},
    ])
    rows, _ = repo.query(action_prefix="sync.")
    assert {r["action"] for r in rows} == {"sync.trigger", "sync.complete"}


def test_query_filter_by_action_in(conn):
    repo = AuditRepository(conn)
    _seed(conn, [
        {"action": "a"}, {"action": "b"}, {"action": "c"},
    ])
    rows, _ = repo.query(action_in=["a", "c"])
    assert {r["action"] for r in rows} == {"a", "c"}


def test_query_filter_by_user(conn):
    repo = AuditRepository(conn)
    _seed(conn, [
        {"user_id": "u1", "action": "x"},
        {"user_id": "u2", "action": "x"},
    ])
    rows, _ = repo.query(user_id="u1")
    assert len(rows) == 1
    assert rows[0]["user_id"] == "u1"


def test_query_filter_by_resource(conn):
    repo = AuditRepository(conn)
    _seed(conn, [
        {"action": "x", "resource": "table:a"},
        {"action": "x", "resource": "table:b"},
    ])
    rows, _ = repo.query(resource="table:a")
    assert len(rows) == 1


def test_query_filter_by_result_pattern(conn):
    repo = AuditRepository(conn)
    _seed(conn, [
        {"action": "x", "result": "success"},
        {"action": "x", "result": "error.timeout"},
        {"action": "x", "result": "error.permission"},
    ])
    rows, _ = repo.query(result_pattern="error.%")
    assert {r["result"] for r in rows} == {"error.timeout", "error.permission"}


def test_query_full_text_q(conn):
    repo = AuditRepository(conn)
    _seed(conn, [
        {"action": "x", "params": {"sql": "SELECT * FROM finance"}},
        {"action": "x", "params": {"sql": "SELECT * FROM marketing"}},
    ])
    rows, _ = repo.query(q="finance")
    assert len(rows) == 1


def test_query_cursor_pagination(conn):
    repo = AuditRepository(conn)
    now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    for i in range(5):
        _seed(conn, [{"action": f"a.{i}", "ts": now - timedelta(minutes=i)}])
    page1, cursor1 = repo.query(limit=2)
    assert len(page1) == 2
    assert cursor1 is not None
    page2, cursor2 = repo.query(limit=2, cursor=cursor1)
    assert len(page2) == 2
    page3, cursor3 = repo.query(limit=2, cursor=cursor2)
    assert len(page3) == 1
    assert cursor3 is None
    all_ids = {r["id"] for r in page1 + page2 + page3}
    assert len(all_ids) == 5


def test_query_ordering_newest_first(conn):
    repo = AuditRepository(conn)
    now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    _seed(conn, [
        {"action": "old", "ts": now - timedelta(hours=2)},
        {"action": "new", "ts": now - timedelta(minutes=1)},
    ])
    rows, _ = repo.query()
    assert rows[0]["action"] == "new"
    assert rows[1]["action"] == "old"
