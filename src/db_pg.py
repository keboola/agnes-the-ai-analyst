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
        logger.warning("AGNES_DB_URL is deprecated — rename to DATABASE_URL (12-factor convention)")
        return legacy

    raise RuntimeError(
        "Postgres URL is unset: set instance.yaml::database.url via /api/admin/db/migrate, or set DATABASE_URL env var"
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

#: Set to ``0`` to disable startup auto-migration and keep the fail-closed
#: behavior of ``assert_pg_at_head`` — for deployments whose pipeline owns
#: migrations (compose ``migrate`` one-shot, CI). Default is on: the PG
#: backend self-migrates at startup exactly like the DuckDB ladder does on
#: connect (issue #636).
_AUTO_MIGRATE_ENV = "AGNES_PG_AUTO_MIGRATE"

#: Fixed application-wide advisory-lock key serializing the startup
#: auto-migration across replicas (app + any sibling container sharing the
#: DB). Arbitrary constant — must simply never be reused for another lock
#: in this codebase.
_PG_MIGRATE_LOCK_KEY = 636_636_636_636


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


def _pg_revisions() -> tuple[Optional[str], Optional[str], bool]:
    """Return ``(db_current, script_head, db_ahead)``.

    ``db_current`` is the revision stamped in ``alembic_version`` (None
    when never stamped); ``script_head`` is the head of the migration
    scripts shipped in this image. ``db_ahead`` is True when the DB's
    revision is unknown to this image's scripts — the app-rollback case,
    whose remedy differs from plain drift.
    """
    from alembic.migration import MigrationContext
    from alembic.script import ScriptDirectory

    engine = get_engine()
    with engine.connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision()

    script = ScriptDirectory.from_config(_alembic_config())
    head = script.get_current_head()

    # A revision this image's migration scripts don't know means the DB is
    # AHEAD, not behind — the operator rolled the app back after a newer
    # image already migrated. The remedies differ, so say so.
    db_ahead = False
    if current is not None and current != head:
        try:
            script.get_revision(current)
        except Exception:
            db_ahead = True
    return current, head, db_ahead


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

    current, head, db_ahead = _pg_revisions()

    if current == head:
        return

    if db_ahead:
        raise RuntimeError(
            "Postgres schema is AHEAD of the application: the DB is at "
            f"Alembic revision {current!r}, which this image's migration "
            f"scripts do not contain (its head is {head!r}) — typically an "
            "app rollback after a newer image already migrated (issue "
            "#636). Roll the app image forward to one that knows this "
            "revision (preferred), or restore the DB backup matching this "
            "image. Set AGNES_SKIP_PG_REVISION_CHECK=1 to boot anyway "
            "(emergency only)."
        )

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


def ensure_pg_at_head() -> None:
    """Bring the Postgres schema to the head Alembic revision at startup.

    Part 2 of issue #636. ``assert_pg_at_head`` turned silent
    write-breakage into a boot refusal; on deployments with no migrate
    step that refusal is a crash-loop on every release carrying a
    migration. This closes the loop — when the DB is BEHIND, apply the
    pending migrations in-process, mirroring the DuckDB ladder's
    self-migration on connect (``src/db.py``).

    Safety properties:

    - **AHEAD stays fail-closed.** A revision unknown to this image means
      an app rollback after a newer image migrated; auto-rollback is never
      safe, so this delegates to ``assert_pg_at_head`` and refuses to boot.
    - **Replica-safe.** The upgrade runs under a session-scoped Postgres
      advisory lock (``_PG_MIGRATE_LOCK_KEY``) and re-checks the revision
      after acquiring it, so concurrent replicas serialize and the late
      acquirer no-ops instead of double-applying.
    - **Opt-out.** ``AGNES_PG_AUTO_MIGRATE=0`` restores the fail-closed
      check for pipeline-controlled deployments;
      ``AGNES_SKIP_PG_REVISION_CHECK=1`` still skips everything
      (emergency boots).
    - **Fail-closed on error.** If the upgrade itself fails (broken
      migration, missing DDL privileges), the boot aborts with the
      original remediation guidance — never serve on a half-migrated
      schema.
    """
    import logging

    logger = logging.getLogger(__name__)

    if os.environ.get(_SKIP_REVISION_CHECK_ENV) == "1":
        assert_pg_at_head()  # logs the skip warning and returns
        return
    if os.environ.get(_AUTO_MIGRATE_ENV, "1") == "0":
        assert_pg_at_head()
        return

    current, head, db_ahead = _pg_revisions()
    if current == head or db_ahead:
        assert_pg_at_head()  # no-op, or the AHEAD refusal
        return

    engine = get_engine()
    with engine.connect() as lock_conn:
        lock_conn.execute(
            sa.text("SELECT pg_advisory_lock(:key)"),
            {"key": _PG_MIGRATE_LOCK_KEY},
        )
        try:
            # Re-check under the lock — a sibling replica may have finished
            # the upgrade while this one waited.
            current, head, db_ahead = _pg_revisions()
            if current != head and not db_ahead:
                logger.warning(
                    "Postgres schema is behind the application (%s -> %s) — "
                    "auto-applying pending Alembic migrations (set %s=0 to "
                    "disable and fail closed instead; issue #636).",
                    current if current is not None else "<never stamped>",
                    head,
                    _AUTO_MIGRATE_ENV,
                )
                from alembic import command

                cfg = _alembic_config()
                # migrations/env.py resolves the URL from cfg.attributes
                # first — pass the app-resolved URL explicitly so overlay
                # (instance.yaml) deployments work without DATABASE_URL in
                # the environment, and keep env.py's hands off the app's
                # already-configured logging.
                cfg.attributes["sqlalchemy.url"] = _resolve_url()
                cfg.attributes["configure_logger"] = False
                try:
                    command.upgrade(cfg, "head")
                except Exception as exc:
                    raise RuntimeError(
                        "Automatic Alembic upgrade to head failed "
                        f"({exc}); refusing to serve on a half-migrated "
                        "schema (issue #636). Apply the migrations "
                        "manually:\n"
                        "  - one-shot:  alembic upgrade head\n"
                        "  - compose:   docker compose -f "
                        "docker-compose.postgres.yml run --rm migrate\n"
                        "Set AGNES_SKIP_PG_REVISION_CHECK=1 to boot anyway "
                        "(emergency only)."
                    ) from exc
                logger.warning("Postgres schema auto-migrated to head %s.", head)
        finally:
            lock_conn.execute(
                sa.text("SELECT pg_advisory_unlock(:key)"),
                {"key": _PG_MIGRATE_LOCK_KEY},
            )

    assert_pg_at_head()


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


#: Session-scoped PG advisory lock id for the startup seed block —
#: "AGNS" packed as an int, distinct from ``_PG_MIGRATE_LOCK_KEY``.
_SEED_LEASE_ID = 0x41474E53


def _lease_use_pg() -> bool:
    from src.repositories import use_pg

    return use_pg()


@contextlib.contextmanager
def seed_lease() -> Iterator[None]:
    """Serialize the startup seed block across concurrently-booting replicas.

    Several replicas can reach the lifespan's seed block at once on a
    Postgres backend (e.g. a rolling deploy or a cold multi-replica
    boot). The seeds themselves are idempotent, but running them
    unserialized invites duplicate-insert races on tables without a
    unique constraint to lean on. This wraps the block in a session-scoped
    Postgres advisory lock: losers block until the winner finishes, then
    run the (idempotent) seeds themselves rather than skipping them —
    correctness over throughput, and startup-only so the extra latency
    is a one-time cost.

    No-op on the DuckDB backend — Task 2's startup guard already
    restricts DuckDB app-state to a single process, so there is nothing
    to serialize.
    """
    if not _lease_use_pg():
        yield
        return
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(sa.text("SELECT pg_advisory_lock(:key)"), {"key": _SEED_LEASE_ID})
        try:
            yield
        finally:
            conn.execute(sa.text("SELECT pg_advisory_unlock(:key)"), {"key": _SEED_LEASE_ID})


#: Session-scoped PG advisory lock id for the orchestrator rebuild critical
#: section — "AGNT" packed as an int, distinct from ``_SEED_LEASE_ID`` and
#: ``_PG_MIGRATE_LOCK_KEY``.
_REBUILD_LEASE_ID = 0x41474E54


@contextlib.contextmanager
def rebuild_lease() -> Iterator[None]:
    """Serialize ``SyncOrchestrator.rebuild()``/``rebuild_source()`` across processes.

    In a role-split topology (a dedicated ``api`` process handling
    ``/api/sync/trigger`` + the Jira webhook, and a separate ``worker``
    process running enqueued jobs) both processes can independently reach
    the orchestrator's rebuild critical section. ``SyncOrchestrator``'s
    ``_rebuild_lock`` (a ``threading.Lock``) only serializes rebuilds
    *within* one process — it is invisible across processes — so a
    job-triggered rebuild in the worker and an HTTP-triggered rebuild in
    the api process can concurrently ATTACH/swap ``analytics.duckdb``,
    which is the known DuckDB corruption class this repo guards against
    elsewhere (see ``docs/architecture.md``).

    This wraps the rebuild critical section in a session-scoped Postgres
    advisory lock, blocking (not failing) until the current holder
    finishes, so the caller can just wait its turn: the in-process
    ``_rebuild_lock`` still runs first (cheap, avoids reaching Postgres
    when nothing outside this process can contend), and this lease adds
    the cross-process guarantee.

    No-op on the DuckDB backend — DuckDB app-state deployments are
    single-process (Task 2's startup guard), so there is no second
    process to serialize against.
    """
    if not _lease_use_pg():
        yield
        return
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(sa.text("SELECT pg_advisory_lock(:key)"), {"key": _REBUILD_LEASE_ID})
        try:
            yield
        finally:
            conn.execute(sa.text("SELECT pg_advisory_unlock(:key)"), {"key": _REBUILD_LEASE_ID})
