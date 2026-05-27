"""Postgres test fixtures.

Three backends, selected at fixture-resolution time via the
``AGNES_TEST_PG_BACKEND`` environment variable:

  - ``pgserver`` (default) — uses the ``pgserver`` package's bundled
    Postgres 16 binary. No system install, no Docker. Works on any dev
    box out of the box.
  - ``container`` — testcontainers boots ``postgres:16-alpine`` once per
    pytest session. Opt-in; requires a working Docker socket.
  - ``embedded`` — pytest-postgresql boots a system ``postgres`` binary
    (initdb on tmpfs). Opt-in; requires the binary on PATH.

Per-test isolation: the session-scoped engine boots PG once; each test
function gets a freshly DROP/CREATE'd ``public`` schema so a previous
test's tables can't leak. ~100x faster than recreating the container/
process per test, with equivalent observable behavior.
"""
from __future__ import annotations

import os
import shutil
from typing import Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session


_VALID_BACKENDS = {"container", "embedded", "pgserver"}


def _resolve_backend() -> str:
    """Return ``"pgserver"`` by default; honor ``AGNES_TEST_PG_BACKEND`` override.

    pgserver ships a Postgres 16 binary in its wheel — works on any dev box
    without Docker or system PG. container/embedded backends remain available
    as explicit opt-ins for fidelity testing or CI matrix runs.
    """
    explicit = os.environ.get("AGNES_TEST_PG_BACKEND")
    if explicit:
        if explicit not in _VALID_BACKENDS:
            raise ValueError(
                f"AGNES_TEST_PG_BACKEND={explicit!r} not in {_VALID_BACKENDS}"
            )
        return explicit
    return "pgserver"


def _start_container() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer("postgres:16-alpine", driver="psycopg")
    try:
        container.start()
    except Exception as exc:
        pytest.skip(f"docker unavailable for testcontainers: {exc}")
        return
    try:
        yield container.get_connection_url()
    finally:
        container.stop()


def _start_embedded() -> Iterator[str]:
    import tempfile
    from pytest_postgresql.executor import PostgreSQLExecutor

    postgres_bin = shutil.which("postgres")
    if not postgres_bin:
        pytest.skip("AGNES_TEST_PG_BACKEND=embedded but no `postgres` on PATH")
        return

    tmpdir = tempfile.mkdtemp(prefix="agnes-pg-")
    try:
        executor = PostgreSQLExecutor(
            executable=postgres_bin,
            host="127.0.0.1",
            port=None,
            user="postgres",
            password="",
            dbname="postgres",
            options="",
            startparams="",
            datadir=tmpdir,
            unixsocketdir="/tmp",
            logfile=os.path.join(tmpdir, "pg.log"),
            postgres_options="",
        )
        executor.start()
        try:
            url = (
                f"postgresql+psycopg://postgres@{executor.host}:{executor.port}/postgres"
            )
            yield url
        finally:
            executor.stop()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _start_pgserver() -> Iterator[str]:
    import pixeltable_pgserver as pgserver
    import tempfile

    tmpdir = tempfile.mkdtemp(prefix="agnes-pgserver-")
    server = pgserver.get_server(tmpdir, cleanup_mode=None)
    try:
        # pgserver returns a unix-socket URI; rewrite to psycopg dialect.
        raw_uri = server.get_uri()
        url = raw_uri.replace("postgresql://", "postgresql+psycopg://", 1)
        yield url
    finally:
        try:
            server.cleanup()
        except Exception:
            pass
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture(scope="session")
def pg_backend() -> str:
    """Expose the resolved backend name to tests that want to assert it."""
    return _resolve_backend()


@pytest.fixture(scope="session")
def _pg_url(pg_backend) -> Iterator[str]:
    """Boot a Postgres (once per session) and yield its SQLAlchemy URL."""
    if pg_backend == "container":
        yield from _start_container()
    elif pg_backend == "embedded":
        yield from _start_embedded()
    elif pg_backend == "pgserver":
        yield from _start_pgserver()
    else:
        raise ValueError(f"unknown backend {pg_backend!r}")


@pytest.fixture(scope="session")
def _pg_engine_session(_pg_url) -> Iterator[Engine]:
    engine = sa.create_engine(_pg_url, future=True)
    try:
        yield engine
    finally:
        engine.dispose()


def _drop_user_schema(engine: Engine) -> None:
    """Reset ``public`` so the next test sees a clean DB."""
    with engine.connect() as conn:
        conn.execute(sa.text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(sa.text("CREATE SCHEMA public"))
        conn.execute(sa.text("GRANT ALL ON SCHEMA public TO public"))
        conn.commit()


@pytest.fixture
def pg_engine(_pg_engine_session) -> Iterator[Engine]:
    """Per-test engine; empty ``public`` schema on entry."""
    _drop_user_schema(_pg_engine_session)
    yield _pg_engine_session


@pytest.fixture
def pg_session(pg_engine) -> Iterator[Session]:
    """Per-test SQLAlchemy session over the per-test engine."""
    with Session(pg_engine, future=True) as session:
        yield session


# ---------------------------------------------------------------------------
# parametrized backend harness — runs the same endpoint test twice, once
# against DuckDB and once against Postgres.
# ---------------------------------------------------------------------------

@pytest.fixture(params=["duckdb", "pg"], ids=["duck", "pg"])
def state_backend(request, monkeypatch, tmp_path, _pg_url, pg_engine):
    """Configure the app-state backend.

    Tests that consume ``seeded_app_both`` indirectly consume this and
    therefore run twice: once with ``AGNES_DB_URL`` unset (DuckDB path)
    and once with it set to the per-test pgserver instance + alembic
    upgraded to head.

    Tests that should ONLY run against one backend can override the
    parametrization::

        @pytest.mark.parametrize("state_backend", ["pg"], indirect=True)
        def test_pg_only_thing(seeded_app_both): ...
    """
    if request.param == "pg":
        # pg_engine already created the engine and bumped schema cleanly.
        # Run alembic upgrade head so the chain is materialised.
        from pathlib import Path
        from alembic import command
        from alembic.config import Config

        REPO_ROOT = Path(__file__).resolve().parents[2]
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
        cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
        command.upgrade(cfg, "head")

        # Seed Admin + Everyone groups (DuckDB does this in _seed_system_groups
        # on every connect; PG needs an explicit seed). Idempotent.
        with pg_engine.begin() as conn_:
            import uuid as _uuid
            for name, description in (
                ("Admin", "System: full access to all data and admin actions"),
                ("Everyone", "System: default group every user is implicitly a member of"),
            ):
                conn_.execute(
                    sa.text(
                        "INSERT INTO user_groups (id, name, description, is_system, created_by) "
                        "VALUES (:id, :name, :desc, TRUE, 'system:seed') "
                        "ON CONFLICT (name) DO UPDATE SET is_system = TRUE"
                    ),
                    {"id": _uuid.uuid4().hex, "name": name, "desc": description},
                )

        monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))

        # Force a fresh PG engine inside the app process
        import src.db_pg as db_pg
        db_pg.dispose()
    else:
        monkeypatch.delenv("AGNES_DB_URL", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)

    # Reset the factory module to pick up the env change on next import
    import importlib
    import src.repositories
    importlib.reload(src.repositories)

    yield request.param


@pytest.fixture
def seeded_app_both(state_backend, tmp_path, monkeypatch):
    """Backend-parametrized TestClient with seeded admin + analyst users.

    Drop-in for tests that want to verify endpoint behaviour identically
    against DuckDB and Postgres. Returns the same dict shape as the
    legacy ``seeded_app`` fixture (client + token strings + env), with
    one extra key ``backend`` for diagnostic assertions.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
    for sub in ("extracts", "analytics", "state", "notifications"):
        (tmp_path / sub).mkdir(exist_ok=True)

    from app.auth.jwt import create_access_token
    from app.main import create_app
    from fastapi.testclient import TestClient
    from src.repositories import users_repo, user_group_members_repo

    if state_backend == "duckdb":
        # DuckDB side: ensure system DB is created + system groups seeded
        from src.db import close_system_db, get_system_db
        close_system_db()
        get_system_db()  # triggers _ensure_schema + _seed_system_groups

    u = users_repo()
    u.create(id="admin1", email="admin@test.com", name="Admin")
    u.create(id="analyst1", email="analyst@test.com", name="Analyst")

    # Find Admin group id (seeded by either DuckDB _ensure_schema or the
    # PG fixture above)
    if state_backend == "duckdb":
        from src.db import get_system_db
        admin_gid = get_system_db().execute(
            "SELECT id FROM user_groups WHERE name = 'Admin'"
        ).fetchone()[0]
    else:
        import sqlalchemy as sa
        from src.db_pg import get_engine
        with get_engine().connect() as conn_:
            admin_gid = conn_.execute(
                sa.text("SELECT id FROM user_groups WHERE name = 'Admin'")
            ).scalar()

    user_group_members_repo().add_member("admin1", admin_gid, source="system_seed")

    app = create_app()
    client = TestClient(app)

    return {
        "client": client,
        "admin_token": create_access_token("admin1", "admin@test.com"),
        "analyst_token": create_access_token("analyst1", "analyst@test.com"),
        "backend": state_backend,
        "data_dir": tmp_path,
    }
