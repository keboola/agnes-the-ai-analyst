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

# Ensure every SQLAlchemy model is registered on ``src.db_pg.Base.metadata``
# before any test runs. Tests that call ``Base.metadata.create_all(pg_engine)``
# (e.g. ``test_db_state_migrator.py``) need the full model set; without this
# pre-import they fail on a pytest-xdist worker whose test slice doesn't
# transitively import ``src.models`` before the first such test runs
# (`relation "users" does not exist` from a half-populated metadata).
import src.models  # noqa: F401


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
            raise ValueError(f"AGNES_TEST_PG_BACKEND={explicit!r} not in {_VALID_BACKENDS}")
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
            url = f"postgresql+psycopg://postgres@{executor.host}:{executor.port}/postgres"
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
    # Record the owning session, AFTER get_server: initdb refuses a non-empty
    # data dir, so this file cannot be written up front. pgserver starts a
    # DETACHED postmaster (ppid == 1 from birth, and it outlives us), so
    # neither the postmaster's liveness nor its parentage tells a later run
    # whether this dir is still in use. Our PID is the only signal that does,
    # and the reaper reads it. Dying in the window before this write leaves an
    # owner-less dir, which the reaper conservatively keeps.
    from pathlib import Path

    from tests.db_pg.pgserver_reaper import OWNER_FILE

    Path(tmpdir, OWNER_FILE).write_text(f"{os.getpid()}\n")
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


@pytest.fixture(scope="session", autouse=True)
def _reap_orphaned_pgserver_data_dirs() -> None:
    """Best-effort removal of ``agnes-pgserver-*`` dirs orphaned by hard-killed runs.

    A run killed hard (SIGKILL, OOM, disk full) never reaches
    ``_start_pgserver``'s ``finally``, leaving ~300 MB data dirs in $TMPDIR
    forever — with ``cleanup_mode=None`` pgserver never stops or removes
    anything itself. Reap them at session start; concurrent worktree sessions
    are protected by the reaper's postmaster.pid liveness check and
    minimum-age guard. Under xdist only gw0 scans — the reap is global, one
    worker suffices.
    """
    if os.environ.get("PYTEST_XDIST_WORKER", "gw0") != "gw0":
        return
    import tempfile
    from pathlib import Path

    from tests.db_pg.pgserver_reaper import reap_orphaned_pgserver_dirs

    reap_orphaned_pgserver_dirs(Path(tempfile.gettempdir()))


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


# ---------------------------------------------------------------------------
# Module-scoped alembic fixture (Phase 7.13)
#
# Running alembic upgrade head once per module (rather than once per test)
# saves ~3-5 s per test.  Tests that need a clean slate TRUNCATE individual
# tables (handled by the autouse _truncate_pg_user_tables fixture below)
# rather than DROP/recreate the whole schema.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pg_engine_with_schema(_pg_engine_session) -> Engine:
    """Module-scoped: fresh schema + alembic head applied once per module.

    The ``public`` schema is dropped and recreated once at the start of each
    test module that requests this fixture — so two modules that both use it
    get independent schemas without re-running the PG process.  Individual
    tests rely on the autouse ``_truncate_pg_user_tables`` fixture to clear
    data rows between runs.
    """
    from pathlib import Path
    from alembic import command
    from alembic.config import Config

    REPO_ROOT = Path(__file__).resolve().parents[2]
    _drop_user_schema(_pg_engine_session)
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(_pg_engine_session.url)
    command.upgrade(cfg, "head")
    return _pg_engine_session


@pytest.fixture(autouse=True)
def _truncate_pg_user_tables(request) -> Iterator[None]:
    """Auto-applied per-test cleanup for tests that use
    ``pg_engine_with_schema``.

    Yields immediately (no setup cost) and on teardown TRUNCATEs all
    ``public`` tables except ``alembic_version``, leaving the schema intact
    so the module-scoped alembic fixture can be reused by the next test.

    Tests that do NOT use ``pg_engine_with_schema`` are unaffected — the
    early-return guard skips the teardown entirely.
    """
    yield
    if "pg_engine_with_schema" not in request.fixturenames:
        return
    engine: Engine = request.getfixturevalue("pg_engine_with_schema")
    with engine.begin() as conn:
        rows = conn.execute(
            sa.text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename != 'alembic_version'")
        ).fetchall()
        for (table,) in rows:
            conn.execute(sa.text(f'TRUNCATE TABLE "{table}" CASCADE'))


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

    Tests that should ONLY run against one backend skip the other inside the
    body (do NOT re-``@parametrize`` ``state_backend`` — re-parametrizing a name
    already supplied by this parametrized fixture is a duplicate-parametrization
    collection error under newer pytest)::

        def test_pg_only_thing(state_backend, seeded_app_both):
            if state_backend != "pg":
                pytest.skip("PG-only")
            ...
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

        admin_gid = get_system_db().execute("SELECT id FROM user_groups WHERE name = 'Admin'").fetchone()[0]
    else:
        import sqlalchemy as sa
        from src.db_pg import get_engine

        with get_engine().connect() as conn_:
            admin_gid = conn_.execute(sa.text("SELECT id FROM user_groups WHERE name = 'Admin'")).scalar()

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


# ---------------------------------------------------------------------------
# CLI fixture — CliRunner wired through the same in-process FastAPI app
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_client_both(seeded_app_both, monkeypatch):
    """CliRunner whose HTTP calls go to the in-process TestClient app.

    Patches cli.client.get_client and cli.client._get_shared_client so
    every CLI command hits the same FastAPI app as the web tests —
    no real ports, full API surface, both backends.
    """
    import contextlib
    from typer.testing import CliRunner

    tc = seeded_app_both["client"]
    admin_token = seeded_app_both["admin_token"]

    def _make_client(timeout=30.0):
        from starlette.testclient import TestClient as _TC

        return _TC(
            app=tc.app,
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    @contextlib.contextmanager
    def _patched_get_client(timeout=30.0):
        client = _make_client(timeout)
        try:
            yield client
        finally:
            client.close()

    import cli.client as _cli_client

    monkeypatch.setattr(_cli_client, "get_client", _patched_get_client)
    monkeypatch.setattr(_cli_client, "_get_shared_client", lambda: _make_client())
    _cli_client._SHARED_CLIENT = None
    monkeypatch.setenv("AGNES_SERVER_URL", "http://testserver")

    # v2_client uses httpx.get/post/etc. directly — patch each helper to
    # route through the in-process TestClient so CLI commands that call
    # v2_client (catalog, schema, my-stack, …) don't try a real TCP connection.
    import cli.v2_client as _v2_client
    from cli.v2_client import V2ClientError, _parse_error_body as _v2_parse_error

    def _v2_get(path, **params):
        c = _make_client()
        r = c.get(path, params=params or None)
        if r.status_code >= 400:
            raise V2ClientError(status_code=r.status_code, body=_v2_parse_error(r))
        return r.json()

    def _v2_post(path, payload=None):
        c = _make_client()
        r = c.post(path, json=payload)
        if r.status_code >= 400:
            raise V2ClientError(status_code=r.status_code, body=_v2_parse_error(r))
        return r.json()

    def _v2_delete(path):
        c = _make_client()
        r = c.delete(path)
        if r.status_code >= 400:
            raise V2ClientError(status_code=r.status_code, body=_v2_parse_error(r))
        return r.json() if r.content else {}

    def _v2_put(path, payload=None):
        c = _make_client()
        r = c.put(path, json=payload)
        if r.status_code >= 400:
            raise V2ClientError(status_code=r.status_code, body=_v2_parse_error(r))
        return r.json()

    monkeypatch.setattr(_v2_client, "api_get_json", _v2_get)
    monkeypatch.setattr(_v2_client, "api_post_json", _v2_post)
    monkeypatch.setattr(_v2_client, "api_delete", _v2_delete)
    monkeypatch.setattr(_v2_client, "api_put_json", _v2_put)

    # cli.commands.* modules do `from cli.v2_client import api_get_json` which
    # creates a local binding that is NOT updated by setattr on _v2_client above.
    # Under xdist, command modules are imported early in the process (before this
    # fixture runs), so we must also patch their local references directly.
    import sys as _sys

    _cmd_patches = {
        "api_get_json": _v2_get,
        "api_post_json": _v2_post,
        "api_delete": _v2_delete,
        "api_put_json": _v2_put,
    }
    for _mod_name, _mod in list(_sys.modules.items()):
        if _mod_name.startswith("cli.commands.") and _mod is not None:
            for _attr, _replacement in _cmd_patches.items():
                if hasattr(_mod, _attr):
                    monkeypatch.setattr(_mod, _attr, _replacement)

    runner = CliRunner()

    def invoke(args):
        from cli.main import app as cli_app

        return runner.invoke(cli_app, args, catch_exceptions=False)

    yield {
        "runner": runner,
        "invoke": invoke,
        "backend": seeded_app_both["backend"],
        "admin_token": admin_token,
        "analyst_token": seeded_app_both["analyst_token"],
        "client": tc,
        "data_dir": seeded_app_both["data_dir"],
    }


# ---------------------------------------------------------------------------
# registered_table_both — a queryable table registered in the active backend
# ---------------------------------------------------------------------------


@pytest.fixture
def registered_table_both(seeded_app_both):
    """Register a table via the API, write parquet + sync_state, yield table info.

    Returns {"table_id": str, "source_name": str, "data_dir": Path}.

    - ``table_id`` is the UUID from table_registry (used by download handler rglob
      and RBAC grants).
    - ``source_name`` is the human name ("smoke_orders") which is the key used in
      sync_state and the manifest ``tables`` dict.
    """
    import pandas as pd
    from src.repositories import sync_state_repo

    client = seeded_app_both["client"]
    admin_token = seeded_app_both["admin_token"]
    data_dir = seeded_app_both["data_dir"]
    headers = {"Authorization": f"Bearer {admin_token}"}

    source_name = "smoke_orders"
    bucket = "smoke_src"

    # Register first to get the table_id (UUID) the download handler looks up
    r = client.post(
        "/api/admin/register-table",
        json={
            "name": source_name,
            "source_type": "keboola",
            "bucket": bucket,
            "source_table": source_name,
            "query_mode": "local",
        },
        headers=headers,
    )
    assert r.status_code == 201, f"register-table failed: {r.text}"
    table_id = r.json()["id"]

    # Write parquet at extracts/{bucket}/data/{table_id}.parquet so the download
    # handler (which rglob-searches "data/{table_id}.parquet") can stream it.
    parquet_dir = data_dir / "extracts" / bucket / "data"
    parquet_dir.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame({"id": [1, 2, 3], "amount": [10.0, 20.0, 30.0]})
    parquet_path = parquet_dir / f"{table_id}.parquet"
    df.to_parquet(str(parquet_path))

    # Populate sync_state directly so the manifest returns this table.
    # sync_state.table_id mirrors table_registry.name ("smoke_orders"), which is
    # the key the manifest uses in its ``tables`` dict.
    sync_state_repo().update_sync(
        table_id=source_name,
        rows=3,
        file_size_bytes=parquet_path.stat().st_size,
        hash="",
    )

    yield {"table_id": table_id, "source_name": source_name, "data_dir": data_dir}
