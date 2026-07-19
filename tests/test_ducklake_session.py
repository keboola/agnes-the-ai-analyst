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
