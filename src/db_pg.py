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
  1. ``DATABASE_URL`` environment variable (preferred; 12-factor convention)
  2. ``AGNES_DB_URL`` environment variable (deprecated alias — logs a warning)

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
    """Return the Postgres URL using fallback chain:

      1. ``instance.yaml::database.url`` (admin-controlled, runtime-mutable).
      2. ``DATABASE_URL`` env var (12-factor convention).
      3. ``AGNES_DB_URL`` env var (deprecated alias — warning logged).

    Raises RuntimeError when no URL is configured.
    """
    import logging
    logger = logging.getLogger(__name__)

    # 1. instance.yaml
    try:
        from src.db_state_machine import read_backend_state
        _state, yaml_url = read_backend_state()
        if yaml_url:
            return yaml_url
    except Exception:
        # State module may be unavailable during early startup; fall
        # through to env vars.
        pass

    # 2. DATABASE_URL
    db_url = os.environ.get("DATABASE_URL")
    if db_url:
        return db_url

    # 3. AGNES_DB_URL (legacy)
    legacy = os.environ.get("AGNES_DB_URL")
    if legacy:
        logger.warning(
            "AGNES_DB_URL is deprecated — rename to DATABASE_URL (12-factor convention)"
        )
        return legacy

    raise RuntimeError(
        "Postgres URL is unset: set instance.yaml::database.url via "
        "/api/admin/db/migrate, or set DATABASE_URL env var"
    )


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
            # Attach the dev debug-toolbar query capture (idempotent; a no-op on
            # every non-debug request and in prod — see app/debug/postgres_panel.py).
            try:
                from app.debug.postgres_panel import instrument_engine

                instrument_engine(_engine)
            except Exception:
                pass
        return _engine


#: Env escape hatch — set to ``1`` to skip the startup Alembic revision
#: check and boot anyway. For emergency boots only (e.g. an operator
#: needs the app up to reach the admin UI / API and apply migrations by
#: hand). Mirrors the manual workaround in issue #636.
_SKIP_REVISION_CHECK_ENV = "AGNES_SKIP_PG_REVISION_CHECK"


def _alembic_config():
    """Build the Alembic ``Config`` bound to this repo's ``alembic.ini``.

    ``script_location`` is set explicitly to the repo's ``migrations/``
    dir (matching ``migrations/env.py`` + the test fixtures) so the
    resolution is robust regardless of the process cwd at boot.
    """
    from pathlib import Path

    from alembic.config import Config

    repo_root = Path(__file__).resolve().parent.parent
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "migrations"))
    return cfg


def assert_pg_at_head() -> None:
    """Fail closed unless the Postgres DB is at the head Alembic revision.

    The DuckDB backend self-migrates on every connect (``src/db.py``
    ladder via ``get_system_db``); the Postgres backend does NOT —
    ``alembic upgrade head`` runs only from the compose ``migrate``
    one-shot and the ``/api/admin/db/migrate`` flow, both one-time. A
    fresh image that expects a newer revision against a PG stamped at an
    older one boots "healthy" but 500s every write touching a post-stamp
    column (issue #636). This converts that silent drift into an
    operator-visible boot refusal.

    Reads the DB's current revision via
    ``MigrationContext.configure(conn).get_current_revision()`` and the
    script head via ``ScriptDirectory.get_current_head()``. Raises
    ``RuntimeError`` when they disagree (including the never-stamped
    ``current is None`` case) naming both revisions and the manual
    remediation. A no-op when they match.

    Honors the ``AGNES_SKIP_PG_REVISION_CHECK=1`` escape hatch for
    emergency boots. This check is PG-only by design — DuckDB needs no
    equivalent because it self-migrates.
    """
    import logging

    from alembic.migration import MigrationContext
    from alembic.script import ScriptDirectory

    logger = logging.getLogger(__name__)

    if os.environ.get(_SKIP_REVISION_CHECK_ENV) == "1":
        logger.warning(
            "%s=1 — skipping the Postgres Alembic revision check. "
            "The DB may be behind the app's expected schema; writes to "
            "newer columns/tables may 500. Apply `alembic upgrade head` "
            "(or the compose `migrate` one-shot) and unset this flag.",
            _SKIP_REVISION_CHECK_ENV,
        )
        return

    engine = get_engine()
    with engine.connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision()

    head = ScriptDirectory.from_config(_alembic_config()).get_current_head()

    if current == head:
        return

    current_label = current if current is not None else "<none — never stamped>"
    raise RuntimeError(
        "Postgres schema is behind the application: the DB is at Alembic "
        f"revision {current_label!r} but this image expects head {head!r}. "
        "Writes touching columns/tables added after the DB's revision will "
        "fail (issue #636). Apply the pending migrations before serving:\n"
        "  - one-shot:  alembic upgrade head\n"
        "  - compose:   docker compose -f docker-compose.postgres.yml run --rm migrate\n"
        "Set AGNES_SKIP_PG_REVISION_CHECK=1 to boot anyway (emergency only)."
    )


def dispose_engine() -> None:
    """Dispose the singleton engine + clear the cache.

    Next ``get_engine()`` call will re-resolve the URL and rebuild the
    engine. Called by ``POST /api/admin/db/migrate`` after a successful
    backend flip to make new repository operations land on the new
    backend without an app restart (though the app DOES restart on
    most migrations — this is a defence-in-depth runtime path).
    """
    global _engine, _session_factory
    with _lock:
        if _engine is not None:
            _engine.dispose()
            _engine = None
        _session_factory = None


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
