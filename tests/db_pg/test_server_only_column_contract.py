"""Cross-engine contract test for the v74 ``table_registry.server_only``
distribution flag (#607).

Asserts the column exists and defaults to ``false`` on BOTH backends:
  - DuckDB: built by ``_ensure_schema`` (fresh-install DDL + v73→v74 ladder).
  - Postgres: built by ``alembic upgrade head`` (migration 0021).

Both repos round-trip the flag through ``register()`` → ``get()`` with
identical observable behavior.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.table_registry import TableRegistryRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return TableRegistryRepository(conn), conn


def _make_pg_repo(pg_engine):
    from alembic import command
    from alembic.config import Config
    from src.repositories.table_registry_pg import TableRegistryPgRepository

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")
    return TableRegistryPgRepository(pg_engine)


def test_server_only_column_exists_and_defaults_false_duckdb(tmp_path):
    repo, conn = _make_duckdb_repo(tmp_path)
    try:
        cols = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'table_registry'"
            ).fetchall()
        }
        assert "server_only" in cols, "DuckDB table_registry missing server_only"

        # A row registered without server_only defaults to false.
        repo.register(id="t_default", name="t_default", source_type="keboola")
        row = repo.get("t_default")
        assert row is not None
        assert bool(row["server_only"]) is False

        # Explicit server_only=true round-trips.
        repo.register(
            id="t_so", name="t_so", source_type="keboola",
            query_mode="local", server_only=True,
        )
        assert bool(repo.get("t_so")["server_only"]) is True
    finally:
        conn.close()


def test_server_only_column_exists_and_defaults_false_pg(pg_engine):
    repo = _make_pg_repo(pg_engine)

    import sqlalchemy as sa
    inspector = sa.inspect(pg_engine)
    cols = {c["name"] for c in inspector.get_columns("table_registry")}
    assert "server_only" in cols, "Postgres table_registry missing server_only"

    repo.register(id="t_default", name="t_default", source_type="keboola")
    row = repo.get("t_default")
    assert row is not None
    assert bool(row["server_only"]) is False

    repo.register(
        id="t_so", name="t_so", source_type="keboola",
        query_mode="local", server_only=True,
    )
    assert bool(repo.get("t_so")["server_only"]) is True
