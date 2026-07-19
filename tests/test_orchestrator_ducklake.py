"""SyncOrchestrator's DuckLake copy-ingest rebuild path (wave-2G, task 3).

Exercises the REAL ``ducklake`` DuckDB extension end-to-end — no mocking of
DuckLake itself. If the extension cannot be INSTALL/LOAD'ed in this
environment (offline, or a DuckDB build predating it), every test here is
skipped via ``pytestmark`` with a loud reason, same pattern as
``tests/test_ducklake_session.py``. This file never fakes success.

Fixtures build extracts shaped like the REAL connector contract — actual
parquet files under ``data/`` plus a minimal ``extract.duckdb`` whose
``_meta`` rows point local-mode tables at
``CREATE VIEW ... AS SELECT * FROM read_parquet(...)`` (see
``connectors/keboola/extractor.py::_register_local_meta``) — rather than
the simplified "real table baked directly into extract.duckdb" shape the
pre-existing ``tests/test_orchestrator.py`` fixture uses. That matters here
because the ducklake ingest path's whole job is reading real parquet-backed
extract data and copying it into the DuckLake catalog.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest


def _extension_available() -> bool:
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
        "Skipping real DuckLake orchestrator tests rather than faking "
        "success — see src/ducklake_session.py::ducklake_available()."
    ),
)


def _write_local_table(data_dir: Path, conn: duckdb.DuckDBPyConnection, name: str, rows: list[dict]) -> tuple[int, int]:
    """Write a real parquet file for *name* and register a
    ``read_parquet``-backed view over it in *conn* — mirrors
    ``connectors/keboola/extractor.py``'s local-mode contract exactly.

    Returns (row_count, size_bytes) for the caller's ``_meta`` insert.
    """
    pq_path = data_dir / f"{name}.parquet"
    if rows:
        cols = list(rows[0].keys())
        arrow_table = pa.table({c: [r[c] for r in rows] for c in cols})
    else:
        arrow_table = pa.table({"id": pa.array([], type=pa.string())})
    pq.write_table(arrow_table, pq_path)

    safe = str(pq_path).replace("'", "''")
    conn.execute(f"CREATE OR REPLACE VIEW \"{name}\" AS SELECT * FROM read_parquet('{safe}')")
    return len(rows), pq_path.stat().st_size


def _create_ducklake_extract(extracts_dir: Path, source_name: str, tables: list[dict]) -> None:
    """Build ``extracts_dir/source_name/{extract.duckdb, data/*.parquet}``.

    Each entry in *tables* is ``{"name": ..., "data": [...], "query_mode":
    "local"|"remote" (default "local")}``. Remote-mode entries get a
    ``_meta`` row with no local parquet/inner-object at all — matching the
    real contract (their inner object is a view over an externally-ATTACHed
    extension, irrelevant here since the ducklake ingest path skips remote
    rows before ever trying to read them).

    Recreates the source directory from scratch if it already exists, so a
    test can call this twice for the same source to simulate "new sync
    happened" between two orchestrator calls.
    """
    source_dir = extracts_dir / source_name
    if source_dir.exists():
        import shutil

        shutil.rmtree(source_dir)
    source_dir.mkdir(parents=True)
    data_dir = source_dir / "data"
    data_dir.mkdir()

    db_path = source_dir / "extract.duckdb"
    conn = duckdb.connect(str(db_path))
    try:
        conn.execute(
            """CREATE TABLE _meta (
                table_name VARCHAR, description VARCHAR, rows BIGINT,
                size_bytes BIGINT, extracted_at TIMESTAMP,
                query_mode VARCHAR DEFAULT 'local'
            )"""
        )
        for t in tables:
            name = t["name"]
            rows_data = t.get("data", [])
            query_mode = t.get("query_mode", "local")

            if query_mode == "remote":
                conn.execute(
                    "INSERT INTO _meta VALUES (?, ?, ?, ?, current_timestamp, ?)",
                    [name, t.get("description", ""), len(rows_data), 0, "remote"],
                )
                continue

            row_count, size_bytes = _write_local_table(data_dir, conn, name, rows_data)
            conn.execute(
                "INSERT INTO _meta VALUES (?, ?, ?, ?, current_timestamp, ?)",
                [name, t.get("description", ""), row_count, size_bytes, query_mode],
            )
    finally:
        conn.close()


@pytest.fixture
def ducklake_env(tmp_path, monkeypatch):
    """Fresh DATA_DIR, ``analytics.backend=ducklake`` (DuckDB-file catalog,
    single-process default), and clean module singleton state for every
    test — reset before AND after so no state leaks across tests, mirroring
    ``tests/test_ducklake_session.py``'s fixture.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGNES_ANALYTICS_BACKEND", "ducklake")
    for var in ("AGNES_DUCKLAKE_CATALOG_DSN", "AGNES_DUCKLAKE_DATA_PATH"):
        monkeypatch.delenv(var, raising=False)

    extracts_dir = tmp_path / "extracts"
    extracts_dir.mkdir()

    import src.analytics_backend as ab
    import src.ducklake_session as ds

    ab.reset_analytics_backend_cache()
    ds.close_ducklake_sessions()
    yield {"data_dir": tmp_path, "extracts_dir": extracts_dir}
    ds.close_ducklake_sessions()
    ab.reset_analytics_backend_cache()


class TestDucklakeRebuild:
    def test_rebuild_two_sources_tables_and_master_views_queryable(self, ducklake_env):
        from src.ducklake_session import get_ducklake_read
        from src.orchestrator import SyncOrchestrator

        _create_ducklake_extract(
            ducklake_env["extracts_dir"],
            "src_a",
            [{"name": "orders", "data": [{"id": "1", "total": "100"}]}],
        )
        _create_ducklake_extract(
            ducklake_env["extracts_dir"],
            "src_b",
            [{"name": "issues", "data": [{"key": "PROJ-1"}]}],
        )

        result = SyncOrchestrator().rebuild()

        assert set(result.get("src_a", [])) == {"orders"}
        assert set(result.get("src_b", [])) == {"issues"}

        r = get_ducklake_read()
        try:
            assert r.execute('SELECT total FROM lake."src_a"."orders" WHERE id=\'1\'').fetchone()[0] == "100"
            assert r.execute('SELECT key FROM lake."src_b"."issues"').fetchone()[0] == "PROJ-1"
            # Master views expose the same rows under the unqualified name
            # legacy's server.duckdb master views use, under lake.main.
            assert r.execute('SELECT total FROM lake."main"."orders"').fetchone()[0] == "100"
            assert r.execute('SELECT key FROM lake."main"."issues"').fetchone()[0] == "PROJ-1"
        finally:
            r.close()

    def test_rebuild_source_only_reingests_that_source(self, ducklake_env):
        """Incremental win: rebuild_source('src_a') must NOT touch src_b's
        DuckLake table/snapshot, even though src_b's own extract on disk
        changed in between."""
        from src.ducklake_session import close_ducklake_sessions, get_ducklake_read
        from src.orchestrator import SyncOrchestrator

        extracts_dir = ducklake_env["extracts_dir"]
        _create_ducklake_extract(extracts_dir, "src_a", [{"name": "orders", "data": [{"id": "1"}]}])
        _create_ducklake_extract(extracts_dir, "src_b", [{"name": "issues", "data": [{"key": "PROJ-1"}]}])

        orch = SyncOrchestrator()
        orch.rebuild()

        r = get_ducklake_read()
        baseline_snapshot_id = r.execute("SELECT max(snapshot_id) FROM lake.snapshots()").fetchone()[0]
        r.close()
        # Release the reader's own extract-source ATTACHes (per
        # get_ducklake_read()'s docstring: "newly added extract sources
        # become visible only on the next (re)open") before rewriting
        # extract.duckdb on disk below — otherwise the fresh
        # duckdb.connect() in _create_ducklake_extract collides with the
        # reader's still-live READ_ONLY attach of the same file path.
        close_ducklake_sessions()

        # Simulate new syncs landing on disk for BOTH sources — src_a gets
        # a second row, src_b gets a second row too.
        _create_ducklake_extract(extracts_dir, "src_a", [{"name": "orders", "data": [{"id": "1"}, {"id": "2"}]}])
        _create_ducklake_extract(
            extracts_dir,
            "src_b",
            [{"name": "issues", "data": [{"key": "PROJ-1"}, {"key": "PROJ-2"}]}],
        )

        tables = orch.rebuild_source("src_a")
        assert set(tables) == {"orders"}

        r = get_ducklake_read()
        try:
            # src_a WAS re-ingested — sees the new row.
            orders_rows = r.execute('SELECT id FROM lake."src_a"."orders" ORDER BY id').fetchall()
            assert [row[0] for row in orders_rows] == ["1", "2"]

            # src_b's ducklake table is UNTOUCHED — still the stale
            # one-row snapshot from the full rebuild, even though its
            # on-disk extract now has 2 rows.
            issues_rows = r.execute('SELECT key FROM lake."src_b"."issues"').fetchall()
            assert [row[0] for row in issues_rows] == ["PROJ-1"]

            # No new snapshot touched src_b's schema/table.
            new_snapshots = r.execute(
                "SELECT changes FROM lake.snapshots() WHERE snapshot_id > ?",
                [baseline_snapshot_id],
            ).fetchall()
        finally:
            r.close()

        assert new_snapshots, "rebuild_source('src_a') should have produced at least one new snapshot"
        for (changes,) in new_snapshots:
            for change_list in changes.values():
                if change_list:
                    for entry in change_list:
                        assert "src_b" not in str(entry), (
                            f"rebuild_source('src_a') must not touch src_b's schema, found: {entry}"
                        )

    def test_view_name_collision_honors_ownership(self, ducklake_env):
        """Two sources both publish a table named 'shared'. First-come-
        first-served (alphabetical iteration order): src_a wins, src_b's
        colliding table is refused — same view_ownership semantics as the
        legacy backend (tests/test_view_collision_detection.py)."""
        from src.ducklake_session import get_ducklake_read
        from src.orchestrator import SyncOrchestrator
        from src.repositories import view_ownership_repo

        extracts_dir = ducklake_env["extracts_dir"]
        _create_ducklake_extract(
            extracts_dir,
            "src_a",
            [{"name": "shared", "data": [{"id": "a1"}]}, {"name": "a_only", "data": [{"id": "1"}]}],
        )
        _create_ducklake_extract(
            extracts_dir,
            "src_b",
            [{"name": "shared", "data": [{"id": "b1"}]}, {"name": "b_only", "data": [{"id": "2"}]}],
        )

        result = SyncOrchestrator().rebuild()

        assert "shared" in result["src_a"]
        assert "a_only" in result["src_a"]
        assert "shared" not in result["src_b"], "src_b should NOT have published a colliding 'shared' table"
        assert "b_only" in result["src_b"]

        repo = view_ownership_repo()
        assert repo.get_owner("shared") == "src_a"
        assert repo.get_owner("a_only") == "src_a"
        assert repo.get_owner("b_only") == "src_b"

        r = get_ducklake_read()
        try:
            # src_a's data wins; no lake."src_b"."shared" table was created.
            assert r.execute('SELECT id FROM lake."main"."shared"').fetchone()[0] == "a1"
            with pytest.raises(duckdb.Error):
                r.execute('SELECT * FROM lake."src_b"."shared"').fetchall()
        finally:
            r.close()

    def test_remote_table_skipped_but_claims_ownership(self, ducklake_env):
        """query_mode='remote' rows are not copy-ingested into the lake
        catalog (no local parquet backs them), but still occupy the
        view_ownership namespace so a local table elsewhere can't silently
        steal the name."""
        from src.orchestrator import SyncOrchestrator
        from src.repositories import view_ownership_repo

        extracts_dir = ducklake_env["extracts_dir"]
        _create_ducklake_extract(
            extracts_dir,
            "bigquery",
            [{"name": "web_sessions", "data": [{"x": "1"}], "query_mode": "remote"}],
        )
        _create_ducklake_extract(
            extracts_dir,
            "keboola",
            [{"name": "orders", "data": [{"id": "1"}]}],
        )

        result = SyncOrchestrator().rebuild()

        # Remote table is not part of the ducklake-ingested table list...
        assert "web_sessions" not in result.get("bigquery", [])
        assert "orders" in result.get("keboola", [])

        # ...but it still claimed its name in view_ownership.
        repo = view_ownership_repo()
        assert repo.get_owner("web_sessions") == "bigquery"

    def test_legacy_backend_default_untouched(self, tmp_path, monkeypatch):
        """Zero-config default (no AGNES_ANALYTICS_BACKEND set) must still
        route through the untouched legacy rebuild-and-swap path."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.delenv("AGNES_ANALYTICS_BACKEND", raising=False)
        (tmp_path / "extracts").mkdir()
        (tmp_path / "analytics").mkdir()

        import src.analytics_backend as ab

        ab.reset_analytics_backend_cache()
        try:
            from src.orchestrator import SyncOrchestrator

            db_path = str(tmp_path / "analytics" / "server.duckdb")
            orch = SyncOrchestrator(analytics_db_path=db_path)
            result = orch.rebuild()
            assert result == {}
            # The legacy path's artifact — server.duckdb — must exist;
            # nothing ducklake-related should have been touched.
            assert Path(db_path).exists()
        finally:
            ab.reset_analytics_backend_cache()
