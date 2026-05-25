"""Postgres-backed app state.

This module is the Postgres equivalent of ``src/db.py::get_system_db()``
for everything that's *not* analytics. Repositories under
``src/repositories/*_pg.py`` import ``Base`` to declare models and
``get_engine`` / ``get_session`` to obtain a connection.

The engine is a process-wide singleton (matching the DuckDB pattern at
``src/db.py:937-959``); the first call creates the pool, subsequent
calls reuse it. ``dispose()`` tears it down — used by tests for
per-test isolation.

URL resolution priority:
  1. ``AGNES_DB_URL`` environment variable
  2. ``DATABASE_URL`` environment variable (12-factor fallback)

No defaulting to ``sqlite:///./tmp.db`` or similar — a missing URL is a
configuration error, not something to paper over.
"""
from __future__ import annotations

import contextlib
import os
import threading
from typing import Iterator, Optional

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """Declarative base for every Postgres-backed model in Agnes.

    SQLAlchemy 2.0 style — models use ``Mapped[...]`` + ``mapped_column``.
    Alembic's ``target_metadata`` in ``migrations/env.py`` is bound to
    ``Base.metadata``; autogenerate compares against it.
    """


_engine: Optional[sa.Engine] = None
_session_factory: Optional[sessionmaker] = None
_lock = threading.Lock()


def _resolve_url() -> str:
    url = os.environ.get("AGNES_DB_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "Postgres URL is unset: set AGNES_DB_URL (preferred) or DATABASE_URL"
        )
    return url


def get_engine() -> sa.Engine:
    """Return the process-wide Engine, creating it on first call.

    Connection pool tuning is conservative (5 + overflow 10) to match
    Cloud SQL's per-instance connection caps. Repository code holding
    sessions for long stretches should chunk work and release.
    """
    global _engine, _session_factory
    with _lock:
        if _engine is None:
            url = _resolve_url()
            _engine = sa.create_engine(
                url,
                future=True,
                pool_size=5,
                max_overflow=10,
                pool_pre_ping=True,
            )
            _session_factory = sessionmaker(bind=_engine, future=True, expire_on_commit=False)
        return _engine


@contextlib.contextmanager
def get_session() -> Iterator[Session]:
    """Yield a Session bound to the singleton engine.

    Commits or rolls back at exit; the session is always closed. Use
    this when you need transactional repository work.
    """
    get_engine()
    assert _session_factory is not None
    session = _session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def dispose() -> None:
    """Drop the singleton engine and clear the session factory.

    Call between test runs or after a config reload. Production code
    does NOT call this on normal request paths — it's reserved for
    explicit lifecycle events.
    """
    global _engine, _session_factory
    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _session_factory = None
