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
