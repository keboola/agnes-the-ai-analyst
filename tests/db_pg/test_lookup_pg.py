"""Postgres-side smoke + invariant tests for the lookup cluster:
view_ownership, column_metadata, bq_metadata_cache, user_sync_settings.
"""
from __future__ import annotations

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def lookup_engine(pg_engine, monkeypatch):
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
# view_ownership
# ---------------------------------------------------------------------------

def test_view_ownership_claim_and_collision(lookup_engine):
    from src.repositories.view_ownership_pg import ViewOwnershipPgRepository

    repo = ViewOwnershipPgRepository(lookup_engine)
    assert repo.claim("revenue", "kbc") is True
    assert repo.get_owner("revenue") == "kbc"
    # Same source re-claim is OK
    assert repo.claim("revenue", "kbc") is True
    # Different source loses
    assert repo.claim("revenue", "jira") is False
    assert repo.get_owner("revenue") == "kbc"


def test_view_ownership_release(lookup_engine):
    from src.repositories.view_ownership_pg import ViewOwnershipPgRepository

    repo = ViewOwnershipPgRepository(lookup_engine)
    repo.claim("revenue", "kbc")
    assert repo.release("revenue", "kbc") is True
    # Release on unowned returns False
    assert repo.release("revenue", "kbc") is False


def test_view_ownership_reconcile(lookup_engine):
    from src.repositories.view_ownership_pg import ViewOwnershipPgRepository

    repo = ViewOwnershipPgRepository(lookup_engine)
    repo.claim("a", "kbc")
    repo.claim("b", "kbc")
    repo.claim("c", "jira")

    dropped = repo.reconcile([("kbc", "a"), ("jira", "c")])
    assert dropped == [("kbc", "b")]
    assert repo.get_owner("b") is None
    assert repo.get_owner("a") == "kbc"
    assert repo.get_owner("c") == "jira"


# ---------------------------------------------------------------------------
# column_metadata
# ---------------------------------------------------------------------------

def test_column_metadata_save_and_get(lookup_engine):
    from src.repositories.column_metadata_pg import ColumnMetadataPgRepository

    repo = ColumnMetadataPgRepository(lookup_engine)
    repo.save("orders", "amount", basetype="NUMERIC", description="Order total in cents")
    row = repo.get("orders", "amount")
    assert row["basetype"] == "NUMERIC"
    assert row["description"] == "Order total in cents"
    # Upsert overwrites
    repo.save("orders", "amount", basetype="DECIMAL", description="Updated")
    row = repo.get("orders", "amount")
    assert row["basetype"] == "DECIMAL"


def test_column_metadata_list_for_table(lookup_engine):
    from src.repositories.column_metadata_pg import ColumnMetadataPgRepository

    repo = ColumnMetadataPgRepository(lookup_engine)
    repo.save("orders", "amount")
    repo.save("orders", "currency")
    repo.save("users", "email")
    rows = repo.list_for_table("orders")
    assert {r["column_name"] for r in rows} == {"amount", "currency"}


def test_column_metadata_delete_idempotent(lookup_engine):
    from src.repositories.column_metadata_pg import ColumnMetadataPgRepository

    repo = ColumnMetadataPgRepository(lookup_engine)
    repo.save("orders", "amount")
    assert repo.delete("orders", "amount") is True
    assert repo.delete("orders", "amount") is False


# ---------------------------------------------------------------------------
# bq_metadata_cache
# ---------------------------------------------------------------------------

def test_bq_metadata_upsert_and_mark_error(lookup_engine):
    from src.repositories.bq_metadata_cache_pg import BqMetadataCachePgRepository

    repo = BqMetadataCachePgRepository(lookup_engine)
    repo.upsert_success(
        "ds.web_sessions",
        rows=1000000,
        size_bytes=104857600,
        partition_by="event_date",
        clustered_by=["country_code", "user_id"],
        entity_type="BASE TABLE",
        known_columns=["session_id", "user_id", "event_date"],
    )
    row = repo.get("ds.web_sessions")
    assert row["rows"] == 1000000
    assert row["entity_type"] == "BASE TABLE"
    assert row["clustered_by"] == ["country_code", "user_id"]
    assert row["known_columns"] == ["session_id", "user_id", "event_date"]
    assert row["error_at"] is None

    # mark_error preserves the prior success row
    repo.mark_error("ds.web_sessions", "transient failure")
    row = repo.get("ds.web_sessions")
    assert row["error_msg"] == "transient failure"
    assert row["rows"] == 1000000  # preserved
    assert row["clustered_by"] == ["country_code", "user_id"]  # preserved


def test_bq_metadata_delete(lookup_engine):
    from src.repositories.bq_metadata_cache_pg import BqMetadataCachePgRepository

    repo = BqMetadataCachePgRepository(lookup_engine)
    repo.upsert_success(
        "ds.t", rows=1, size_bytes=1, partition_by=None, clustered_by=None,
    )
    repo.delete("ds.t")
    assert repo.get("ds.t") is None


# ---------------------------------------------------------------------------
# user_sync_settings
# ---------------------------------------------------------------------------

def test_sync_settings_set_and_is_enabled(lookup_engine):
    from src.repositories.sync_settings_pg import SyncSettingsPgRepository

    repo = SyncSettingsPgRepository(lookup_engine)
    repo.set_dataset_enabled("u1", "ds_a", enabled=True)
    repo.set_dataset_enabled("u1", "ds_b", enabled=False)
    repo.set_dataset_enabled("u2", "ds_a", enabled=True)

    assert repo.is_dataset_enabled("u1", "ds_a") is True
    assert repo.is_dataset_enabled("u1", "ds_b") is False
    assert repo.is_dataset_enabled("u1", "ds_nonexistent") is False
    assert repo.get_enabled_datasets("u1") == ["ds_a"]
    assert set(repo.get_enabled_datasets("u2")) == {"ds_a"}


def test_sync_settings_upsert(lookup_engine):
    from src.repositories.sync_settings_pg import SyncSettingsPgRepository

    repo = SyncSettingsPgRepository(lookup_engine)
    repo.set_dataset_enabled("u1", "ds_a", enabled=True)
    repo.set_dataset_enabled("u1", "ds_a", enabled=False)
    assert repo.is_dataset_enabled("u1", "ds_a") is False
