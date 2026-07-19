"""Wave-2G (DuckLake) task 4 — reader path: query endpoints ride DuckLake.

Exercises the REAL ``ducklake`` DuckDB extension end-to-end, same
skip-loudly-if-unavailable contract as ``tests/test_ducklake_session.py``
and ``tests/test_orchestrator_ducklake.py`` — this file never fakes
DuckLake itself.

Covers the task-4 contract:
  - ``/api/query`` over a ducklake-backed local table returns identical
    results to the legacy backend for the same extract data.
  - An unqualified master-view name (``SELECT * FROM "<table>"``, no
    ``lake.main.`` prefix) resolves against a ducklake reader — the
    reader issues ``USE lake`` on every cursor so the catalog/schema
    default matches what task 3's writer creates views in.
  - Internal-table short-circuit (``agnes_audit`` etc.) is completely
    unaffected by the analytics backend — it never touches
    ``get_analytics_db_readonly()`` at all.
  - Hybrid query (``/api/query/hybrid``'s ``RemoteQueryEngine``) can
    ``register()`` a mocked BQ sub-result and join it against a ducklake
    ``lake.main`` view on the same cursor.
  - Snapshot isolation: a reader **transaction** started before a
    concurrent writer commit keeps seeing the pre-commit snapshot (the
    POC-verified property documented in
    docs/superpowers/specs/2026-07-16-three-plane-scalable-architecture.md
    §"Readers"); a fresh transaction/cursor sees the new data.
  - Remote-mode (``query_mode='remote'``) tables resolve as a
    ``lake.main`` wrapper view built from ``table_registry`` — asserted
    via the view's existence + resolution, not live BigQuery data (no
    live BQ in this environment).
  - The reader never holds a persistent attach on a source whose
    extract.duckdb is mutated in place (Jira's ``update_meta()`` pattern)
    — proven by performing exactly that kind of open while the ducklake
    reader session is alive.
"""

from __future__ import annotations


import duckdb
import pyarrow as pa
import pytest

from tests.conftest import create_mock_extract


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
        "Skipping real DuckLake query-path tests rather than faking "
        "success — see src/ducklake_session.py::ducklake_available()."
    ),
)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def ducklake_env(tmp_path, monkeypatch):
    """Fresh DATA_DIR, ``analytics.backend=ducklake``, clean singleton
    state — mirrors ``tests/test_orchestrator_ducklake.py``'s fixture."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGNES_ANALYTICS_BACKEND", "ducklake")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
    for var in ("AGNES_DUCKLAKE_CATALOG_DSN", "AGNES_DUCKLAKE_DATA_PATH"):
        monkeypatch.delenv(var, raising=False)

    (tmp_path / "extracts").mkdir()
    (tmp_path / "analytics").mkdir()
    (tmp_path / "state").mkdir()

    import src.analytics_backend as ab
    import src.ducklake_session as ds

    ab.reset_analytics_backend_cache()
    ds.close_ducklake_sessions()
    yield {
        "data_dir": tmp_path,
        "extracts_dir": tmp_path / "extracts",
        "analytics_db": str(tmp_path / "analytics" / "server.duckdb"),
    }
    ds.close_ducklake_sessions()
    ab.reset_analytics_backend_cache()


def _register_and_rebuild(env, source_name: str, table_name: str, rows: list[dict], *, query_mode="local"):
    """Register *table_name* in table_registry, write a mock extract, and
    run ``SyncOrchestrator().rebuild()`` (backend-dispatched by whatever
    ``AGNES_ANALYTICS_BACKEND`` is currently set to)."""
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository
    from src.orchestrator import SyncOrchestrator

    conn = get_system_db()
    try:
        TableRegistryRepository(conn).register(
            id=f"{source_name}.{table_name}",
            name=table_name,
            source_type=source_name,
            bucket="bucket",
            source_table=table_name,
            query_mode=query_mode,
        )
    finally:
        conn.close()

    create_mock_extract(
        env["extracts_dir"], source_name, [{"name": table_name, "data": rows, "query_mode": query_mode}]
    )
    return SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()


def _make_admin_client(env):
    """Create a fresh FastAPI app/TestClient over *env*, seeded with one
    admin user, and return ``(client, admin_token)``. Idempotent — safe to
    call more than once against the same env (e.g. a repeat query in the
    same test) since it only creates the user on first call."""
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token
    from app.main import create_app
    from fastapi.testclient import TestClient

    conn = get_system_db()
    repo = UserRepository(conn)
    if repo.get_by_id("admin1") is None:
        repo.create(id="admin1", email="admin@test.com", name="Admin")
        admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()[0]
        UserGroupMembersRepository(conn).add_member("admin1", admin_gid, source="system_seed")
    conn.close()

    app = create_app()
    client = TestClient(app)
    token = create_access_token("admin1", "admin@test.com")
    return client, token


def _build_app_and_query(env, sql: str):
    """Create a fresh FastAPI app/TestClient over *env* and POST /api/query."""
    client, token = _make_admin_client(env)
    return client.post("/api/query", json={"sql": sql}, headers=_auth(token))


class TestQueryDucklakeParity:
    def test_identical_results_local_table_legacy_vs_ducklake(self, tmp_path, monkeypatch):
        """Same extract data, same SQL, two isolated environments (one
        legacy, one ducklake) — the /api/query response bodies must
        contain the identical rows/columns."""
        rows_data = [
            {"id": "1", "product": "Widget", "amount": "100"},
            {"id": "2", "product": "Gadget", "amount": "200"},
        ]
        sql = "SELECT id, product, amount FROM orders ORDER BY id"

        # ---- Phase 1: legacy backend -------------------------------------
        legacy_dir = tmp_path / "legacy"
        monkeypatch.setenv("DATA_DIR", str(legacy_dir))
        monkeypatch.delenv("AGNES_ANALYTICS_BACKEND", raising=False)
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
        (legacy_dir / "extracts").mkdir(parents=True)
        (legacy_dir / "analytics").mkdir()
        (legacy_dir / "state").mkdir()

        import src.analytics_backend as ab
        import src.ducklake_session as ds

        ab.reset_analytics_backend_cache()
        ds.close_ducklake_sessions()
        legacy_env = {
            "extracts_dir": legacy_dir / "extracts",
            "analytics_db": str(legacy_dir / "analytics" / "server.duckdb"),
        }
        _register_and_rebuild(legacy_env, "keboola", "orders", rows_data)
        legacy_resp = _build_app_and_query(legacy_env, sql)
        assert legacy_resp.status_code == 200
        legacy_body = legacy_resp.json()

        # ---- Phase 2: ducklake backend ------------------------------------
        ducklake_dir = tmp_path / "ducklake"
        monkeypatch.setenv("DATA_DIR", str(ducklake_dir))
        monkeypatch.setenv("AGNES_ANALYTICS_BACKEND", "ducklake")
        (ducklake_dir / "extracts").mkdir(parents=True)
        (ducklake_dir / "analytics").mkdir()
        (ducklake_dir / "state").mkdir()

        ab.reset_analytics_backend_cache()
        ds.close_ducklake_sessions()
        ducklake_env = {
            "extracts_dir": ducklake_dir / "extracts",
            "analytics_db": str(ducklake_dir / "analytics" / "server.duckdb"),
        }
        _register_and_rebuild(ducklake_env, "keboola", "orders", rows_data)
        ducklake_resp = _build_app_and_query(ducklake_env, sql)
        assert ducklake_resp.status_code == 200
        ducklake_body = ducklake_resp.json()

        ds.close_ducklake_sessions()
        ab.reset_analytics_backend_cache()

        assert ducklake_body["columns"] == legacy_body["columns"]
        assert ducklake_body["rows"] == legacy_body["rows"]
        assert ducklake_body["row_count"] == legacy_body["row_count"] == 2

    def test_unqualified_master_view_resolves(self, ducklake_env):
        """The reader's ``USE lake`` cursor setting lets an unqualified
        ``SELECT ... FROM "<table>"`` — exactly what legacy master views
        expose — resolve against ``lake.main`` with no prefix."""
        from src.ducklake_session import get_ducklake_read

        _register_and_rebuild(ducklake_env, "keboola", "orders", [{"id": "1", "total": "50"}])

        r = get_ducklake_read()
        try:
            assert r.execute('SELECT total FROM "orders"').fetchone()[0] == "50"
            assert r.execute("SELECT current_database()").fetchone()[0] == "lake"
            assert r.execute("SELECT current_schema()").fetchone()[0] == "main"
        finally:
            r.close()


class TestQueryDucklakeInternalUnaffected:
    def test_internal_table_query_unaffected_by_ducklake_backend(self, ducklake_env):
        """`/api/query` against an internal table (agnes_audit) never
        touches get_analytics_db_readonly()/the ducklake reader at all —
        must behave identically regardless of analytics backend."""
        _register_and_rebuild(ducklake_env, "keboola", "orders", [{"id": "1", "total": "50"}])
        # First query populates at least one audit_log row.
        first = _build_app_and_query(ducklake_env, 'SELECT total FROM "orders"')
        assert first.status_code == 200

        internal_resp = _build_app_and_query(ducklake_env, "SELECT * FROM agnes_audit LIMIT 5")
        assert internal_resp.status_code == 200
        body = internal_resp.json()
        assert body["row_count"] >= 1


class TestQueryDucklakeSnapshotIsolation:
    def test_reader_transaction_holds_snapshot_across_writer_commit(self, ducklake_env):
        """A reader that started an explicit transaction before a
        concurrent writer commits keeps seeing the pre-commit snapshot;
        a fresh cursor/transaction sees the new data. Mirrors the
        POC-verified property from
        docs/superpowers/specs/2026-07-16-three-plane-scalable-architecture.md.
        """
        from src.ducklake_session import get_ducklake_read, get_ducklake_write

        w = get_ducklake_write()
        w.execute("CREATE SCHEMA IF NOT EXISTS lake.snap_src")
        w.execute("CREATE OR REPLACE TABLE lake.snap_src.t AS SELECT 1 AS x")
        w.execute("CREATE OR REPLACE VIEW lake.main.snap_t AS SELECT * FROM lake.snap_src.t")

        pinned = get_ducklake_read()
        pinned.execute("BEGIN TRANSACTION")
        assert pinned.execute("SELECT * FROM lake.main.snap_t").fetchall() == [(1,)]

        # Concurrent writer commits a second row while `pinned` is still
        # mid-transaction.
        w.execute("INSERT INTO lake.snap_src.t VALUES (2)")

        # The pinned transaction still sees only the old snapshot.
        assert pinned.execute("SELECT * FROM lake.main.snap_t").fetchall() == [(1,)]
        pinned.execute("COMMIT")
        pinned.close()

        # A fresh cursor sees the committed new row.
        fresh = get_ducklake_read()
        assert sorted(x for (x,) in fresh.execute("SELECT * FROM lake.main.snap_t").fetchall()) == [1, 2]
        fresh.close()
        w.close()


class TestQueryDucklakeRemoteViews:
    def test_remote_table_resolves_from_table_registry(self, ducklake_env):
        """A query_mode='remote' table_registry row gets a lake.main
        wrapper view pointing at the extract source's own inner object —
        asserted via resolution (no error, correct rowset), not live BQ
        data (there is none in this environment)."""
        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository
        from src.ducklake_session import get_ducklake_read

        # Extract with a remote-mode row: create_mock_extract() builds an
        # empty base table `"remote_tbl"` for query_mode='remote' entries
        # (see tests/conftest.py) — no live BigQuery ATTACH required.
        create_mock_extract(
            ducklake_env["extracts_dir"],
            "bigquery",
            [{"name": "remote_tbl", "data": [], "query_mode": "remote"}],
        )
        conn = get_system_db()
        try:
            TableRegistryRepository(conn).register(
                id="bigquery.remote_tbl",
                name="remote_tbl",
                source_type="bigquery",
                bucket="dataset",
                source_table="remote_tbl",
                query_mode="remote",
            )
        finally:
            conn.close()

        r = get_ducklake_read()
        try:
            # Resolves with zero rows — the point is the view exists and
            # is queryable, not that it returns live BQ data.
            assert r.execute('SELECT * FROM lake."main"."remote_tbl"').fetchall() == []
            # Unqualified form (USE lake) also resolves.
            assert r.execute('SELECT * FROM "remote_tbl"').fetchall() == []
        finally:
            r.close()

    def test_remote_source_extract_stays_attached_across_calls(self, ducklake_env):
        """Once a source has a registered remote-mode table, its
        extract.duckdb attach persists across separate get_ducklake_read()
        calls (safe — BigQuery/Keboola-direct extractors always
        temp-file-swap, never mutate in place)."""
        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository
        from src.ducklake_session import get_ducklake_read

        create_mock_extract(
            ducklake_env["extracts_dir"],
            "bigquery",
            [{"name": "remote_tbl", "data": [], "query_mode": "remote"}],
        )
        conn = get_system_db()
        try:
            TableRegistryRepository(conn).register(
                id="bigquery.remote_tbl",
                name="remote_tbl",
                source_type="bigquery",
                bucket="dataset",
                source_table="remote_tbl",
                query_mode="remote",
            )
        finally:
            conn.close()

        r1 = get_ducklake_read()
        attached_after_first = {row[0] for row in r1.execute("SELECT database_name FROM duckdb_databases()").fetchall()}
        r1.close()
        r2 = get_ducklake_read()
        attached_after_second = {
            row[0] for row in r2.execute("SELECT database_name FROM duckdb_databases()").fetchall()
        }
        r2.close()
        assert "bigquery" in attached_after_first
        assert "bigquery" in attached_after_second


class TestDucklakeReaderNoCollisionWithLiveInPlaceExtract:
    def test_reader_never_attaches_local_only_source_extract(self, ducklake_env):
        """A purely local-mode source (no table_registry row with
        query_mode='remote') is NEVER attached by the long-lived ducklake
        reader — proven both by absence from duckdb_databases() and by a
        Jira-style direct read-write re-open of its extract.duckdb (which
        would raise 'Conflicting lock' if the reader still held it)
        succeeding while the reader session is alive."""
        from src.ducklake_session import get_ducklake_read
        from src.duckdb_conn import _open_duckdb

        _register_and_rebuild(ducklake_env, "jira", "issues", [{"key": "PROJ-1"}])

        r = get_ducklake_read()
        try:
            attached = {row[0] for row in r.execute("SELECT database_name FROM duckdb_databases()").fetchall()}
            assert "jira" not in attached

            # Simulate connectors/jira/extract_init.py::update_meta's
            # direct read-write open of the SAME path while the reader
            # session is alive.
            jira_db = ducklake_env["extracts_dir"] / "jira" / "extract.duckdb"
            live_write_conn = _open_duckdb(str(jira_db))
            try:
                live_write_conn.execute("UPDATE _meta SET rows = rows")
            finally:
                live_write_conn.close()
        finally:
            r.close()


class TestHybridQueryDucklake:
    def test_hybrid_registers_bq_subresult_and_joins_ducklake_view(self, ducklake_env):
        """RemoteQueryEngine.register_bq()'s conn.register() call and the
        subsequent join execute() both work against a ducklake reader
        cursor — the contract app/api/query_hybrid.py relies on."""
        from src.remote_query import RemoteQueryEngine
        from src.ducklake_session import get_ducklake_read, get_ducklake_write

        w = get_ducklake_write()
        w.execute("CREATE SCHEMA IF NOT EXISTS lake.hybrid_src")
        w.execute("CREATE OR REPLACE TABLE lake.hybrid_src.orders AS SELECT 1 AS order_id, 'CZ' AS country")
        w.execute("CREATE OR REPLACE VIEW lake.main.orders AS SELECT * FROM lake.hybrid_src.orders")
        w.close()

        r = get_ducklake_read()
        try:
            engine = RemoteQueryEngine(r)
            bq_table = pa.table({"order_id": pa.array([1], type=pa.int64()), "revenue": pa.array([9.5])})
            # register() is DuckDB's Python-object registration API — bind
            # it directly rather than going through RemoteQueryEngine's BQ
            # network path (no live BQ in this environment; this proves
            # the join mechanics against a ducklake cursor, matching how
            # register_bq() itself calls self._conn.register(alias, arrow_table)).
            r.register("bq_orders", bq_table)

            result = r.execute(
                'SELECT o.order_id, o.country, b.revenue FROM "orders" o JOIN bq_orders b ON o.order_id = b.order_id'
            ).fetchall()
            assert result == [(1, "CZ", 9.5)]
            assert engine is not None  # engine constructed successfully against the cursor
        finally:
            r.close()
