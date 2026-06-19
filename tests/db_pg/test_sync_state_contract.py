"""Cross-engine contract tests for the sync_state repository.

Targets: sync_state_repo (DuckDB + Postgres). Parametrises over both
backends; identical inputs must produce identical outputs.

Follows the fixture pattern in test_rbac_contract.py: DuckDB via
_ensure_schema, Postgres via alembic upgrade -> head.
"""

from __future__ import annotations

import pytest


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.sync_state import SyncStateRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return SyncStateRepository(conn), conn


def _make_pg_repo(pg_engine, monkeypatch):
    from pathlib import Path
    from alembic import command
    from alembic.config import Config

    REPO_ROOT = Path(__file__).resolve().parents[2]
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    engine = db_pg.get_engine()

    from src.repositories.sync_state_pg import SyncStatePgRepository

    return SyncStatePgRepository(engine), None


@pytest.fixture(params=["duckdb", "pg"])
def sync_repo(request, tmp_path, pg_engine, monkeypatch):
    """Yields ``(repo, raw_conn_or_None, backend)`` for both backends."""
    backend = request.param
    if backend == "duckdb":
        repo, conn = _make_duckdb_repo(tmp_path)
        yield repo, conn, backend
        if conn is not None:
            conn.close()
    else:
        repo, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield repo, None, backend


# ---------------------------------------------------------------------------
# contract tests
# ---------------------------------------------------------------------------


def test_clear_for_table_removes_state_and_history(sync_repo):
    repo, _, _ = sync_repo

    # Seed via the existing update_sync(): writes both sync_state and
    # sync_history in one call.
    repo.update_sync(
        table_id="bucket.orders",
        rows=42,
        file_size_bytes=1024,
        hash="abc123",
        duration_ms=99,
    )

    assert repo.get_table_state("bucket.orders") is not None
    assert repo.get_sync_history("bucket.orders") != []

    removed = repo.clear_for_table("bucket.orders")
    assert removed == 1

    assert repo.get_table_state("bucket.orders") is None
    assert repo.get_sync_history("bucket.orders") == []


def test_clear_for_table_no_rows_returns_zero(sync_repo):
    repo, _, _ = sync_repo
    assert repo.clear_for_table("never.synced") == 0


def test_clear_for_table_only_targets_named_table(sync_repo):
    repo, _, _ = sync_repo

    repo.update_sync(table_id="t.keep", rows=1, file_size_bytes=10, hash="h1")
    repo.update_sync(table_id="t.drop", rows=2, file_size_bytes=20, hash="h2")

    removed = repo.clear_for_table("t.drop")
    assert removed == 1

    assert repo.get_table_state("t.drop") is None
    assert repo.get_sync_history("t.drop") == []
    # Untouched sibling survives.
    assert repo.get_table_state("t.keep") is not None
    assert repo.get_sync_history("t.keep") != []
