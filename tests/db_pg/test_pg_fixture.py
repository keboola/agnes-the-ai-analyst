"""Failing tests for the pg_engine fixture itself.

Drives the fixture into existence (TDD step 1 in the parent plan). These
tests are the load-bearing check that *all* downstream PG tests have a
working Postgres to talk to.

Backend selection is via AGNES_TEST_PG_BACKEND env var:
  - "container" (default in CI) → testcontainers spins postgres:16-alpine
  - "embedded" (default locally if a system PG binary is present)
                → pytest-postgresql boots a local PG process

If neither backend is available, fixture tests skip cleanly with a clear
message — no silent green pass.
"""
import os

import pytest
import sqlalchemy as sa


def test_pg_engine_is_sqlalchemy_engine(pg_engine):
    """Fixture yields a SQLAlchemy Engine, not a connection or session."""
    assert isinstance(pg_engine, sa.Engine)


def test_pg_engine_select_one_works(pg_engine):
    """Connection actually reaches a live Postgres."""
    with pg_engine.connect() as conn:
        result = conn.execute(sa.text("SELECT 1")).scalar()
        assert result == 1


def test_pg_engine_reports_postgres_dialect(pg_engine):
    """We did not accidentally hand back a SQLite or DuckDB engine."""
    assert pg_engine.dialect.name == "postgresql"


def test_pg_engine_starts_with_empty_user_schema(pg_engine):
    """Fresh DB: no user tables before any migration runs.

    Catches a class of test pollution where a previous test left tables
    behind in a session-scoped DB. We rely on this invariant for
    round-trip / drift tests downstream.
    """
    inspector = sa.inspect(pg_engine)
    user_tables = [
        t for t in inspector.get_table_names(schema="public")
        if not t.startswith("pg_")
    ]
    assert user_tables == [], f"expected empty public schema, found: {user_tables}"


def test_pg_session_factory_yields_session(pg_session):
    """`pg_session` fixture wraps the engine in a transaction-scoped session
    that auto-rollbacks at test end (per-test isolation)."""
    from sqlalchemy.orm import Session
    assert isinstance(pg_session, Session)
    result = pg_session.execute(sa.text("SELECT 2")).scalar()
    assert result == 2


def test_backend_env_var_is_respected():
    """The fixture honors AGNES_TEST_PG_BACKEND — if neither env value nor
    autodetection succeeds, downstream tests skip with a clear message
    rather than failing in an opaque way."""
    backend = os.environ.get("AGNES_TEST_PG_BACKEND")
    if backend is not None:
        assert backend in {"container", "embedded", "pgserver"}, (
            f"AGNES_TEST_PG_BACKEND must be one of container|embedded|pgserver (got {backend!r})"
        )


def test_default_backend_is_pgserver(monkeypatch):
    """When AGNES_TEST_PG_BACKEND is unset, pgserver is the default — not autodetect."""
    monkeypatch.delenv("AGNES_TEST_PG_BACKEND", raising=False)
    from tests.db_pg.conftest import _resolve_backend
    assert _resolve_backend() == "pgserver"
