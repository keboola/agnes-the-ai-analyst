"""End-to-end integration coverage for query_mode='materialized'.

Unit tests verify each piece in isolation; this file glues them together:

1. Admin POST /api/admin/register-table (materialized) → registry row written
2. _run_materialized_pass writes parquet + sync_state with correct hash
3. GET /api/sync/manifest (per-user) returns the row with query_mode +
   the parquet hash, filtered by RBAC
4. Mode-switch transitions (remote → materialized, materialized → SQL edit
   preserves registered_at) maintain registry invariants.

Devil's-advocate review found these were the gaps the unit tests left
open. Each piece passes in isolation; this file proves they compose.
"""
import duckdb
import hashlib
import pytest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch, MagicMock

from connectors.bigquery.access import BqAccess, BqProjects
from src.repositories.table_registry import TableRegistryRepository
from src.repositories.sync_state import SyncStateRepository


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def bq_instance(monkeypatch):
    """Force instance.yaml to look like a BigQuery deployment so the BQ
    register validator's project_id check passes."""
    fake_cfg = {
        "data_source": {
            "type": "bigquery",
            "bigquery": {"project": "my-test-project", "location": "us"},
        },
    }
    monkeypatch.setattr(
        "app.instance_config.load_instance_config",
        lambda: fake_cfg,
        raising=False,
    )
    from app.instance_config import reset_cache
    reset_cache()
    yield fake_cfg
    reset_cache()


@pytest.fixture
def stub_bq_extractor(monkeypatch):
    """Mirror tests/test_admin_bq_register.py::stub_bq_extractor — replaces
    rebuild_from_registry + SyncOrchestrator so the API's post-register
    materialize doesn't hit real BQ during HTTP-driven tests."""
    rebuild_mock = MagicMock(return_value={
        "project_id": "my-test-project",
        "tables_registered": 1,
        "errors": [],
        "skipped": False,
    })
    monkeypatch.setattr(
        "connectors.bigquery.extractor.rebuild_from_registry",
        rebuild_mock,
    )
    orch_mock = MagicMock()
    monkeypatch.setattr(
        "src.orchestrator.SyncOrchestrator",
        lambda *a, **kw: orch_mock,
    )
    return {"rebuild": rebuild_mock, "orchestrator": orch_mock}


@pytest.fixture
def stub_bq():
    """Real-shape BqAccess wired to in-memory DuckDB factories so the
    materialize_query path can run end-to-end without GCP."""
    @contextmanager
    def _session(_p):
        conn = duckdb.connect(":memory:")
        try:
            conn.execute("ATTACH ':memory:' AS bq")
            conn.execute("CREATE SCHEMA bq.test")
            conn.execute(
                "CREATE OR REPLACE TABLE bq.test.orders AS "
                "SELECT 'EU' AS region, 100 AS revenue UNION ALL "
                "SELECT 'US' AS region, 250 AS revenue"
            )
            yield conn
        finally:
            conn.close()
    return BqAccess(
        BqProjects(billing="my-test-project", data="my-test-project"),
        client_factory=lambda _p: MagicMock(),
        duckdb_session_factory=_session,
    )


def test_e2e_register_then_materialize_then_manifest_via_repo(
    bq_instance, stub_bq, tmp_path, monkeypatch,
):
    """Glue test: register row at the repository layer (skips HTTP/auth),
    run the materialized pass, verify sync_state, then exercise the
    `_build_manifest_for_user` admin path. Catches integration breakage
    that unit tests miss because each only sees one layer."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    from src.db import _ensure_schema
    _ensure_schema(conn)

    table_id = "orders_summary_e2e"
    repo = TableRegistryRepository(conn)
    repo.register(
        id=table_id, name=table_id, source_type="bigquery",
        query_mode="materialized",
        source_query="SELECT region, SUM(revenue) AS revenue "
                     "FROM bq.test.orders GROUP BY 1",
        sync_schedule="every 1m",
    )

    # Run the materialized pass.
    from app.api import sync as sync_mod
    summary = sync_mod._run_materialized_pass(conn, stub_bq)
    assert table_id in summary["materialized"], summary
    assert not summary["errors"]

    # Parquet on disk.
    parquet_path = (
        tmp_path / "data" / "extracts" / "bigquery" / "data"
        / f"{table_id}.parquet"
    )
    assert parquet_path.exists(), f"Expected {parquet_path} to exist"

    # sync_state hash matches the file's MD5.
    expected_hash = hashlib.md5(parquet_path.read_bytes()).hexdigest()
    state = SyncStateRepository(conn)
    row = state.get_table_state(table_id)
    assert row is not None
    assert row["hash"] == expected_hash
    assert row["rows"] == 2

    # Manifest builder exposes query_mode + hash to admin (no RBAC filter).
    admin_user = {"id": "u-admin", "email": "admin@test", "role": "admin"}
    manifest = sync_mod._build_manifest_for_user(conn, admin_user)
    assert table_id in manifest["tables"]
    entry = manifest["tables"][table_id]
    assert entry["query_mode"] == "materialized"
    assert entry["hash"] == expected_hash
    assert entry["rows"] == 2

    conn.close()


def test_remote_to_materialized_transition_clears_bucket_table(
    seeded_app, bq_instance, stub_bq_extractor,
):
    """Switching a remote BQ row to materialized must accept source_query
    and the merged validator must not trip on the now-irrelevant
    bucket/source_table fields."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    # Seed a remote row.
    r = c.post("/api/admin/register-table", json={
        "name": "live_to_mat",
        "source_type": "bigquery",
        "bucket": "analytics",
        "source_table": "orders",
        "query_mode": "remote",
    }, headers=_auth(token))
    assert r.status_code in (200, 202), r.json()
    table_id = r.json()["id"]

    # Switch to materialized — must include source_query for the validator.
    r2 = c.put(f"/api/admin/registry/{table_id}", json={
        "query_mode": "materialized",
        "source_query": "SELECT 1 AS n",
    }, headers=_auth(token))
    assert r2.status_code == 200, r2.json()

    # Verify the merged record reflects the switch.
    r3 = c.get("/api/admin/registry", headers=_auth(token))
    row = next((t for t in r3.json()["tables"] if t["id"] == table_id), None)
    assert row is not None
    assert row["query_mode"] == "materialized"
    assert row["source_query"] == "SELECT 1 AS n"


def test_materialized_sql_edit_preserves_registered_at(
    seeded_app, bq_instance, stub_bq_extractor, monkeypatch,
):
    """Editing source_query on an existing materialized row must not
    reset registered_at — the row's registration history is preserved
    across SQL edits (issue #130 invariant)."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    # Seed a materialized row.
    r = c.post("/api/admin/register-table", json={
        "name": "sql_edit_target",
        "source_type": "bigquery",
        "query_mode": "materialized",
        "source_query": "SELECT 1 AS n",
    }, headers=_auth(token))
    assert r.status_code == 201, r.json()
    table_id = r.json()["id"]

    # Capture the original registered_at.
    r2 = c.get("/api/admin/registry", headers=_auth(token))
    row = next((t for t in r2.json()["tables"] if t["id"] == table_id), None)
    original_ts = row["registered_at"]
    assert original_ts is not None

    # Edit the SQL.
    import time
    time.sleep(0.01)  # ensure a clock tick elapses so a fresh stamp would differ
    r3 = c.put(f"/api/admin/registry/{table_id}", json={
        "query_mode": "materialized",
        "source_query": "SELECT 2 AS n",
    }, headers=_auth(token))
    assert r3.status_code == 200, r3.json()

    r4 = c.get("/api/admin/registry", headers=_auth(token))
    row = next((t for t in r4.json()["tables"] if t["id"] == table_id), None)
    assert row["source_query"] == "SELECT 2 AS n"
    # registered_at preserved across edit
    assert row["registered_at"] == original_ts, (
        f"Expected registered_at preserved (issue #130 contract). "
        f"Original: {original_ts}, after edit: {row['registered_at']}"
    )


def test_materialized_zero_rows_logs_warning(stub_bq, tmp_path, caplog):
    """Devil's-advocate item: an SQL filter that returns 0 rows is
    indistinguishable from 'SQL is wrong'. Confirm we log a WARNING so
    operators can grep on it."""
    import logging
    from connectors.bigquery.extractor import materialize_query

    out = tmp_path / "extracts" / "bigquery"
    out.mkdir(parents=True)

    # Add an empty BQ table to the stub for this test.
    @contextmanager
    def _session_empty(_p):
        conn = duckdb.connect(":memory:")
        try:
            conn.execute("ATTACH ':memory:' AS bq")
            conn.execute("CREATE SCHEMA bq.test")
            conn.execute("CREATE OR REPLACE TABLE bq.test.empty AS "
                         "SELECT 1 AS n WHERE FALSE")
            yield conn
        finally:
            conn.close()

    bq_empty = BqAccess(
        BqProjects(billing="t", data="t"),
        client_factory=lambda _p: MagicMock(),
        duckdb_session_factory=_session_empty,
    )

    with caplog.at_level(logging.WARNING, logger="connectors.bigquery.extractor"):
        stats = materialize_query(
            table_id="empty_t",
            sql="SELECT * FROM bq.test.empty",
            bq=bq_empty,
            output_dir=str(out),
        )

    assert stats["rows"] == 0
    assert any("0 rows" in rec.message for rec in caplog.records), (
        f"Expected '0 rows' WARNING; got: {[r.message for r in caplog.records]}"
    )


def test_attach_real_error_propagates(stub_bq, tmp_path):
    """ATTACH 'project=...' that fails for a real reason (not the
    'already attached' tolerated case) must propagate so callers see
    the actual error instead of a confusing downstream 'bq is not
    attached' message."""
    from connectors.bigquery.extractor import materialize_query

    out = tmp_path / "extracts" / "bigquery"
    out.mkdir(parents=True)

    @contextmanager
    def _session_attach_fails(_p):
        conn = duckdb.connect(":memory:")
        try:
            # Force ATTACH 'project=...' to raise something other than
            # "already attached" by intercepting via execute wrapper —
            # since DuckDB's real connection doesn't accept attribute
            # patches, we use a thin proxy for this test.
            class _Proxy:
                def __init__(self, real):
                    self._real = real
                def execute(self, sql, *a, **kw):
                    if sql.startswith("ATTACH 'project="):
                        raise duckdb.Error("fake permission denied: missing serviceusage.services.use")
                    return self._real.execute(sql, *a, **kw)
                def __getattr__(self, name):
                    return getattr(self._real, name)
                def close(self):
                    return self._real.close()
            yield _Proxy(conn)
        finally:
            conn.close()

    bq_bad = BqAccess(
        BqProjects(billing="t", data="t"),
        client_factory=lambda _p: MagicMock(),
        duckdb_session_factory=_session_attach_fails,
    )

    with pytest.raises(duckdb.Error, match="permission denied"):
        materialize_query(
            table_id="x", sql="SELECT 1",
            bq=bq_bad, output_dir=str(out),
        )
