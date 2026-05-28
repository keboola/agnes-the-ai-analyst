"""Tests for ChatRepository — sessions, messages, and workdir markers.

Fixture note: the plan's spec names ``open_db`` / ``migrate`` but those don't
exist in src/db.py.  The real equivalents (same pattern as
tests/test_chat_db_migration.py) are:
  - ``duckdb.connect(":memory:")``   to open an in-memory connection
  - ``_ensure_schema(conn)``         to migrate it to the current version
"""
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pytest

from src.db import _ensure_schema

from app.chat.persistence import ChatRepository
from app.chat.types import Surface


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def repo() -> ChatRepository:
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    return ChatRepository(conn)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def test_create_and_get_session(repo: ChatRepository):
    s = repo.create_session(user_email="u@x", surface=Surface.WEB, title="t")
    assert s.id.startswith("chat_") and len(s.id) == len("chat_") + 12
    fetched = repo.get_session(s.id)
    assert fetched is not None
    assert fetched.user_email == "u@x"
    assert fetched.surface == Surface.WEB
    assert fetched.title == "t"
    assert fetched.archived is False


def test_list_sessions_by_user_recent_first(repo: ChatRepository):
    a = repo.create_session(user_email="u@x", surface=Surface.WEB)
    b = repo.create_session(user_email="u@x", surface=Surface.WEB)
    repo.append_message(session_id=b.id, role="user", content="hi")
    listing = repo.list_sessions("u@x")
    assert [s.id for s in listing] == [b.id, a.id]


def test_get_slack_dm_session_by_channel(repo: ChatRepository):
    s = repo.create_session(
        user_email="u@x", surface=Surface.SLACK_DM, slack_channel_id="C123",
    )
    again = repo.get_slack_dm_session("C123")
    assert again is not None and again.id == s.id
    assert repo.get_slack_dm_session("C-other") is None


def test_get_slack_thread_session(repo: ChatRepository):
    s = repo.create_session(
        user_email="u@x", surface=Surface.SLACK_THREAD,
        slack_channel_id="C1", slack_thread_ts="123.456",
    )
    again = repo.get_slack_thread_session("C1", "123.456")
    assert again is not None and again.id == s.id


def test_archive_session(repo: ChatRepository):
    s = repo.create_session(user_email="u@x", surface=Surface.WEB)
    repo.archive_session(s.id)
    refreshed = repo.get_session(s.id)
    assert refreshed is not None and refreshed.archived is True


def test_archived_slack_dm_does_not_block_new_one(repo: ChatRepository):
    a = repo.create_session(user_email="u@x", surface=Surface.SLACK_DM, slack_channel_id="C1")
    repo.archive_session(a.id)
    b = repo.create_session(user_email="u@x", surface=Surface.SLACK_DM, slack_channel_id="C1")
    assert b.id != a.id


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def test_append_and_list_messages(repo: ChatRepository):
    s = repo.create_session(user_email="u@x", surface=Surface.WEB)
    m1 = repo.append_message(session_id=s.id, role="user", content="hi")
    m2 = repo.append_message(
        session_id=s.id, role="assistant", content="hello",
        tool_calls=[{"tool": "list_catalog", "args": {}}],
        tokens_in=5, tokens_out=3, model="claude-haiku-4-5-20251001",
    )
    msgs = repo.list_messages(s.id)
    assert [m.id for m in msgs] == [m1.id, m2.id]
    assert msgs[1].tool_calls == [{"tool": "list_catalog", "args": {}}]
    refreshed = repo.get_session(s.id)
    assert refreshed is not None and refreshed.message_count == 2


def test_list_messages_after_cursor(repo: ChatRepository):
    s = repo.create_session(user_email="u@x", surface=Surface.WEB)
    m1 = repo.append_message(session_id=s.id, role="user", content="a")
    m2 = repo.append_message(session_id=s.id, role="user", content="b")
    out = repo.list_messages(s.id, after_id=m1.id)
    assert [m.id for m in out] == [m2.id]


# ---------------------------------------------------------------------------
# Workdirs
# ---------------------------------------------------------------------------

def test_workdir_upsert_and_fetch(repo: ChatRepository):
    repo.upsert_workdir(
        user_email="u@x", marketplace_sha="abc",
        initial_workspace_sha="def", agnes_version="0.55.0",
    )
    w = repo.get_workdir("u@x")
    assert w is not None
    assert w.marketplace_sha == "abc"
    assert w.agnes_version_at_init == "0.55.0"


def test_daily_anthropic_tokens(repo: ChatRepository):
    s = repo.create_session(user_email="u@x", surface=Surface.WEB)
    repo.append_message(session_id=s.id, role="assistant", content="x",
                         tokens_in=100, tokens_out=50)
    repo.append_message(session_id=s.id, role="assistant", content="y",
                         tokens_in=200, tokens_out=80)
    tin, tout = repo.daily_anthropic_tokens("u@x")
    assert tin == 300 and tout == 130
