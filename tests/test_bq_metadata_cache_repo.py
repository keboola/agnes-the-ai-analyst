"""Repository + freshness tests for the persistent BQ metadata cache."""

from datetime import datetime, timedelta, timezone

import pytest

from src.repositories.bq_metadata_cache import BqMetadataCacheRepository


def test_upsert_success_inserts_then_updates(seeded_app):
    from src.db import get_system_db
    conn = get_system_db()
    try:
        repo = BqMetadataCacheRepository(conn)
        repo.upsert_success(
            "orders", rows=10, size_bytes=2048,
            partition_by="event_date", clustered_by=["country"],
            entity_type="BASE TABLE",
            known_columns=["event_date", "country", "amount"],
        )
        row = repo.get("orders")
        assert row is not None
        assert row["rows"] == 10
        assert row["size_bytes"] == 2048
        assert row["partition_by"] == "event_date"
        assert row["clustered_by"] == ["country"]
        assert row["entity_type"] == "BASE TABLE"
        assert row["known_columns"] == ["event_date", "country", "amount"]
        assert row["refreshed_at"] is not None
        assert row["error_at"] is None

        # Update with new numbers; refreshed_at advances.
        first_refresh = row["refreshed_at"]
        repo.upsert_success(
            "orders", rows=20, size_bytes=4096,
            partition_by=None, clustered_by=[],
        )
        row2 = repo.get("orders")
        assert row2["rows"] == 20
        assert row2["partition_by"] is None
        assert row2["clustered_by"] == []
        assert row2["refreshed_at"] >= first_refresh
    finally:
        conn.close()


def test_mark_error_preserves_prior_success(seeded_app):
    """After a successful refresh, a subsequent failure must keep the
    rows/size_bytes columns untouched — analyst Claude keeps using the
    last-known-good numbers while the next scheduled retry attempts to
    recover."""
    from src.db import get_system_db
    conn = get_system_db()
    try:
        repo = BqMetadataCacheRepository(conn)
        repo.upsert_success(
            "orders", rows=100, size_bytes=1000,
            partition_by=None, clustered_by=None,
        )
        repo.mark_error("orders", "BQ timeout")
        row = repo.get("orders")
        assert row["rows"] == 100, "prior success must be preserved across error"
        assert row["size_bytes"] == 1000
        assert row["error_at"] is not None
        assert row["error_msg"] == "BQ timeout"
        # Subsequent success clears the error.
        repo.upsert_success(
            "orders", rows=200, size_bytes=2000,
            partition_by=None, clustered_by=None,
        )
        row2 = repo.get("orders")
        assert row2["rows"] == 200
        assert row2["error_at"] is None
        assert row2["error_msg"] is None
    finally:
        conn.close()


def test_mark_error_truncates_long_messages(seeded_app):
    from src.db import get_system_db
    conn = get_system_db()
    try:
        repo = BqMetadataCacheRepository(conn)
        repo.mark_error("orders", "x" * 2000)
        row = repo.get("orders")
        assert len(row["error_msg"]) == 512
    finally:
        conn.close()


def test_list_all_orders_by_table_id(seeded_app):
    from src.db import get_system_db
    conn = get_system_db()
    try:
        repo = BqMetadataCacheRepository(conn)
        repo.upsert_success(
            "zeta", rows=1, size_bytes=1, partition_by=None, clustered_by=None,
        )
        repo.upsert_success(
            "alpha", rows=2, size_bytes=2, partition_by=None, clustered_by=None,
        )
        rows = repo.list_all()
        ids = [r["table_id"] for r in rows]
        assert ids == sorted(ids)
    finally:
        conn.close()


def test_delete_removes_row(seeded_app):
    from src.db import get_system_db
    conn = get_system_db()
    try:
        repo = BqMetadataCacheRepository(conn)
        repo.upsert_success(
            "orders", rows=1, size_bytes=1, partition_by=None, clustered_by=None,
        )
        repo.delete("orders")
        assert repo.get("orders") is None
    finally:
        conn.close()


# ─── compute_freshness ────────────────────────────────────────────────────


def test_freshness_never_fetched_for_missing_row():
    from app.api.bq_metadata_refresh import compute_freshness
    assert compute_freshness(None) == "never_fetched"


def test_freshness_never_fetched_for_no_refresh_no_error():
    from app.api.bq_metadata_refresh import compute_freshness
    row = {"refreshed_at": None, "error_at": None}
    assert compute_freshness(row) == "never_fetched"


def test_freshness_error_when_only_error_present():
    from app.api.bq_metadata_refresh import compute_freshness
    row = {
        "refreshed_at": None,
        "error_at": datetime.now(timezone.utc),
    }
    assert compute_freshness(row) == "error"


def test_freshness_fresh_within_threshold():
    from app.api.bq_metadata_refresh import compute_freshness
    now = datetime.now(timezone.utc)
    row = {
        "refreshed_at": now - timedelta(seconds=60),
        "error_at": None,
    }
    # 1-minute-old row with a 1-hour threshold ⇒ fresh.
    assert compute_freshness(row, now=now, fresh_threshold=3600) == "fresh"


def test_freshness_stale_beyond_threshold():
    from app.api.bq_metadata_refresh import compute_freshness
    now = datetime.now(timezone.utc)
    row = {
        "refreshed_at": now - timedelta(hours=10),
        "error_at": None,
    }
    assert compute_freshness(row, now=now, fresh_threshold=3600) == "stale"


# ─── entity_type + known_columns ───────────────────────────────────────────


def test_upsert_without_entity_type_or_known_columns(seeded_app):
    """Legacy callers (or pre-fetch paths) may not have entity_type or
    known_columns yet. Default-None must round-trip as None / None."""
    from src.db import get_system_db
    conn = get_system_db()
    try:
        repo = BqMetadataCacheRepository(conn)
        repo.upsert_success(
            "older", rows=1, size_bytes=1,
            partition_by=None, clustered_by=None,
        )
        row = repo.get("older")
        assert row["entity_type"] is None
        assert row["known_columns"] is None
    finally:
        conn.close()


def test_entity_type_view_is_round_tripped(seeded_app):
    from src.db import get_system_db
    conn = get_system_db()
    try:
        repo = BqMetadataCacheRepository(conn)
        repo.upsert_success(
            "a_view", rows=None, size_bytes=None,
            partition_by=None, clustered_by=None,
            entity_type="VIEW", known_columns=["a", "b"],
        )
        row = repo.get("a_view")
        assert row["entity_type"] == "VIEW"
        assert row["known_columns"] == ["a", "b"]
    finally:
        conn.close()


def test_known_columns_empty_list_distinct_from_none(seeded_app):
    """An empty known_columns list (e.g. table exists but COLUMNS returned
    nothing) must round-trip as ``[]`` not ``None``."""
    from src.db import get_system_db
    conn = get_system_db()
    try:
        repo = BqMetadataCacheRepository(conn)
        repo.upsert_success(
            "empty_cols", rows=0, size_bytes=0,
            partition_by=None, clustered_by=None,
            entity_type="BASE TABLE", known_columns=[],
        )
        row = repo.get("empty_cols")
        assert row["known_columns"] == []
    finally:
        conn.close()
