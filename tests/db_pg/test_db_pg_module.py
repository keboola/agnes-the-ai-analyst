"""Tests for src/db_pg.py — engine/session factory + DeclarativeBase.

Mirrors the shape of src/db.py::get_system_db (lines 937-959): a process-
wide singleton engine guarded by a lock, lazy-initialized, reads the URL
from AGNES_DB_URL / DATABASE_URL. Disposing the engine is supported for
test isolation.
"""
from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Session


def test_module_exports_base_and_factories():
    """Public surface contract."""
    import src.db_pg as db_pg

    assert hasattr(db_pg, "Base"), "src.db_pg must expose `Base`"
    assert issubclass(db_pg.Base, DeclarativeBase), "Base must subclass DeclarativeBase"
    assert hasattr(db_pg, "get_engine"), "src.db_pg must expose `get_engine`"
    assert hasattr(db_pg, "get_session"), "src.db_pg must expose `get_session`"
    assert hasattr(db_pg, "dispose"), "src.db_pg must expose `dispose`"


def test_get_engine_returns_singleton(_pg_url, monkeypatch):
    """get_engine() returns the same Engine across calls.

    Matches the DuckDB singleton pattern in src/db.py (one process owns
    one connection pool, all repos share it).
    """
    import src.db_pg as db_pg

    db_pg.dispose()
    monkeypatch.setenv("AGNES_DB_URL", _pg_url)
    e1 = db_pg.get_engine()
    e2 = db_pg.get_engine()
    assert e1 is e2


def test_get_engine_reads_url_from_env(_pg_url, monkeypatch):
    """No URL → RuntimeError. With AGNES_DB_URL set → connects."""
    import src.db_pg as db_pg

    db_pg.dispose()
    monkeypatch.delenv("AGNES_DB_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="AGNES_DB_URL"):
        db_pg.get_engine()

    monkeypatch.setenv("AGNES_DB_URL", _pg_url)
    db_pg.dispose()
    eng = db_pg.get_engine()
    with eng.connect() as conn:
        assert conn.execute(sa.text("SELECT 42")).scalar() == 42


def test_get_session_yields_session(_pg_url, monkeypatch):
    """get_session() is a context-manager that produces a Session."""
    import src.db_pg as db_pg

    db_pg.dispose()
    monkeypatch.setenv("AGNES_DB_URL", _pg_url)
    with db_pg.get_session() as session:
        assert isinstance(session, Session)
        assert session.execute(sa.text("SELECT 7")).scalar() == 7


def test_dispose_clears_singleton(_pg_url, monkeypatch):
    """Calling dispose() drops the engine; next get_engine() builds a fresh one."""
    import src.db_pg as db_pg

    monkeypatch.setenv("AGNES_DB_URL", _pg_url)
    db_pg.dispose()
    e1 = db_pg.get_engine()
    db_pg.dispose()
    e2 = db_pg.get_engine()
    assert e1 is not e2
