"""SyncStateRepository.list_recent() — cross-table chronological feed."""
import duckdb
import pytest
from datetime import datetime, timezone, timedelta
from src.db import _ensure_schema as init_database
from src.repositories.sync_state import SyncStateRepository


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "test.duckdb"
    c = duckdb.connect(str(db_path))
    init_database(c)
    yield c
    c.close()


def _record(conn, table_id: str, synced_at: datetime, status: str = "ok", rows: int = 100):
    import uuid
    conn.execute(
        "INSERT INTO sync_history (id, table_id, synced_at, rows, duration_ms, status, error) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [str(uuid.uuid4()), table_id, synced_at, rows, 1234, status, None]
    )


def test_list_recent_returns_all_tables_newest_first(conn):
    repo = SyncStateRepository(conn)
    now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    _record(conn, "orders", now - timedelta(hours=1))
    _record(conn, "customers", now - timedelta(minutes=30))
    _record(conn, "products", now - timedelta(minutes=5))

    rows = repo.list_recent(since=now - timedelta(hours=2), limit=50)
    assert [r["table_id"] for r in rows] == ["products", "customers", "orders"]


def test_list_recent_respects_since(conn):
    repo = SyncStateRepository(conn)
    now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    _record(conn, "old", now - timedelta(days=3))
    _record(conn, "new", now - timedelta(minutes=10))
    rows = repo.list_recent(since=now - timedelta(hours=1), limit=50)
    assert [r["table_id"] for r in rows] == ["new"]


def test_list_recent_respects_limit(conn):
    repo = SyncStateRepository(conn)
    now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    for i in range(20):
        _record(conn, f"t{i}", now - timedelta(minutes=i))
    rows = repo.list_recent(since=now - timedelta(hours=1), limit=5)
    assert len(rows) == 5


def test_list_recent_includes_failures(conn):
    repo = SyncStateRepository(conn)
    now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    _record(conn, "t1", now, status="ok")
    _record(conn, "t2", now, status="error")
    rows = repo.list_recent(since=now - timedelta(hours=1), limit=10)
    statuses = {r["table_id"]: r["status"] for r in rows}
    assert statuses["t1"] == "ok"
    assert statuses["t2"] == "error"
