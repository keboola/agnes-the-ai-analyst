"""`_pg_revisions()` reads the stamped Alembic revision with a plain SELECT,
not `MigrationContext.configure()`.

The `/api/health` liveness probe refreshes a 30s schema cache that calls
`_pg_revisions()` continuously. Configuring a full `MigrationContext` on every
call logged two `alembic.runtime.migration` INFO lines ("Context impl
PostgresqlImpl" / "Will assume transactional DDL") — thousands of noise lines a
day drowning real app logs. A plain `SELECT version_num FROM alembic_version`
reads the same revision without the MigrationContext, and preserves the "never
stamped → None" contract.

Only a missing `alembic_version` table (`ProgrammingError`) is swallowed to
None; any other DB error (transient connectivity, permissions) propagates so a
broken DB reads as `unreachable` rather than a masked "never stamped" (which
would falsely surface as schema drift and get pinned in the 30s cache).

These are unit tests over a fake engine so they run without a live Postgres
(the PG-backed variant lives in tests/db_pg/); the log-noise regression against
a real MigrationContext is asserted in
tests/db_pg/test_startup_revision_check.py.
"""

from __future__ import annotations

import logging

import pytest
import sqlalchemy as sa


class _FakeResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar(self) -> object:
        return self._value


class _FakeConn:
    """Minimal SQLAlchemy-connection stand-in: answers the revision SELECT, or
    raises a supplied exception to mimic a missing table / broken connection. It
    deliberately lacks the `.dialect` a real `MigrationContext.configure()`
    needs, so the pre-fix code path cannot silently pass against it."""

    def __init__(self, value: object, exc: Exception | None) -> None:
        self._value = value
        self._exc = exc

    def execute(self, _stmt: object) -> _FakeResult:
        if self._exc is not None:
            raise self._exc
        return _FakeResult(self._value)

    def __enter__(self) -> "_FakeConn":
        return self

    def __exit__(self, *_a: object) -> bool:
        return False


class _FakeEngine:
    def __init__(self, value: object, exc: Exception | None = None) -> None:
        self._value = value
        self._exc = exc

    def connect(self) -> _FakeConn:
        return _FakeConn(self._value, self._exc)


def _programming_error() -> sa.exc.ProgrammingError:
    """A `ProgrammingError` shaped like psycopg's UndefinedTable (42P01)."""
    return sa.exc.ProgrammingError(
        "SELECT version_num FROM alembic_version",
        {},
        Exception('relation "alembic_version" does not exist'),
    )


def _operational_error() -> sa.exc.OperationalError:
    """A transient connectivity error — must NOT be masked as never-stamped."""
    return sa.exc.OperationalError(
        "SELECT version_num FROM alembic_version",
        {},
        Exception("server closed the connection unexpectedly"),
    )


def test_pg_revisions_returns_stamped_revision(monkeypatch):
    """The stamped revision comes straight from the `alembic_version` SELECT."""
    import src.db_pg as db_pg

    monkeypatch.setattr(db_pg, "get_engine", lambda: _FakeEngine("deadbeefcafe"))
    current, head, db_ahead = db_pg._pg_revisions()

    assert current == "deadbeefcafe"
    assert head, "head should resolve from the shipped migration scripts"
    # Unknown revision (not in this image's scripts) → DB is ahead.
    assert db_ahead is True


def test_pg_revisions_missing_table_reads_as_none(monkeypatch):
    """A never-stamped DB (no `alembic_version` table → ProgrammingError) reads
    as None, matching `get_current_revision()`'s old contract — not a crash."""
    import src.db_pg as db_pg

    monkeypatch.setattr(db_pg, "get_engine", lambda: _FakeEngine(None, exc=_programming_error()))
    current, head, db_ahead = db_pg._pg_revisions()

    assert current is None
    assert head
    assert db_ahead is False


def test_pg_revisions_transient_error_propagates(monkeypatch):
    """A transient DB error (not a missing table) must NOT be swallowed to
    None — that would mask a broken DB as "never stamped", falsely reading as
    schema drift and getting pinned in the health cache."""
    import src.db_pg as db_pg

    monkeypatch.setattr(db_pg, "get_engine", lambda: _FakeEngine(None, exc=_operational_error()))
    with pytest.raises(sa.exc.OperationalError):
        db_pg._pg_revisions()


def test_pg_revisions_emits_no_alembic_migration_log_noise(monkeypatch, caplog):
    """The revision read must not spew `alembic.runtime.migration` INFO on every
    health probe."""
    import src.db_pg as db_pg

    monkeypatch.setattr(db_pg, "get_engine", lambda: _FakeEngine("deadbeefcafe"))
    with caplog.at_level(logging.INFO):
        db_pg._pg_revisions()

    noise = [r for r in caplog.records if r.name.startswith("alembic.runtime.migration")]
    assert not noise, f"health probe logged alembic migration noise: {[r.message for r in noise]}"
