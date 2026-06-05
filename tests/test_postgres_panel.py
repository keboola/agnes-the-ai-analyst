import sqlalchemy as sa
import pytest

from app.debug.postgres_panel import (
    _request_store,
    get_request_store,
    instrument_engine,
    record_query,
)


@pytest.fixture
def store_token():
    """Provide a fresh request-scoped store."""
    token = _request_store.set([])
    yield
    _request_store.reset(token)


@pytest.fixture
def engine():
    """In-memory SQLite engine, instrumented like the real PG engine.

    The capture is driven by SQLAlchemy ``before/after_cursor_execute`` +
    ``handle_error`` events, which are engine-agnostic — SQLite exercises the
    exact code path used against Cloud SQL Postgres without a live PG.
    """
    eng = sa.create_engine("sqlite://")
    instrument_engine(eng)
    return eng


def test_records_query(store_token, engine):
    with engine.connect() as conn:
        conn.execute(sa.text("SELECT 1"))
    store = get_request_store()
    assert len(store) == 1
    q = store[0]
    assert q.db == "postgres"
    assert "SELECT 1" in q.sql
    assert q.error is None
    assert q.ms >= 0


def test_records_query_with_params(store_token, engine):
    with engine.connect() as conn:
        conn.execute(sa.text("SELECT :x"), {"x": 42})
    store = get_request_store()
    assert len(store) == 1
    assert "42" in str(store[0].params)


def test_records_error(store_token, engine):
    with pytest.raises(sa.exc.SQLAlchemyError):
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT * FROM bogus_table_xyz"))
    store = get_request_store()
    assert len(store) >= 1
    errored = [q for q in store if q.error is not None]
    assert errored, "expected the failed query to be recorded with an error"
    assert "bogus_table_xyz" in errored[-1].error or "no such table" in errored[-1].error.lower()


def test_no_op_outside_request(engine):
    """When _request_store is None (outside a debug request), do not record or raise."""
    assert get_request_store() is None
    with engine.connect() as conn:
        conn.execute(sa.text("SELECT 1"))  # must not raise
    assert get_request_store() is None


def test_instrument_engine_idempotent(store_token):
    """Re-instrumenting must not double-register listeners (no duplicate rows)."""
    eng = sa.create_engine("sqlite://")
    instrument_engine(eng)
    instrument_engine(eng)
    with eng.connect() as conn:
        conn.execute(sa.text("SELECT 1"))
    assert len(get_request_store()) == 1


def test_record_query_no_op_when_store_none():
    """record_query is safe to call when no store is set."""
    record_query("postgres", "SELECT 1", None, 0.0, None)  # must not raise
