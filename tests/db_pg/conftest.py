"""Postgres test fixtures.

Three backends, selected at fixture-resolution time via the
``AGNES_TEST_PG_BACKEND`` environment variable:

  - ``container`` — testcontainers boots ``postgres:16-alpine`` once per
    pytest session. Highest fidelity; requires a working Docker socket.
  - ``embedded`` — pytest-postgresql boots a system ``postgres`` binary
    (initdb on tmpfs). Fast (~0.5s); requires the binary on PATH.
  - ``pgserver`` — uses the ``pgserver`` package's bundled Postgres 16
    binary. No system install, no Docker. The universal fallback.

If the env var is unset, autodetect picks the first that's actually
usable, in priority order: container → embedded → pgserver. Pgserver
always works (it ships its own binary), so downstream tests never silent-
skip; they always run against a real PG.

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


def _docker_usable() -> bool:
    """Cheaply check whether the Docker socket is reachable.

    We don't shell out to ``docker info`` — too slow. Instead, try to
    connect to ``/var/run/docker.sock`` directly. Equivalent fidelity
    for our purpose (autodetection); the real verification happens
    when testcontainers actually tries to start a container.
    """
    if not shutil.which("docker"):
        return False
    sock = "/var/run/docker.sock"
    if not os.path.exists(sock):
        return False
    return os.access(sock, os.R_OK | os.W_OK)


def _resolve_backend() -> str:
    """Return ``"container"`` | ``"embedded"`` | ``"pgserver"``.

    Order: explicit env var → working Docker → system postgres → pgserver
    (always works).
    """
    explicit = os.environ.get("AGNES_TEST_PG_BACKEND")
    if explicit:
        if explicit not in _VALID_BACKENDS:
            raise ValueError(
                f"AGNES_TEST_PG_BACKEND={explicit!r} not in {_VALID_BACKENDS}"
            )
        return explicit
    if _docker_usable():
        return "container"
    if shutil.which("postgres"):
        return "embedded"
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
    import pgserver
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


@pytest.fixture(scope="module")
def pg_engine_migrated_module(_pg_engine_session) -> Iterator[Engine]:
    """Module-scoped engine — schema dropped + alembic upgrade head ONCE
    per test module.

    Codex finding #14 (SPLIT): the per-test ``store_engine`` fixture in
    ``test_store_pg.py`` re-ran the full alembic chain for every test in
    the file (~12 migrations × N tests). Used heavily across the
    marketplace / store / flea-market cluster — meaningful real-world
    cost. Tests that need a clean PG per test should still depend on
    ``pg_engine``; tests that just need "schema present, my own rows"
    can switch to this fixture + the ``pg_truncate_all`` helper below
    for a per-test wipe that's ~100× cheaper than re-migrating.

    Trade-off: schema drift between migrations and the in-memory model
    is no longer caught test-by-test in modules that use this fixture
    (the run-once setup means later tests inherit any partial-migrate
    state). The drift-detector test in ``test_alembic_roundtrip.py``
    still runs at module-fresh scope and catches that class of bug.
    """
    from pathlib import Path
    from alembic import command
    from alembic.config import Config

    _drop_user_schema(_pg_engine_session)
    repo_root = Path(__file__).resolve().parents[2]
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(_pg_engine_session.url)
    command.upgrade(cfg, "head")
    yield _pg_engine_session


def pg_truncate_all(engine: Engine) -> None:
    """TRUNCATE every base table in ``public`` (RESTART IDENTITY CASCADE).

    Companion to ``pg_engine_migrated_module``: schema stays intact,
    rows go. Preserves ``alembic_version`` so a follow-up
    ``alembic upgrade head`` is a no-op. Faster + safer than
    drop-schema + re-migrate when only data isolation is needed.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname='public' AND tablename <> 'alembic_version'"
            )
        ).all()
    targets = [r[0] for r in rows]
    if not targets:
        return
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                f"TRUNCATE TABLE {', '.join(targets)} RESTART IDENTITY CASCADE"
            )
        )


# ---------------------------------------------------------------------------
# App-state backend setup (single-backend post-cutover).
# ---------------------------------------------------------------------------

@pytest.fixture
def state_backend(monkeypatch, tmp_path, _pg_url, pg_engine):
    """Configure the app-state backend.

    Originally parametrised over ``[duckdb, pg]`` for the dual-write
    cutover window. Post-cutover the DuckDB app-state code is gone;
    the fake ``params=["pg"]`` parametrisation was confusing — Codex
    finding #13 — so it's a plain fixture now. Returns the literal
    string ``"pg"`` so existing callers that key on
    ``state_backend == "pg"`` keep working without a signature
    change.
    """
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

    # Seed Admin + Everyone groups. Idempotent.
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

    # Reset the factory module to pick up the env change on next import
    import importlib
    import src.repositories
    importlib.reload(src.repositories)

    yield "pg"


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

    u = users_repo()
    u.create(id="admin1", email="admin@test.com", name="Admin")
    u.create(id="analyst1", email="analyst@test.com", name="Analyst")

    # Admin group is seeded by the ``state_backend`` fixture above.
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
