"""``src/ducklake_session.py`` reader plane — shared-PG-catalog contract.

The load-test-critical property of the three-plane split: the api-role
reader is a strictly READ-ONLY plane. It must resolve the writer-created
``lake.main`` remote wrapper views through the *shared Postgres catalog*
(genuine cross-instance visibility — a separate physical DuckDB connection
from the writer, not the file-catalog same-connection shortcut) while
committing NO new catalog snapshots itself. A ``CREATE OR REPLACE VIEW``
against a DuckLake catalog commits a fresh snapshot on every call even when
unchanged (verified vs DuckDB 1.5.2), so a reader that did per-request view
creation would be unbounded catalog write-amplification onto the shared PG
catalog under concurrent load.

Uses the pgserver fixture as the shared PG catalog, mirroring
``tests/db_pg/test_ducklake_pg_catalog.py`` (which this complements: that
file covers the write/read roundtrip + connection sizing; this file pins
the reader's no-snapshot invariant and cross-instance resolution of a
writer-owned remote view). Same loud-skip contract if the DuckDB
``ducklake``/``postgres`` extensions can't be installed here.
"""

from __future__ import annotations

import pytest

from tests.conftest import create_mock_extract


def _extensions_available() -> bool:
    import duckdb

    try:
        probe = duckdb.connect(":memory:")
        try:
            probe.execute("INSTALL ducklake")
            probe.execute("LOAD ducklake")
            probe.execute("INSTALL postgres")
            probe.execute("LOAD postgres")
        finally:
            probe.close()
        return True
    except Exception:
        return False


_EXTENSIONS_AVAILABLE = _extensions_available()

pytestmark = pytest.mark.skipif(
    not _EXTENSIONS_AVAILABLE,
    reason=(
        "DuckDB 'ducklake'/'postgres' extensions could not be INSTALL/LOAD'ed "
        "in this environment (offline, or DuckDB build predates them). "
        "Skipping real DuckLake-over-Postgres reader tests rather than faking "
        "success — see src/ducklake_session.py::ducklake_available()."
    ),
)


@pytest.fixture
def ducklake_reader_pg_env(pg_engine, monkeypatch, tmp_path):
    """DuckLake analytics catalog on the per-test pgserver schema, with the
    orchestrator dispatched onto the ducklake backend.

    ``AGNES_ANALYTICS_BACKEND=ducklake`` is required so ``SyncOrchestrator``
    routes through the copy-ingest / remote-view path (not the legacy
    server.duckdb rebuild). ``table_registry`` (system.duckdb) stays a
    DuckDB file under ``DATA_DIR`` — only the analytics catalog is Postgres.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGNES_ANALYTICS_BACKEND", "ducklake")
    dsn = pg_engine.url.render_as_string(hide_password=False)
    monkeypatch.setenv("AGNES_DUCKLAKE_CATALOG_DSN", dsn)
    for var in ("AGNES_DUCKLAKE_DATA_PATH",):
        monkeypatch.delenv(var, raising=False)

    (tmp_path / "extracts").mkdir()
    (tmp_path / "analytics").mkdir()
    (tmp_path / "state").mkdir()

    import src.analytics_backend as ab
    import src.ducklake_session as ds

    ab.reset_analytics_backend_cache()
    ds.close_ducklake_sessions()
    yield {"data_dir": tmp_path, "extracts_dir": tmp_path / "extracts"}
    ds.close_ducklake_sessions()
    ab.reset_analytics_backend_cache()


def _register_remote(source: str, name: str) -> None:
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository

    conn = get_system_db()
    try:
        TableRegistryRepository(conn).register(
            id=f"{source}.{name}",
            name=name,
            source_type=source,
            bucket="dataset",
            source_table=name,
            query_mode="remote",
        )
    finally:
        conn.close()


def test_reader_resolves_writer_remote_view_cross_instance_no_new_snapshots(ducklake_reader_pg_env):
    """WRITER (one PG-catalog connection) creates the remote wrapper view +
    commits; a separate READER connection on the SAME PG catalog resolves
    it — and running N reader queries commits NO new catalog snapshots."""
    import src.ducklake_session as ds
    from src.ducklake_session import get_ducklake_read
    from src.orchestrator import SyncOrchestrator

    extracts_dir = ducklake_reader_pg_env["extracts_dir"]
    create_mock_extract(extracts_dir, "bigquery", [{"name": "remote_tbl", "data": [], "query_mode": "remote"}])
    _register_remote("bigquery", "remote_tbl")

    # Reader session opened BEFORE the writer commits the view — attaches
    # the remote extract source now (registry already has the row).
    r_early = get_ducklake_read()
    r_early.close()

    # WRITER: rebuild creates lake.main.remote_tbl + commits, on its OWN
    # PG-catalog connection (independent of the reader's).
    SyncOrchestrator().rebuild()
    assert ds._read_conn is not ds._write_conn, "PG catalog must give reader and writer independent connections"

    # READER: a fresh cursor on the pre-existing reader connection sees the
    # writer's commit through the shared PG catalog (cross-instance MVCC).
    r = get_ducklake_read()
    try:
        assert r.execute('SELECT * FROM lake."main"."remote_tbl"').fetchall() == []
        assert r.execute('SELECT * FROM "remote_tbl"').fetchall() == []  # unqualified (USE lake)

        def _snaps() -> int:
            return r.execute("SELECT count(*) FROM ducklake_snapshots('lake')").fetchone()[0]

        base = _snaps()
        for _ in range(5):
            c = get_ducklake_read()
            try:
                c.execute('SELECT * FROM lake."main"."remote_tbl"').fetchall()
            finally:
                c.close()
        assert _snaps() == base, "reader plane must not commit catalog snapshots per request"
    finally:
        r.close()
