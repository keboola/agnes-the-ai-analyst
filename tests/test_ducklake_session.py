"""``src/ducklake_session.py`` — reader/writer singleton contract, DuckDB-file
catalog (no Postgres required).

Mirrors ``tests/test_analytics_db_singleton.py``'s singleton-contract style:
same connection reused across calls, cursor-close doesn't tear down the
connection, config change forces a fresh (re)open, close+reopen is clean.
The Postgres-catalog variant of the same contract (plus the "exactly one
connection per ATTACH" invariant) lives in
``tests/db_pg/test_ducklake_pg_catalog.py``.

If the ``ducklake`` DuckDB extension cannot be installed in this
environment (offline / air-gapped CI), every test here is skipped via the
module-level ``_require_ducklake`` fixture with a loud reason — this file
never fakes success.
"""

from __future__ import annotations

import pytest


def _extension_available() -> bool:
    import duckdb

    try:
        probe = duckdb.connect(":memory:")
        try:
            probe.execute("INSTALL ducklake")
            probe.execute("LOAD ducklake")
        finally:
            probe.close()
        return True
    except Exception:
        return False


_DUCKLAKE_EXTENSION_AVAILABLE = _extension_available()


pytestmark = pytest.mark.skipif(
    not _DUCKLAKE_EXTENSION_AVAILABLE,
    reason=(
        "DuckDB 'ducklake' extension could not be INSTALL/LOAD'ed in this "
        "environment (offline, or DuckDB build predates the extension). "
        "Skipping real DuckLake session tests rather than faking success — "
        "see src/ducklake_session.py::ducklake_available()."
    ),
)


@pytest.fixture(autouse=True)
def _reset_ducklake_singletons(monkeypatch, tmp_path):
    """Fresh DATA_DIR + clean module singleton state for every test.

    Reset before AND after so a leaked connection from a previous test
    (this file or elsewhere) never pollutes the case under inspection —
    same rationale as ``tests/test_analytics_db_singleton.py``.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for var in ("AGNES_DUCKLAKE_CATALOG_DSN", "AGNES_DUCKLAKE_DATA_PATH"):
        monkeypatch.delenv(var, raising=False)

    import src.analytics_backend as ab
    import src.ducklake_session as ds

    ab.reset_analytics_backend_cache()
    ds.close_ducklake_sessions()
    yield
    ds.close_ducklake_sessions()
    ab.reset_analytics_backend_cache()


def test_ducklake_available_true_when_extension_loads():
    from src.ducklake_session import ducklake_available

    assert ducklake_available() is True


def test_file_catalog_write_then_read_roundtrip():
    """Writer creates a table; a separately-opened reader singleton sees
    it after commit — the basic snapshot-visibility contract."""
    from src.ducklake_session import get_ducklake_read, get_ducklake_write

    w = get_ducklake_write()
    w.execute("CREATE SCHEMA IF NOT EXISTS lake.src1")
    w.execute("CREATE OR REPLACE TABLE lake.src1.t1 AS SELECT 1 AS x")
    w.close()

    r = get_ducklake_read()
    assert r.execute("SELECT * FROM lake.src1.t1").fetchall() == [(1,)]
    r.close()


def test_reader_sees_write_committed_after_reader_already_open():
    """A reader opened *before* the writer commits still sees the new
    row on its next query — DuckLake/DuckDB auto-commit means each
    query reads the latest committed snapshot at query time."""
    from src.ducklake_session import get_ducklake_read, get_ducklake_write

    w = get_ducklake_write()
    w.execute("CREATE SCHEMA IF NOT EXISTS lake.src1")
    w.execute("CREATE OR REPLACE TABLE lake.src1.t1 AS SELECT 1 AS x")

    r = get_ducklake_read()
    assert r.execute("SELECT * FROM lake.src1.t1").fetchall() == [(1,)]

    w.execute("INSERT INTO lake.src1.t1 VALUES (2)")

    assert sorted(x for (x,) in r.execute("SELECT * FROM lake.src1.t1").fetchall()) == [1, 2]
    r.close()
    w.close()


def test_read_singleton_caches_connection():
    """Two consecutive get_ducklake_read() calls share the same
    underlying connection — cursors differ, connection doesn't."""
    import src.ducklake_session as ds

    cur1 = ds.get_ducklake_read()
    cur2 = ds.get_ducklake_read()
    assert cur1 is not cur2
    assert ds._read_conn is not None
    cur1.execute("CREATE SCHEMA IF NOT EXISTS lake.probe_src")
    cur1.execute("CREATE OR REPLACE TABLE lake.probe_src.t AS SELECT 42 AS x")
    assert cur2.execute("SELECT * FROM lake.probe_src.t").fetchall() == [(42,)]


def test_closing_cursor_does_not_close_connection():
    from src.ducklake_session import get_ducklake_read
    import src.ducklake_session as ds

    cur1 = get_ducklake_read()
    cur1.close()
    assert ds._read_conn is not None
    cur2 = get_ducklake_read()
    cur2.execute("SELECT 1")


def test_reopens_on_data_dir_change(tmp_path, monkeypatch):
    from src.ducklake_session import get_ducklake_read
    import src.ducklake_session as ds

    cur1 = get_ducklake_read()
    first_conn = ds._read_conn
    assert first_conn is not None

    new_dir = tmp_path / "other"
    new_dir.mkdir()
    monkeypatch.setenv("DATA_DIR", str(new_dir))

    cur2 = get_ducklake_read()
    assert ds._read_conn is not None
    assert ds._read_conn is not first_conn
    cur1.close()
    cur2.close()


def test_close_reopen_cycle_is_clean():
    """close_ducklake_sessions() + a fresh get_ducklake_read() must work
    repeatedly without error (guards against a stale-attach regression)."""
    from src.ducklake_session import close_ducklake_sessions, get_ducklake_read, get_ducklake_write

    for i in range(3):
        w = get_ducklake_write()
        w.execute("CREATE SCHEMA IF NOT EXISTS lake.cycle_src")
        w.execute(f"CREATE OR REPLACE TABLE lake.cycle_src.t AS SELECT {i} AS x")
        w.close()

        r = get_ducklake_read()
        assert r.execute("SELECT * FROM lake.cycle_src.t").fetchall() == [(i,)]
        r.close()

        close_ducklake_sessions()


def test_file_catalog_reader_and_writer_share_one_physical_connection():
    """DuckDB refuses a second same-process ATTACH of the same DuckLake
    *file* catalog target from a different connection (verified directly
    against DuckDB 1.5.2 — a same-process restriction stricter than the
    documented "file catalogs are single-process across processes").
    Both singletons must therefore share one physical connection for a
    file catalog — this pins that design choice."""
    import src.ducklake_session as ds

    r = ds.get_ducklake_read()
    w = ds.get_ducklake_write()
    assert ds._read_conn is ds._write_conn
    r.close()
    w.close()


def test_memory_caps_applied_to_reader_and_writer():
    from src.ducklake_session import get_ducklake_read, get_ducklake_write

    r = get_ducklake_read()
    (mem,) = r.execute("SELECT current_setting('memory_limit')").fetchone()
    assert mem  # a budget was applied (exact string varies by DuckDB rounding)
    r.close()

    w = get_ducklake_write()
    (mem_w,) = w.execute("SELECT current_setting('memory_limit')").fetchone()
    assert mem_w
    w.close()


def test_threads_setting_applied():
    from src.ducklake_session import get_ducklake_read

    r = get_ducklake_read()
    (threads,) = r.execute("SELECT current_setting('threads')").fetchone()
    assert int(threads) == 2
    r.close()


# --- pg_dsn_to_libpq converter (unit-level, no Postgres required) --------


def test_pg_dsn_to_libpq_basic_tcp_form():
    from src.ducklake_session import pg_dsn_to_libpq

    dsn = "postgresql://agnes:s3cret@pg-host:5432/agnes_ducklake"
    libpq = pg_dsn_to_libpq(dsn)
    assert "dbname='agnes_ducklake'" in libpq
    assert "user='agnes'" in libpq
    assert "password='s3cret'" in libpq
    assert "host='pg-host'" in libpq
    assert "port=5432" in libpq


def test_pg_dsn_to_libpq_handles_sqlalchemy_driver_suffix():
    from src.ducklake_session import pg_dsn_to_libpq

    dsn = "postgresql+psycopg://agnes:pw@pg-host:5432/agnes_ducklake"
    libpq = pg_dsn_to_libpq(dsn)
    assert "dbname='agnes_ducklake'" in libpq
    assert "host='pg-host'" in libpq


def test_pg_dsn_to_libpq_unix_socket_host_query_param():
    """pgserver-style URL: no netloc host, socket dir rides in ?host=."""
    from src.ducklake_session import pg_dsn_to_libpq

    dsn = "postgresql://postgres:@/postgres?host=/tmp/agnes-pgserver-abc123"
    libpq = pg_dsn_to_libpq(dsn)
    assert "host='/tmp/agnes-pgserver-abc123'" in libpq
    assert "dbname='postgres'" in libpq


def test_pg_dsn_to_libpq_passthrough_query_params():
    from src.ducklake_session import pg_dsn_to_libpq

    dsn = "postgresql://agnes:pw@pg-host/agnes_ducklake?sslmode=require"
    libpq = pg_dsn_to_libpq(dsn)
    assert "sslmode='require'" in libpq


def test_pg_dsn_to_libpq_escapes_special_characters():
    """A password containing a single quote and a space must round-trip
    through libpq's own escaping without corrupting the keyword string."""
    from src.ducklake_session import pg_dsn_to_libpq

    dsn = "postgresql://user:pa%27ss%20word@myhost:5432/mydb"
    libpq = pg_dsn_to_libpq(dsn)
    assert "password='pa\\'ss word'" in libpq


def test_pg_dsn_to_libpq_rejects_non_postgres_scheme():
    from src.ducklake_session import pg_dsn_to_libpq

    with pytest.raises(ValueError):
        pg_dsn_to_libpq("mysql://user@host/db")


def test_is_postgres_dsn_detects_url_forms_and_rejects_file_paths():
    from src.ducklake_session import _is_postgres_dsn

    assert _is_postgres_dsn("postgresql://host/db") is True
    assert _is_postgres_dsn("postgres://host/db") is True
    assert _is_postgres_dsn("postgresql+psycopg://host/db") is True
    assert _is_postgres_dsn("/data/analytics/catalog.ducklake") is False


def test_reset_ducklake_available_cache_forces_reprobe(monkeypatch):
    import src.ducklake_session as ds

    assert ds.ducklake_available() is True
    ds.reset_ducklake_available_cache()
    assert ds._available_cache is None
    assert ds.ducklake_available() is True


# ---------------------------------------------------------------------------
# validate_ducklake_migration_prerequisites (wave-2G Task 6)
# ---------------------------------------------------------------------------


def test_validate_prerequisites_ok_for_file_catalog_single_process(monkeypatch):
    """Default test env: file catalog (no AGNES_DUCKLAKE_CATALOG_DSN set,
    per the autouse fixture), single-process — no problems."""
    import app.startup_guards as guards
    from src.ducklake_session import validate_ducklake_migration_prerequisites

    monkeypatch.setattr(guards, "is_multi_process", lambda: False)
    assert validate_ducklake_migration_prerequisites() == []


def test_validate_prerequisites_flags_multi_process_without_pg_dsn(monkeypatch):
    """A file-catalog target under a multi-process topology is refused —
    mirrors app.startup_guards.validate_deployment's own boot-time check,
    but surfaced BEFORE the (potentially large) migration rebuild runs."""
    import app.startup_guards as guards
    from src.ducklake_session import validate_ducklake_migration_prerequisites

    monkeypatch.setattr(guards, "is_multi_process", lambda: True)
    problems = validate_ducklake_migration_prerequisites()
    assert len(problems) == 1
    assert "multi-process" in problems[0]
    assert "Postgres DSN" in problems[0]


def test_validate_prerequisites_flags_missing_extension(monkeypatch):
    import app.startup_guards as guards
    import src.ducklake_session as ds

    monkeypatch.setattr(guards, "is_multi_process", lambda: False)
    monkeypatch.setattr(ds, "ducklake_available", lambda: False)
    problems = ds.validate_ducklake_migration_prerequisites()
    assert len(problems) == 1
    assert "extension" in problems[0]


def test_validate_prerequisites_reports_both_problems_at_once(monkeypatch):
    """Every applicable check runs — an operator sees the extension AND
    the multi-process/file-catalog mismatch in one pass, not one fix at a
    time."""
    import app.startup_guards as guards
    import src.ducklake_session as ds

    monkeypatch.setattr(guards, "is_multi_process", lambda: True)
    monkeypatch.setattr(ds, "ducklake_available", lambda: False)
    problems = ds.validate_ducklake_migration_prerequisites()
    assert len(problems) == 2


def test_validate_prerequisites_pg_catalog_missing_database_create_also_fails(monkeypatch, tmp_path):
    """Postgres-catalog path, without needing a real server: the ATTACH
    probe raises a "database does not exist" error, and the auto-repair
    CREATE DATABASE attempt itself fails (e.g. insufficient privilege) —
    the exact manual command must be surfaced so the operator isn't left
    guessing."""
    import src.ducklake_session as ds

    monkeypatch.setenv("AGNES_DUCKLAKE_CATALOG_DSN", "postgresql://user:pw@myhost:5432/agnes_ducklake")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    def _fake_attach_ducklake(conn, *, catalog_dsn, data_path):
        raise RuntimeError('database "agnes_ducklake" does not exist')

    class _FakePsycopg:
        @staticmethod
        def connect(*args, **kwargs):
            raise RuntimeError("permission denied to create database")

    monkeypatch.setattr(ds, "_attach_ducklake", _fake_attach_ducklake)
    monkeypatch.setitem(__import__("sys").modules, "psycopg", _FakePsycopg)

    problems = ds.validate_ducklake_migration_prerequisites()
    assert len(problems) == 1
    assert 'CREATE DATABASE "agnes_ducklake";' in problems[0]


def test_validate_prerequisites_pg_catalog_missing_database_auto_create_succeeds(monkeypatch, tmp_path):
    """Same starting failure as above, but the CREATE DATABASE attempt
    succeeds and the retried attach then succeeds too — no problems
    reported, migration proceeds without any manual operator step."""
    import src.ducklake_session as ds

    monkeypatch.setenv("AGNES_DUCKLAKE_CATALOG_DSN", "postgresql://user:pw@myhost:5432/agnes_ducklake")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    attempts = {"n": 0}

    def _fake_attach_ducklake(conn, *, catalog_dsn, data_path):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError('database "agnes_ducklake" does not exist')
        # second call (post-repair retry) succeeds — no-op

    class _FakeAdminConn:
        def execute(self, *args, **kwargs):
            pass

        def close(self):
            pass

    class _FakePsycopg:
        @staticmethod
        def connect(*args, **kwargs):
            return _FakeAdminConn()

    monkeypatch.setattr(ds, "_attach_ducklake", _fake_attach_ducklake)
    monkeypatch.setitem(__import__("sys").modules, "psycopg", _FakePsycopg)

    problems = ds.validate_ducklake_migration_prerequisites()
    assert problems == []
    assert attempts["n"] == 2


def test_validate_prerequisites_pg_catalog_other_failure_not_treated_as_missing_db(monkeypatch, tmp_path):
    """A non-"does not exist" failure (auth, network, ...) is reported
    verbatim and never triggers the CREATE DATABASE repair path."""
    import src.ducklake_session as ds

    monkeypatch.setenv("AGNES_DUCKLAKE_CATALOG_DSN", "postgresql://user:pw@myhost:5432/agnes_ducklake")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    def _fake_attach_ducklake(conn, *, catalog_dsn, data_path):
        raise RuntimeError("could not connect to server: Connection refused")

    monkeypatch.setattr(ds, "_attach_ducklake", _fake_attach_ducklake)

    problems = ds.validate_ducklake_migration_prerequisites()
    assert len(problems) == 1
    assert "Connection refused" in problems[0]
    assert "CREATE DATABASE" not in problems[0]


def test_dsn_with_database_swaps_path_keeps_query_and_netloc():
    from src.ducklake_session import _dsn_with_database

    swapped = _dsn_with_database("postgresql://user:pw@host:5432/agnes_ducklake?sslmode=require", "postgres")
    assert swapped == "postgresql://user:pw@host:5432/postgres?sslmode=require"


def test_ducklake_target_database_name_extracts_bare_dbname():
    from src.ducklake_session import _ducklake_target_database_name

    assert _ducklake_target_database_name("postgresql://user:pw@host:5432/agnes_ducklake") == "agnes_ducklake"
