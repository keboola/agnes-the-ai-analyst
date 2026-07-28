"""Backend-split regression tests for the connector extractors.

Three extractors read app-state (``table_registry`` / ``sync_state`` /
``mcp_sources`` / ``tool_registry``) directly off a DuckDB connection or via
``get_system_db()`` instead of the backend-aware ``src.repositories``
factory — on a Postgres-backed instance those reads silently see an empty
DuckDB shard instead of the real Postgres rows.

Each test below seeds the *Postgres* side of a repo pair, points a
DuckDB-typed connection/handle at something else (or nothing at all), flips
``AGNES_DB_URL`` so ``use_pg()`` is True, and asserts the extractor still
finds the Postgres-seeded row. Pre-fix, each of these would fail because the
extractor read straight through the DuckDB handle.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _migrate_pg(pg_engine, monkeypatch):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    return db_pg.get_engine()


# ---------------------------------------------------------------------------
# connectors/bigquery/extractor.py::rebuild_from_registry
# ---------------------------------------------------------------------------


def test_bigquery_rebuild_from_registry_reads_pg_table_registry(pg_engine, monkeypatch, e2e_env):
    from unittest.mock import MagicMock

    engine = _migrate_pg(pg_engine, monkeypatch)

    from src.repositories.table_registry_pg import TableRegistryPgRepository

    TableRegistryPgRepository(engine).register(
        id="orders",
        name="orders",
        source_type="bigquery",
        bucket="analytics",
        source_table="orders",
        query_mode="remote",
        profile_after_sync=False,
    )

    from connectors.bigquery import extractor as bq

    monkeypatch.setattr(bq, "_resolve_bq_project_id", lambda: "proj")
    fake_init = MagicMock(return_value={"tables_registered": 1, "errors": []})
    monkeypatch.setattr(bq, "init_extract", fake_init)

    result = bq.rebuild_from_registry()

    assert result["skipped"] is False
    fake_init.assert_called_once()
    args, _kwargs = fake_init.call_args
    names = [t["name"] for t in args[2]]
    assert "orders" in names


def test_bigquery_rebuild_from_registry_ignores_stale_duckdb_conn_on_pg(pg_engine, monkeypatch, e2e_env):
    """Even when a caller (e.g. app/api/admin.py) passes a DuckDB conn, PG
    must win once the active backend is Postgres."""
    from unittest.mock import MagicMock

    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb

    engine = _migrate_pg(pg_engine, monkeypatch)

    from src.repositories.table_registry_pg import TableRegistryPgRepository

    TableRegistryPgRepository(engine).register(
        id="orders",
        name="orders",
        source_type="bigquery",
        bucket="analytics",
        source_table="orders",
        query_mode="remote",
        profile_after_sync=False,
    )

    # A DuckDB connection with the schema, but no BQ row registered on it —
    # simulates the always-DuckDB conn a Postgres-backed request handler
    # would still hand over.
    stray_conn = _open_duckdb(str(Path(e2e_env["data_dir"]) / "stray.duckdb"))
    _ensure_schema(stray_conn)

    from connectors.bigquery import extractor as bq

    monkeypatch.setattr(bq, "_resolve_bq_project_id", lambda: "proj")
    fake_init = MagicMock(return_value={"tables_registered": 1, "errors": []})
    monkeypatch.setattr(bq, "init_extract", fake_init)

    result = bq.rebuild_from_registry(conn=stray_conn)
    stray_conn.close()

    assert result["skipped"] is False
    fake_init.assert_called_once()
    args, _kwargs = fake_init.call_args
    names = [t["name"] for t in args[2]]
    assert "orders" in names


# ---------------------------------------------------------------------------
# connectors/keboola/extractor.py::_registered_keboola_tables
# ---------------------------------------------------------------------------


def test_keboola_registered_tables_reads_pg_table_registry(pg_engine, monkeypatch, e2e_env):
    engine = _migrate_pg(pg_engine, monkeypatch)

    from src.repositories.table_registry_pg import TableRegistryPgRepository

    TableRegistryPgRepository(engine).register(
        id="customers",
        name="customers",
        source_type="keboola",
        bucket="in.c-main",
        source_table="customers",
        query_mode="local",
        profile_after_sync=False,
    )

    from connectors.keboola.extractor import _registered_keboola_tables

    tables = _registered_keboola_tables()
    ids = [t["id"] for t in tables]
    assert "customers" in ids


# ---------------------------------------------------------------------------
# connectors/keboola/extractor.py::_read_last_sync_for_tc
# ---------------------------------------------------------------------------


def test_keboola_read_last_sync_reads_pg_sync_state(pg_engine, monkeypatch, e2e_env):
    engine = _migrate_pg(pg_engine, monkeypatch)

    from src.repositories.sync_state_pg import SyncStatePgRepository

    SyncStatePgRepository(engine).update_sync(
        table_id="customers",
        rows=10,
        file_size_bytes=1000,
        hash="abc123",
    )

    from connectors.keboola.extractor import _read_last_sync_for_tc

    last_sync = _read_last_sync_for_tc({"id": "customers"})
    assert last_sync is not None


# ---------------------------------------------------------------------------
# connectors/mcp/extractor.py::extract_source
# ---------------------------------------------------------------------------


def test_mcp_extract_source_ignores_stray_duckdb_conn_on_pg(pg_engine, monkeypatch, e2e_env):
    """``extract_source`` takes a caller-supplied ``system_conn``; on a
    Postgres-backed instance it must resolve the source/tools from Postgres
    regardless of what (empty) DuckDB connection the caller passed in."""
    engine = _migrate_pg(pg_engine, monkeypatch)

    from src.repositories.mcp_sources_pg import MCPSourcePgRepository
    from src.repositories.tool_registry_pg import ToolRegistryPgRepository

    MCPSourcePgRepository(engine).upsert(
        id="src_crm",
        name="crm",
        transport="http",
        url="https://example.com/mcp",
    )
    ToolRegistryPgRepository(engine).upsert(
        tool_id="tool_accounts",
        source_id="src_crm",
        original_name="list_accounts",
        exposed_name="accounts",
        mode="materialize",
        schedule="0 * * * *",
    )

    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb

    # Empty DuckDB conn — no mcp_sources / tool_registry rows on it.
    stray_conn = _open_duckdb(str(Path(e2e_env["data_dir"]) / "stray.duckdb"))
    _ensure_schema(stray_conn)

    from connectors.mcp import extractor as mcp_extractor

    def _fake_materialize(*, source, tool, output_path):
        import pandas as pd

        parquet_path = output_path / "data" / f"{tool['exposed_name']}.parquet"
        parquet_path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([{"id": "1"}, {"id": "2"}, {"id": "3"}]).to_parquet(parquet_path, index=False)
        return (3, 42)

    monkeypatch.setattr(mcp_extractor, "_materialize_one_tool", _fake_materialize)

    result = mcp_extractor.extract_source(
        system_conn=stray_conn,
        source_id="src_crm",
        output_root=Path(e2e_env["data_dir"]) / "extracts" / "crm",
    )
    stray_conn.close()

    assert result["source_name"] == "crm"
    assert result["tables"] == [{"table": "accounts", "rows": 3, "size_bytes": 42}]
    assert result["errors"] == []
