import duckdb
import pytest

from app.debug.duckdb_panel import (
    InstrumentedConnection,
    _request_store,
    get_request_store,
    record_query,
)


@pytest.fixture
def store_token():
    """Provide a fresh request-scoped store."""
    token = _request_store.set([])
    yield
    _request_store.reset(token)


@pytest.fixture
def conn():
    return duckdb.connect(":memory:")


def test_records_query(store_token, conn):
    inst = InstrumentedConnection(conn, "system")
    inst.execute("SELECT 1")
    store = get_request_store()
    assert len(store) == 1
    q = store[0]
    assert q.db == "system"
    assert q.sql == "SELECT 1"
    assert q.error is None
    assert q.ms >= 0


def test_records_query_with_params(store_token, conn):
    inst = InstrumentedConnection(conn, "analytics")
    inst.execute("SELECT $1::INT", [42])
    store = get_request_store()
    assert len(store) == 1
    assert store[0].params == [42]


def test_records_error(store_token, conn):
    inst = InstrumentedConnection(conn, "system")
    with pytest.raises(duckdb.Error):
        inst.execute("SELECT * FROM bogus_table_xyz")
    store = get_request_store()
    assert len(store) == 1
    assert store[0].error is not None
    assert "bogus_table_xyz" in store[0].error or "does not exist" in store[0].error.lower()


def test_db_tag_preserved(store_token):
    a = duckdb.connect(":memory:")
    b = duckdb.connect(":memory:")
    InstrumentedConnection(a, "system").execute("SELECT 1")
    InstrumentedConnection(b, "analytics").execute("SELECT 2")
    store = get_request_store()
    assert {q.db for q in store} == {"system", "analytics"}


def test_no_op_outside_request(conn):
    """When _request_store is None (outside a debug request), do not raise."""
    assert get_request_store() is None
    inst = InstrumentedConnection(conn, "system")
    inst.execute("SELECT 1")  # must not raise
    assert get_request_store() is None


def test_passthrough_attributes(conn):
    """Wrapper must delegate non-execute methods to the real connection."""
    inst = InstrumentedConnection(conn, "system")
    inst.execute("CREATE TABLE t (x INT)")
    inst.execute("INSERT INTO t VALUES (1), (2), (3)")
    rows = inst.execute("SELECT x FROM t ORDER BY x").fetchall()
    assert rows == [(1,), (2,), (3,)]


def test_cursor_returns_instrumented(store_token, conn):
    """A cursor() call returns an InstrumentedConnection wrapping the real cursor."""
    inst = InstrumentedConnection(conn, "system")
    cur = inst.cursor()
    assert isinstance(cur, InstrumentedConnection)
    cur.execute("SELECT 99")
    store = get_request_store()
    assert len(store) == 1
    assert store[0].db == "system"


def test_record_query_no_op_when_store_none():
    """record_query is safe to call when no store is set."""
    record_query("system", "SELECT 1", None, 0.0, None)  # must not raise
