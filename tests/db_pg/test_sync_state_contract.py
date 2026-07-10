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


# ---------------------------------------------------------------------------
# set_skipped (#754) — per-table skip reason, mirrors set_error's shape.
# ---------------------------------------------------------------------------


def test_set_skipped_creates_row_with_reason(sync_repo):
    repo, _, _ = sync_repo

    repo.set_skipped("bucket.orders", "in_flight")

    state = repo.get_table_state("bucket.orders")
    assert state["status"] == "skipped"
    assert state["error"] == "in_flight"
    # First-ever skip (no prior sync) must not claim a sync happened.
    assert state["last_sync"] is None


def test_set_skipped_preserves_prior_sync_fields(sync_repo):
    repo, _, _ = sync_repo

    repo.update_sync(table_id="bucket.orders", rows=42, file_size_bytes=1024, hash="abc123")
    repo.set_skipped("bucket.orders", "source_filter")

    state = repo.get_table_state("bucket.orders")
    assert state["status"] == "skipped"
    assert state["error"] == "source_filter"
    # Untouched — the last successful sync's data stays visible to the
    # manifest / `agnes pull` while this run's skip reason is recorded.
    assert state["rows"] == 42
    assert state["hash"] == "abc123"
    assert state["last_sync"] is not None


def test_update_sync_can_preserve_last_sync(sync_repo):
    """`bump_last_sync=False` records fresh rows/hash and clears a prior
    error WITHOUT touching last_sync — the filesystem-fallback publish path
    for materialized rows needs exactly this so the daily schedule gate
    stays open (a bumped last_sync would starve same-day retries)."""
    repo, _, _ = sync_repo

    repo.update_sync(table_id="mat.orders", rows=1, file_size_bytes=10, hash="a" * 32)
    before = repo.get_table_state("mat.orders")["last_sync"]
    assert before is not None
    repo.set_error("mat.orders", "killed mid-run")

    repo.update_sync(table_id="mat.orders", rows=7, file_size_bytes=70, hash="b" * 32, bump_last_sync=False)

    state = repo.get_table_state("mat.orders")
    assert state["last_sync"] == before, "bump_last_sync=False must preserve last_sync"
    assert state["rows"] == 7
    assert state["hash"] == "b" * 32
    assert state["status"] == "ok"
    assert state["error"] in (None, "")


def test_update_sync_preserve_on_fresh_row_leaves_last_sync_null(sync_repo):
    """First-ever write with bump_last_sync=False must not fabricate a
    last_sync — NULL keeps the row 'due' and the manifest honest."""
    repo, _, _ = sync_repo

    repo.update_sync(table_id="mat.fresh", rows=3, file_size_bytes=30, hash="c" * 32, bump_last_sync=False)

    state = repo.get_table_state("mat.fresh")
    assert state is not None
    assert state["last_sync"] is None
    assert state["rows"] == 3
    assert state["status"] == "ok"


def test_update_sync_clears_a_prior_skip(sync_repo):
    """A table that gets skipped one run and materializes successfully the
    next must flip back to status='ok' — mirrors `update_sync` already
    clearing a prior `set_error`."""
    repo, _, _ = sync_repo

    repo.set_skipped("bucket.orders", "not_in_target")
    repo.update_sync(table_id="bucket.orders", rows=1, file_size_bytes=10, hash="h1")

    state = repo.get_table_state("bucket.orders")
    assert state["status"] == "ok"
    assert state["error"] in (None, "")
