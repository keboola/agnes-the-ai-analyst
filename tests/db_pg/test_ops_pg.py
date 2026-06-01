"""Postgres-side smoke + invariant tests for the ops triad:
table_registry, sync_state, sync_history.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def ops_engine(pg_engine, monkeypatch):
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
# table_registry
# ---------------------------------------------------------------------------

def test_table_registry_register_and_get(ops_engine):
    from src.repositories.table_registry_pg import TableRegistryPgRepository

    repo = TableRegistryPgRepository(ops_engine)
    repo.register(
        id="t1",
        name="web_sessions",
        source_type="bigquery",
        bucket="analytics_prod",
        source_table="web_sessions",
        query_mode="remote",
        primary_key=["session_id", "event_date"],
    )
    row = repo.get("t1")
    assert row["name"] == "web_sessions"
    assert row["source_type"] == "bigquery"
    assert row["query_mode"] == "remote"
    assert row["primary_key"] == ["session_id", "event_date"]


def test_table_registry_register_upserts(ops_engine):
    from src.repositories.table_registry_pg import TableRegistryPgRepository

    repo = TableRegistryPgRepository(ops_engine)
    repo.register(id="t1", name="orig_name", source_type="keboola")
    repo.register(id="t1", name="new_name", source_type="bigquery")
    row = repo.get("t1")
    assert row["name"] == "new_name"
    assert row["source_type"] == "bigquery"


def test_table_registry_find_by_bq_path_case_insensitive(ops_engine):
    from src.repositories.table_registry_pg import TableRegistryPgRepository

    repo = TableRegistryPgRepository(ops_engine)
    repo.register(
        id="t1",
        name="web_sessions",
        source_type="bigquery",
        bucket="Analytics_Prod",
        source_table="Web_Sessions",
    )
    found = repo.find_by_bq_path("analytics_prod", "web_sessions")
    assert found is not None
    assert found["id"] == "t1"


def test_table_registry_list_filters(ops_engine):
    from src.repositories.table_registry_pg import TableRegistryPgRepository

    repo = TableRegistryPgRepository(ops_engine)
    repo.register(id="t1", name="a", source_type="bigquery", query_mode="remote")
    repo.register(id="t2", name="b", source_type="keboola", query_mode="local")
    repo.register(id="t3", name="c", source_type="bigquery", query_mode="local")

    bq_rows = repo.list_by_source("bigquery")
    assert {r["id"] for r in bq_rows} == {"t1", "t3"}

    local_rows = repo.list_local()
    assert {r["id"] for r in local_rows} == {"t2", "t3"}

    local_bq = repo.list_local(source_type="bigquery")
    assert {r["id"] for r in local_bq} == {"t3"}


def test_table_registry_unregister(ops_engine):
    from src.repositories.table_registry_pg import TableRegistryPgRepository

    repo = TableRegistryPgRepository(ops_engine)
    repo.register(id="t1", name="a")
    assert repo.get("t1") is not None
    repo.unregister("t1")
    assert repo.get("t1") is None


# ---------------------------------------------------------------------------
# sync_state + sync_history
# ---------------------------------------------------------------------------

def test_sync_state_upsert_writes_history(ops_engine):
    from src.repositories.sync_state_pg import SyncStatePgRepository

    repo = SyncStatePgRepository(ops_engine)
    repo.update_sync("t1", rows=1000, file_size_bytes=2048, hash="h1", duration_ms=42)
    state = repo.get_table_state("t1")
    assert state["rows"] == 1000
    assert state["status"] == "ok"
    history = repo.get_sync_history("t1")
    assert len(history) == 1
    assert history[0]["duration_ms"] == 42

    # Second sync upserts state, appends history
    repo.update_sync("t1", rows=2000, file_size_bytes=4096, hash="h2", duration_ms=50)
    state = repo.get_table_state("t1")
    assert state["rows"] == 2000
    assert state["hash"] == "h2"
    history = repo.get_sync_history("t1")
    assert len(history) == 2


def test_sync_state_set_and_clear_error(ops_engine):
    from src.repositories.sync_state_pg import SyncStatePgRepository

    repo = SyncStatePgRepository(ops_engine)
    # set_error creates a row when none exists yet, last_sync stays NULL
    repo.set_error("t1", "kbcstorage 502")
    state = repo.get_table_state("t1")
    assert state["status"] == "error"
    assert state["error"] == "kbcstorage 502"
    assert state["last_sync"] is None

    # Followed by a successful sync flips status back via update_sync
    repo.update_sync("t1", rows=1, file_size_bytes=10, hash="h")
    state = repo.get_table_state("t1")
    assert state["status"] == "ok"

    # Direct clear_error path is idempotent on already-ok rows
    repo.clear_error("t1")
    assert repo.get_table_state("t1")["status"] == "ok"


def test_sync_state_set_error_preserves_prior_success_fields(ops_engine):
    from src.repositories.sync_state_pg import SyncStatePgRepository

    repo = SyncStatePgRepository(ops_engine)
    repo.update_sync("t1", rows=1000, file_size_bytes=2048, hash="h1")
    repo.set_error("t1", "transient failure")
    state = repo.get_table_state("t1")
    assert state["status"] == "error"
    assert state["error"] == "transient failure"
    # Prior success columns preserved so analysts can keep using the last good parquet
    assert state["rows"] == 1000
    assert state["hash"] == "h1"


def test_sync_history_list_recent_filters(ops_engine):
    from src.repositories.sync_state_pg import SyncStatePgRepository

    repo = SyncStatePgRepository(ops_engine)
    repo.update_sync("t1", rows=10, file_size_bytes=1, hash="h")
    repo.update_sync("t2", rows=20, file_size_bytes=2, hash="h")
    repo.update_sync("t3", rows=30, file_size_bytes=3, hash="h", status="ok")

    now_minus_one = datetime.now(timezone.utc) - timedelta(seconds=10)
    rows = repo.list_recent(since=now_minus_one, limit=10)
    assert len(rows) == 3

    # Future since: no rows
    rows = repo.list_recent(
        since=datetime.now(timezone.utc) + timedelta(hours=1), limit=10
    )
    assert rows == []
