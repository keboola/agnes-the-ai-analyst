"""DELETE /api/admin/registry/{id} for materialized rows must remove the
materialized parquet file too — otherwise sync_state still has the row,
the manifest still serves it, and `da sync` keeps trying to download
data for a table that no longer has a registry entry. The orchestrator's
rebuild path additionally skips parquets that lack a matching
table_registry row, so a transient race (or operator-deleted parquet)
can't resurrect a master view for a dropped table.

E2E sub-agent finding 2026-05-01: registering a materialized BQ row,
syncing, then DELETEing the registry row left the parquet at
`/data/extracts/bigquery/data/<id>.parquet` and a master view in
`analytics.duckdb` — `/api/sync/manifest` and the master view both still
exposed the table.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import duckdb
import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def bq_instance(monkeypatch):
    fake_cfg = {
        "data_source": {
            "type": "bigquery",
            "bigquery": {"project": "my-test-project", "location": "us"},
        },
    }
    monkeypatch.setattr(
        "app.instance_config.load_instance_config", lambda: fake_cfg, raising=False,
    )
    from app.instance_config import reset_cache
    reset_cache()
    yield fake_cfg
    reset_cache()


@pytest.fixture
def stub_bq_extractor(monkeypatch):
    """Bypass post-register rebuild's BQ traffic so the test stays offline."""
    rebuild_mock = MagicMock(return_value={
        "project_id": "my-test-project",
        "tables_registered": 1, "errors": [], "skipped": False,
    })
    monkeypatch.setattr(
        "connectors.bigquery.extractor.rebuild_from_registry",
        rebuild_mock,
    )
    monkeypatch.setattr(
        "src.orchestrator.SyncOrchestrator",
        lambda *a, **kw: MagicMock(),
    )
    return rebuild_mock


def test_delete_materialized_bq_row_removes_parquet(
    seeded_app, bq_instance, stub_bq_extractor,
):
    """DELETE on a materialized BQ registry row removes the canonical parquet
    file at /data/extracts/bigquery/data/<id>.parquet so the orchestrator's
    next rebuild can't resurrect a master view for the dropped row."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    data_dir = seeded_app["env"]["data_dir"]

    # Seed a materialized row + drop a fake parquet at the canonical path.
    r = c.post(
        "/api/admin/register-table",
        json={
            "name": "drop_me",
            "source_type": "bigquery",
            "query_mode": "materialized",
            "source_query": 'SELECT 1 FROM bq."ds"."t"',
        },
        headers=_auth(token),
    )
    assert r.status_code == 201, r.json()
    table_id = r.json()["id"]

    parquet_path = data_dir / "extracts" / "bigquery" / "data" / f"{table_id}.parquet"
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    parquet_path.write_bytes(b"PAR1\x00fake-parquet-content")
    # Also drop a stale .tmp to verify defensive cleanup.
    tmp_path = parquet_path.parent / f"{table_id}.parquet.tmp"
    tmp_path.write_bytes(b"PAR1\x00partial")

    assert parquet_path.exists()
    assert tmp_path.exists()

    r2 = c.delete(f"/api/admin/registry/{table_id}", headers=_auth(token))
    assert r2.status_code == 204

    assert not parquet_path.exists(), "DELETE should remove the materialized parquet"
    assert not tmp_path.exists(), "DELETE should also clean up stale .tmp file"


def test_delete_materialized_keboola_row_removes_parquet(seeded_app):
    """Same contract for Keboola materialized rows — the canonical path is
    /data/extracts/keboola/data/<id>.parquet."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    data_dir = seeded_app["env"]["data_dir"]

    r = c.post(
        "/api/admin/register-table",
        json={
            "name": "kbc_drop_me",
            "source_type": "keboola",
            "query_mode": "materialized",
            "source_query": "SELECT * FROM kbc.\"in.c-bucket\".\"events\"",
        },
        headers=_auth(token),
    )
    assert r.status_code == 201, r.json()
    table_id = r.json()["id"]

    parquet_path = data_dir / "extracts" / "keboola" / "data" / f"{table_id}.parquet"
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    parquet_path.write_bytes(b"PAR1\x00fake")
    assert parquet_path.exists()

    r2 = c.delete(f"/api/admin/registry/{table_id}", headers=_auth(token))
    assert r2.status_code == 204
    assert not parquet_path.exists()


def test_delete_remote_bq_row_does_not_touch_data_dir(
    seeded_app, bq_instance, stub_bq_extractor,
):
    """DELETE on a remote-mode row (no materialized parquet exists) must not
    fail and must not error out trying to delete a non-existent file."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    r = c.post(
        "/api/admin/register-table",
        json={
            "name": "remote_drop",
            "source_type": "bigquery",
            "bucket": "analytics",
            "source_table": "orders",
            "query_mode": "remote",
        },
        headers=_auth(token),
    )
    assert r.status_code in (200, 202), r.json()
    table_id = r.json()["id"]

    r2 = c.delete(f"/api/admin/registry/{table_id}", headers=_auth(token))
    assert r2.status_code == 204


def test_delete_clears_sync_state_for_materialized_row(seeded_app):
    """DELETE must also clear the sync_state row so the manifest stops
    advertising the dropped table to `da sync`."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    r = c.post(
        "/api/admin/register-table",
        json={
            "name": "manifest_drop",
            "source_type": "keboola",
            "query_mode": "materialized",
            "source_query": "SELECT 1",
        },
        headers=_auth(token),
    )
    assert r.status_code == 201, r.json()
    table_id = r.json()["id"]

    # Seed a sync_state row as if it had been materialized.
    from src.db import get_system_db
    from src.repositories.sync_state import SyncStateRepository
    sys_conn = get_system_db()
    try:
        SyncStateRepository(sys_conn).update_sync(
            table_id="manifest_drop",  # = registry.name
            rows=10, file_size_bytes=1024, hash="abc",
        )
        assert SyncStateRepository(sys_conn).get_table_state("manifest_drop") is not None
    finally:
        sys_conn.close()

    r2 = c.delete(f"/api/admin/registry/{table_id}", headers=_auth(token))
    assert r2.status_code == 204

    sys_conn = get_system_db()
    try:
        st = SyncStateRepository(sys_conn).get_table_state("manifest_drop")
    finally:
        sys_conn.close()
    assert st is None, "sync_state row should be removed by DELETE"


# --- Orchestrator skips orphan parquets without matching registry rows -------


def test_orchestrator_skips_orphan_parquet_in_extracts(e2e_env, monkeypatch):
    """A parquet at /data/extracts/<source>/data/<id>.parquet whose stem
    has no matching `table_registry.name` row must NOT have a master view
    created at rebuild time. Defensive — the DELETE handler removes the
    parquet at unregister time, but a transient race (or manual cleanup
    in flight) shouldn't leave the orchestrator exposing a dropped table.
    """
    from src.db import get_system_db
    from src.orchestrator import SyncOrchestrator
    from src.repositories.table_registry import TableRegistryRepository
    from tests.conftest import create_mock_extract

    extracts_dir = e2e_env["extracts_dir"]

    # Build a normal extract.duckdb with a registered table.
    create_mock_extract(extracts_dir, "bigquery", [
        {"name": "valid_table", "data": [{"id": "1"}], "query_mode": "local"},
    ])
    # Drop an orphan parquet at the connector's data dir without registering it.
    orphan_path = extracts_dir / "bigquery" / "data" / "orphan_test.parquet"
    conn0 = duckdb.connect()
    conn0.execute(
        f"COPY (SELECT 1 AS id) TO '{orphan_path}' (FORMAT PARQUET)"
    )
    conn0.close()
    assert orphan_path.exists()

    # Register only the legitimate row in table_registry.
    sys_conn = get_system_db()
    try:
        TableRegistryRepository(sys_conn).register(
            id="valid_table", name="valid_table",
            source_type="bigquery", query_mode="local",
        )
    finally:
        sys_conn.close()

    orch = SyncOrchestrator(analytics_db_path=e2e_env["analytics_db"])
    orch.rebuild()

    # The orphan parquet must NOT be exposed as a master view.
    analytics = duckdb.connect(e2e_env["analytics_db"], read_only=True)
    try:
        views = {
            r[0] for r in analytics.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_type='VIEW'"
            ).fetchall()
        }
    finally:
        analytics.close()
    assert "valid_table" in views, views
    assert "orphan_test" not in views, (
        "orphan parquet without a registry row should not get a master view"
    )
