"""``ducklake-maintenance`` job kind — Postgres-catalog contract (wave-2G
Task 5).

Complements ``tests/test_ducklake_maintenance.py`` (DuckDB-file catalog,
where the catalog-VACUUM step is a documented no-op) with the
Postgres-specific fourth step: a real ``VACUUM`` issued against the live
pgserver-backed catalog database, using the repo's existing
``pg_engine`` fixture (same pattern as
``tests/db_pg/test_ducklake_pg_catalog.py``).

Same loud-skip contract as that file: if the ``ducklake``/``postgres``
DuckDB extensions can't be installed here, every test in this module is
skipped with an explicit reason rather than faking success.
"""

from __future__ import annotations

import pytest


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
        "Skipping real DuckLake-maintenance-over-Postgres tests rather than "
        "faking success — see src/ducklake_session.py::ducklake_available()."
    ),
)


@pytest.fixture(autouse=True)
def clean_job_kinds_registry():
    from app.worker.registry import JOB_KINDS

    JOB_KINDS.clear()
    yield
    JOB_KINDS.clear()


@pytest.fixture
def ducklake_maintenance_pg_env(pg_engine, monkeypatch, tmp_path):
    """Point the ducklake catalog + analytics backend at the per-test
    pgserver schema; clean singleton/cache state before AND after — mirrors
    ``tests/db_pg/test_ducklake_pg_catalog.py``'s ``ducklake_pg_env``, plus
    forcing ``analytics.backend=ducklake`` (the handler checks this)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGNES_ANALYTICS_BACKEND", "ducklake")
    monkeypatch.delenv("AGNES_DUCKLAKE_SNAPSHOT_RETENTION_DAYS", raising=False)
    dsn = pg_engine.url.render_as_string(hide_password=False)
    monkeypatch.setenv("AGNES_DUCKLAKE_CATALOG_DSN", dsn)

    import src.analytics_backend as ab
    import src.ducklake_session as ds

    ab.reset_analytics_backend_cache()
    ds.close_ducklake_sessions()
    yield dsn
    ds.close_ducklake_sessions()
    ab.reset_analytics_backend_cache()


def test_vacuum_ducklake_catalog_runs_against_real_pg_catalog(ducklake_maintenance_pg_env):
    from src.ducklake_session import get_ducklake_write, vacuum_ducklake_catalog

    # A live table so VACUUM has something real to visit, not just an
    # empty catalog.
    w = get_ducklake_write()
    w.execute("CREATE SCHEMA IF NOT EXISTS lake.src1")
    w.execute("CREATE OR REPLACE TABLE lake.src1.t1 AS SELECT 1 AS x")
    w.close()

    assert vacuum_ducklake_catalog() is True


def test_full_maintenance_handler_against_pg_catalog(ducklake_maintenance_pg_env, monkeypatch):
    """End-to-end: real writer session against the Postgres catalog,
    real churn, real handler (merge -> expire -> cleanup -> VACUUM),
    asserting the snapshot count actually dropped and the live data
    survived — the Postgres-catalog counterpart of
    ``tests/test_ducklake_maintenance.py``'s file-catalog version.

    The 1-hour retention safety floor (``src.analytics_backend
    ._MIN_RETENTION_FLOOR_SECONDS``, finding 1-retention-floor) would
    otherwise refuse to expire snapshots created seconds ago even with
    retention_days=0 — forced to 0 here so this test keeps proving the
    real merge/expire/cleanup sequence against a live Postgres catalog;
    the floor-clamping behavior itself is covered by
    ``tests/test_ducklake_maintenance.py::TestCallOrderAndSql
    ::test_retention_zero_is_clamped_to_floor_not_zero_days``."""
    monkeypatch.setenv("AGNES_DUCKLAKE_SNAPSHOT_RETENTION_DAYS", "0")
    monkeypatch.setattr("src.analytics_backend._MIN_RETENTION_FLOOR_SECONDS", 0)

    from app.worker.kinds import register_all_kinds
    from app.worker.registry import JOB_KINDS
    from src.ducklake_session import get_ducklake_write

    register_all_kinds()

    w = get_ducklake_write()
    w.execute("CREATE SCHEMA IF NOT EXISTS lake.src1")
    w.execute("CREATE OR REPLACE TABLE lake.src1.t1 AS SELECT range AS id FROM range(30)")
    for i in range(4):
        w.execute(f"INSERT INTO lake.src1.t1 SELECT range + {(i + 1) * 100} FROM range(10)")
        w.execute(f"DELETE FROM lake.src1.t1 WHERE id = {i}")
    snapshots_before = w.execute("SELECT count(*) FROM ducklake_snapshots('lake')").fetchone()[0]
    assert snapshots_before > 1
    w.close()

    JOB_KINDS["ducklake-maintenance"].handler({})  # must not raise

    w2 = get_ducklake_write()
    snapshots_after = w2.execute("SELECT count(*) FROM ducklake_snapshots('lake')").fetchone()[0]
    row_count = w2.execute("SELECT count(*) FROM lake.src1.t1").fetchone()[0]
    w2.close()

    assert snapshots_after < snapshots_before
    assert row_count == 30 + 4 * 10 - 4
