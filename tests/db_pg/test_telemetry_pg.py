"""Postgres-side tests for the telemetry + observability cluster:
session_processor_state, observability_views, usage.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def tel_engine(pg_engine, monkeypatch):
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
# session_processor_state
# ---------------------------------------------------------------------------

def test_session_processor_mark_and_is_processed(tel_engine):
    from src.repositories.session_processor_state_pg import (
        SessionProcessorStatePgRepository,
    )

    repo = SessionProcessorStatePgRepository(tel_engine)
    assert repo.is_processed("p1", "u/x.jsonl", "hash-A") is False

    repo.mark_processed("p1", "u/x.jsonl", username="u1", items_count=5, file_hash="hash-A")
    assert repo.is_processed("p1", "u/x.jsonl", "hash-A") is True
    # Different hash → counted as unprocessed (file grew)
    assert repo.is_processed("p1", "u/x.jsonl", "hash-B") is False
    # Different processor name → independent state
    assert repo.is_processed("p2", "u/x.jsonl", "hash-A") is False


def test_session_processor_upsert(tel_engine):
    from src.repositories.session_processor_state_pg import (
        SessionProcessorStatePgRepository,
    )

    repo = SessionProcessorStatePgRepository(tel_engine)
    repo.mark_processed("p1", "u/x.jsonl", username="u1", items_count=1, file_hash="A")
    repo.mark_processed("p1", "u/x.jsonl", username="u1", items_count=2, file_hash="B")
    assert repo.is_processed("p1", "u/x.jsonl", "B") is True


# ---------------------------------------------------------------------------
# observability_views
# ---------------------------------------------------------------------------

def test_obs_views_create_list_delete(tel_engine):
    from src.repositories.observability_views_pg import ObservabilityViewsPgRepository

    repo = ObservabilityViewsPgRepository(tel_engine)
    saved = repo.create("u1", "errors-only", {"is_error": True})
    assert saved["name"] == "errors-only"
    assert saved["query"] == {"is_error": True}

    rows = repo.list_for_user("u1")
    assert len(rows) == 1
    assert rows[0]["name"] == "errors-only"

    assert repo.delete("u1", saved["id"]) is True
    assert repo.list_for_user("u1") == []
    # Re-delete returns False
    assert repo.delete("u1", saved["id"]) is False


def test_obs_views_create_upserts_on_name(tel_engine):
    from src.repositories.observability_views_pg import ObservabilityViewsPgRepository

    repo = ObservabilityViewsPgRepository(tel_engine)
    repo.create("u1", "saved-1", {"a": 1})
    repo.create("u1", "saved-1", {"a": 2})
    rows = repo.list_for_user("u1")
    assert len(rows) == 1
    assert rows[0]["query"] == {"a": 2}


# ---------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------

def test_usage_upsert_events_dedupes_by_id(tel_engine):
    from src.repositories.usage_pg import UsagePgRepository

    repo = UsagePgRepository(tel_engine)
    now = datetime.now(timezone.utc)
    rows = [
        {
            "id": "e1", "session_id": "s1", "session_file": "f1", "username": "u",
            "event_type": "tool_call", "tool_name": "Read", "source": "claude",
            "occurred_at": now,
        },
        {
            "id": "e2", "session_id": "s1", "session_file": "f1", "username": "u",
            "event_type": "tool_call", "tool_name": "Edit", "source": "claude",
            "occurred_at": now,
        },
    ]
    repo.upsert_events(rows, processor_version=1)
    # Second call with same ids: ON CONFLICT DO NOTHING (dedup)
    repo.upsert_events(rows, processor_version=1)

    import sqlalchemy as sa
    with tel_engine.connect() as conn:
        count = conn.execute(sa.text("SELECT COUNT(*) FROM usage_events")).scalar()
    assert count == 2


def test_usage_upsert_summary_overwrites(tel_engine):
    from src.repositories.usage_pg import UsagePgRepository

    repo = UsagePgRepository(tel_engine)
    summary = {
        "session_file": "f1", "session_id": "s1", "username": "u",
        "user_messages": 5, "tool_calls": 10,
    }
    repo.upsert_summary(summary, processor_version=1)
    summary["user_messages"] = 7
    repo.upsert_summary(summary, processor_version=2)

    import sqlalchemy as sa
    with tel_engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT user_messages, processor_version FROM usage_session_summary WHERE session_file = 'f1'")
        ).first()
    assert row[0] == 7
    assert row[1] == 2


def test_usage_purge_for_session(tel_engine):
    from src.repositories.usage_pg import UsagePgRepository

    repo = UsagePgRepository(tel_engine)
    now = datetime.now(timezone.utc)
    repo.upsert_events(
        [
            {
                "id": "e1", "session_id": "s1", "session_file": "f1", "username": "u",
                "event_type": "x", "source": "x", "occurred_at": now,
            }
        ],
        processor_version=1,
    )
    repo.upsert_summary(
        {"session_file": "f1", "session_id": "s1", "username": "u"},
        processor_version=1,
    )
    deleted = repo.purge_for_session("f1")
    assert deleted == 1


# ---------------------------------------------------------------------------
# summary_query_telemetry (#410) — PG mirror of the DuckDB aggregation.
# ---------------------------------------------------------------------------

def _ins_audit(conn, *, action, resource, bytes_scanned, ts):
    import json
    import uuid as _uuid
    import sqlalchemy as sa

    params = {} if bytes_scanned is None else {"bytes_scanned": bytes_scanned}
    conn.execute(
        sa.text(
            """INSERT INTO audit_log (id, timestamp, user_id, action, resource, params, result)
               VALUES (:id, :ts, 'u1', :action, :resource, CAST(:params AS JSONB), 'success')"""
        ),
        {"id": str(_uuid.uuid4()), "ts": ts, "action": action,
         "resource": resource, "params": json.dumps(params)},
    )


def test_summary_query_telemetry_pg(tel_engine):
    from datetime import timedelta
    from src.repositories.usage_pg import UsagePgRepository
    import sqlalchemy as sa

    now = datetime.now(timezone.utc)
    with tel_engine.begin() as conn:
        # orders: remote x3 (100/200/300), local x1
        _ins_audit(conn, action="query.remote", resource="table:kbc.orders",
                   bytes_scanned=100, ts=now - timedelta(hours=1))
        _ins_audit(conn, action="query.remote", resource="table:kbc.orders",
                   bytes_scanned=200, ts=now - timedelta(hours=2))
        _ins_audit(conn, action="query.remote", resource="table:kbc.orders",
                   bytes_scanned=300, ts=now - timedelta(hours=3))
        _ins_audit(conn, action="query.local", resource="table:kbc.orders",
                   bytes_scanned=None, ts=now - timedelta(hours=4))
        # sessions: remote x1 (50), local x1
        _ins_audit(conn, action="query.remote", resource="table:kbc.sessions",
                   bytes_scanned=50, ts=now - timedelta(hours=5))
        _ins_audit(conn, action="query.local", resource="table:kbc.sessions",
                   bytes_scanned=None, ts=now - timedelta(hours=6))
        # adhoc local — totals only, not per-table ranking
        _ins_audit(conn, action="query.local", resource="adhoc",
                   bytes_scanned=None, ts=now - timedelta(hours=7))
        # snapshot.create on orders (resource carries :as: suffix), bytes 1000
        _ins_audit(conn, action="snapshot.create",
                   resource="table:kbc.orders:as:o_recent",
                   bytes_scanned=1000, ts=now - timedelta(hours=1))
        # out-of-window row — excluded by the 7d cutoff
        _ins_audit(conn, action="query.remote", resource="table:kbc.old",
                   bytes_scanned=999, ts=now - timedelta(days=40))

    repo = UsagePgRepository(tel_engine)
    out = repo.summary_query_telemetry(now - timedelta(days=7))

    by_id = {t["table_id"]: t for t in out["top_tables"]}
    assert out["top_tables"][0]["table_id"] == "kbc.orders"
    assert by_id["kbc.orders"]["queries"] == 5      # 3 remote + 1 local + 1 snapshot
    assert by_id["kbc.orders"]["scan_bytes"] == 1600
    assert by_id["kbc.orders"]["remote"] == 3
    assert by_id["kbc.orders"]["local"] == 1
    assert by_id["kbc.sessions"]["queries"] == 2
    assert by_id["kbc.sessions"]["scan_bytes"] == 50
    assert "adhoc" not in by_id
    assert "kbc.old" not in by_id

    assert out["total_scan_bytes"] == 1650
    assert out["remote_queries"] == 4
    assert out["local_queries"] == 3
    assert out["snapshot_creates"] == 1

    # per-day frequency: orders today → 3 remote + 1 local
    orders_freq = [r for r in out["frequency"] if r["table_id"] == "kbc.orders"]
    assert sum(r["remote"] for r in orders_freq) == 3
    assert sum(r["local"] for r in orders_freq) == 1


def test_summary_query_telemetry_empty_window_pg(tel_engine):
    from datetime import timedelta
    from src.repositories.usage_pg import UsagePgRepository

    now = datetime.now(timezone.utc)
    with tel_engine.begin() as conn:
        _ins_audit(conn, action="query.remote", resource="table:kbc.old",
                   bytes_scanned=999, ts=now - timedelta(days=40))

    out = UsagePgRepository(tel_engine).summary_query_telemetry(now - timedelta(days=7))
    assert out["top_tables"] == []
    assert out["frequency"] == []
    assert out["total_scan_bytes"] == 0
    assert out["remote_queries"] == 0
    assert out["local_queries"] == 0
    assert out["snapshot_creates"] == 0
