"""Tests for the ``ducklake-maintenance`` job kind (wave-2G Task 5).

Covers:

- Registration: ``ducklake-maintenance`` is registered in the ``LIGHT``
  lane.
- Real DuckLake writer session (file catalog — no Postgres needed): builds
  a small lake, generates dead snapshots/files via insert+delete churn,
  runs the REAL handler (nothing mocked), and asserts the snapshot count
  actually dropped — proving ``merge_adjacent_files`` ->
  ``ducklake_expire_snapshots`` -> ``ducklake_cleanup_old_files`` ran for
  real against the real ``ducklake`` extension. A companion test proves
  the configured retention window is honored (a long retention leaves
  everything untouched).
- Call-order + SQL-shape assertion via a lightweight spy cursor (compact ->
  expire(with the configured retention interpolated) -> cleanup -> VACUUM),
  complementing the end-to-end test above with a direct check of exactly
  what gets sent to DuckDB.
- Legacy backend: the handler no-ops — asserts ``get_ducklake_write`` is
  never called at all.
- Scheduler row: ``ducklake-maintenance`` appears in ``build_jobs()`` with
  the documented kind/idempotency_key/target.

Skips loudly (never fakes success) if the ``ducklake`` DuckDB extension
can't be installed here — same pattern as ``tests/test_ducklake_session.py``.
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
        "Skipping real DuckLake maintenance tests rather than faking "
        "success — see src/ducklake_session.py::ducklake_available()."
    ),
)


@pytest.fixture(autouse=True)
def clean_job_kinds_registry():
    """The registry is a process-wide module dict — isolate each test."""
    from app.worker.registry import JOB_KINDS

    JOB_KINDS.clear()
    yield
    JOB_KINDS.clear()


@pytest.fixture
def ducklake_env(monkeypatch, tmp_path):
    """Fresh DATA_DIR + file-catalog DuckLake, backend forced to
    ``ducklake``, clean singleton state before AND after (mirrors
    ``tests/test_ducklake_session.py``'s ``_reset_ducklake_singletons``)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for var in (
        "AGNES_DUCKLAKE_CATALOG_DSN",
        "AGNES_DUCKLAKE_DATA_PATH",
        "AGNES_DUCKLAKE_SNAPSHOT_RETENTION_DAYS",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGNES_ANALYTICS_BACKEND", "ducklake")

    import src.analytics_backend as ab
    import src.ducklake_session as ds

    ab.reset_analytics_backend_cache()
    ds.close_ducklake_sessions()
    yield
    ds.close_ducklake_sessions()
    ab.reset_analytics_backend_cache()


def _snapshot_count(conn) -> int:
    return conn.execute("SELECT count(*) FROM ducklake_snapshots('lake')").fetchone()[0]


class TestRegistration:
    def test_registered_in_light_lane(self):
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS, LIGHT_LANE

        register_all_kinds()

        assert "ducklake-maintenance" in JOB_KINDS
        assert JOB_KINDS["ducklake-maintenance"].lane == LIGHT_LANE

    def test_registered_alongside_the_other_five_kinds(self):
        """Regression guard for the wave-2B `register_all_kinds` test
        (`tests/test_worker_kinds.py`), which asserts an exact 5-name set —
        this proves the sixth kind coexists without disturbing the other
        five."""
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        register_all_kinds()

        assert set(JOB_KINDS) == {
            "data-refresh",
            "marketplaces-sync",
            "session-collector",
            "corporate-memory",
            "jira-refresh",
            "ducklake-maintenance",
        }


class TestLegacyBackendNoOp:
    def test_handler_never_touches_ducklake_on_legacy_backend(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.delenv("AGNES_ANALYTICS_BACKEND", raising=False)

        import src.analytics_backend as ab

        ab.reset_analytics_backend_cache()
        try:
            assert ab.analytics_backend() == "legacy"  # sanity: the case under test

            from app.worker.kinds import register_all_kinds
            from app.worker.registry import JOB_KINDS

            register_all_kinds()

            def _explode():
                raise AssertionError("get_ducklake_write() must not be called on the legacy backend")

            monkeypatch.setattr("src.ducklake_session.get_ducklake_write", _explode)
            monkeypatch.setattr("src.ducklake_session.vacuum_ducklake_catalog", _explode)

            JOB_KINDS["ducklake-maintenance"].handler({})  # must not raise, must be a pure no-op
        finally:
            ab.reset_analytics_backend_cache()


class TestRealMaintenanceSequence:
    """Real ``ducklake`` extension, nothing mocked — exercises the actual
    handler end to end."""

    def test_merge_expire_cleanup_reduces_snapshot_count(self, ducklake_env, monkeypatch):
        """Insert/delete churn leaves several stale snapshots behind.
        Retention forced to 0 (no grace window) so expiry is deterministic
        regardless of wall-clock timing — the real production default (7
        days) would correctly NOT expire anything created seconds ago in a
        test, so this override is what makes the assertion meaningful
        rather than flaky/vacuous."""
        monkeypatch.setenv("AGNES_DUCKLAKE_SNAPSHOT_RETENTION_DAYS", "0")

        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS
        from src.ducklake_session import get_ducklake_write

        register_all_kinds()

        w = get_ducklake_write()
        w.execute("CREATE SCHEMA IF NOT EXISTS lake.src1")
        w.execute("CREATE OR REPLACE TABLE lake.src1.t1 AS SELECT range AS id FROM range(50)")
        for i in range(5):
            w.execute(f"INSERT INTO lake.src1.t1 SELECT range + {(i + 1) * 100} FROM range(20)")
            w.execute(f"DELETE FROM lake.src1.t1 WHERE id = {i}")
        snapshots_before = _snapshot_count(w)
        assert snapshots_before > 1, "fixture churn should leave multiple snapshots behind"
        w.close()

        JOB_KINDS["ducklake-maintenance"].handler({})

        w2 = get_ducklake_write()
        snapshots_after = _snapshot_count(w2)
        # The table's data must survive the maintenance pass untouched —
        # merge/expire/cleanup compact history and reclaim dead files, they
        # must never lose live rows.
        row_count = w2.execute("SELECT count(*) FROM lake.src1.t1").fetchone()[0]
        w2.close()

        assert snapshots_after < snapshots_before
        assert row_count == 50 + 5 * 20 - 5  # initial + inserts - the 5 deleted ids

    def test_snapshot_retention_window_is_honored(self, ducklake_env, monkeypatch):
        """A long retention window (3650 days) must NOT expire any
        snapshot — proves the configured value actually reaches
        ``ducklake_expire_snapshots``'s ``older_than`` argument rather than
        a hardcoded short window. Minimal churn (create + one insert, no
        deletes) so ``merge_adjacent_files`` has nothing to compact and
        cannot itself add a snapshot — isolating the assertion to the
        retention behavior alone."""
        monkeypatch.setenv("AGNES_DUCKLAKE_SNAPSHOT_RETENTION_DAYS", "3650")

        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS
        from src.ducklake_session import get_ducklake_write

        register_all_kinds()

        w = get_ducklake_write()
        w.execute("CREATE SCHEMA IF NOT EXISTS lake.src1")
        w.execute("CREATE OR REPLACE TABLE lake.src1.t1 AS SELECT range AS id FROM range(10)")
        w.execute("INSERT INTO lake.src1.t1 SELECT range + 100 FROM range(5)")
        snapshots_before = _snapshot_count(w)
        w.close()

        JOB_KINDS["ducklake-maintenance"].handler({})

        w2 = get_ducklake_write()
        snapshots_after = _snapshot_count(w2)
        w2.close()

        assert snapshots_after == snapshots_before

    def test_default_retention_is_seven_days_when_unset(self, ducklake_env):
        """No override set at all — the handler must use the documented
        7-day default (not raise, not expire anything created seconds
        ago)."""
        from src.analytics_backend import ducklake_snapshot_retention_days

        assert ducklake_snapshot_retention_days() == 7

        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS
        from src.ducklake_session import get_ducklake_write

        register_all_kinds()

        w = get_ducklake_write()
        w.execute("CREATE SCHEMA IF NOT EXISTS lake.src1")
        w.execute("CREATE OR REPLACE TABLE lake.src1.t1 AS SELECT 1 AS x")
        snapshots_before = _snapshot_count(w)
        w.close()

        JOB_KINDS["ducklake-maintenance"].handler({})  # must not raise

        w2 = get_ducklake_write()
        snapshots_after = _snapshot_count(w2)
        w2.close()

        assert snapshots_after == snapshots_before

    def test_file_catalog_vacuum_is_skipped_not_erroring(self, ducklake_env):
        """No Postgres catalog here — ``vacuum_ducklake_catalog()`` must
        return False (skip) rather than raise, and the handler overall
        must still complete cleanly."""
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS
        from src.ducklake_session import get_ducklake_write, vacuum_ducklake_catalog

        register_all_kinds()

        w = get_ducklake_write()
        w.execute("CREATE SCHEMA IF NOT EXISTS lake.src1")
        w.execute("CREATE OR REPLACE TABLE lake.src1.t1 AS SELECT 1 AS x")
        w.close()

        assert vacuum_ducklake_catalog() is False

        JOB_KINDS["ducklake-maintenance"].handler({})  # must not raise


class TestCallOrderAndSql:
    """Spy-cursor check of the exact CALL sequence/shape, complementing the
    real end-to-end test above with a direct assertion of what gets sent to
    DuckDB — including that the configured retention value is interpolated
    into the ``INTERVAL`` literal."""

    def test_call_order_and_retention_interpolation(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("AGNES_ANALYTICS_BACKEND", "ducklake")
        monkeypatch.setenv("AGNES_DUCKLAKE_SNAPSHOT_RETENTION_DAYS", "14")

        import src.analytics_backend as ab

        ab.reset_analytics_backend_cache()
        try:
            from app.worker.kinds import register_all_kinds
            from app.worker.registry import JOB_KINDS

            register_all_kinds()

            executed: list[str] = []

            class _SpyCursor:
                def execute(self, sql, *a, **kw):
                    executed.append(sql)
                    return self

                def close(self):
                    pass

            vacuum_calls = []
            monkeypatch.setattr("src.ducklake_session.get_ducklake_write", lambda: _SpyCursor())
            monkeypatch.setattr(
                "src.ducklake_session.vacuum_ducklake_catalog",
                lambda: vacuum_calls.append(1) or True,
            )

            JOB_KINDS["ducklake-maintenance"].handler({})

            assert len(executed) == 3
            assert executed[0] == "CALL lake.merge_adjacent_files()"
            assert executed[1] == ("CALL ducklake_expire_snapshots('lake', older_than => now() - INTERVAL '14 days')")
            assert executed[2] == "CALL ducklake_cleanup_old_files('lake', cleanup_all => true)"
            assert vacuum_calls == [1]  # VACUUM ran exactly once, after cleanup
        finally:
            ab.reset_analytics_backend_cache()


class TestSchedulerRow:
    def test_ducklake_maintenance_row_present_with_documented_shape(self):
        from services.scheduler.__main__ import build_jobs

        target = next(j for j in build_jobs() if j[0] == "ducklake-maintenance")
        assert len(target) == 6, "must be a 6-tuple (with json_body), like the other enqueue-migrated rows"
        _, _schedule, endpoint, method, _timeout, json_body = target
        assert endpoint == "/api/jobs"
        assert method == "POST"
        assert json_body == {"kind": "ducklake-maintenance", "idempotency_key": "ducklake-maintenance"}
