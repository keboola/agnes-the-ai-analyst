"""CRUD-level tests for app/chat/persistence.py (chat_sessions + chat_messages)."""

import duckdb
import pytest

from src.db import _ensure_schema
from app.chat.persistence import ChatRepository


@pytest.fixture
def conn():
    c = duckdb.connect(":memory:")
    _ensure_schema(c)
    return c


@pytest.fixture
def repo(conn):
    return ChatRepository(conn)


class TestSessions:
    def test_create_returns_session_with_id_and_zero_count(self, repo):
        session = repo.create_session("alice@example.com", title="Q4 revenue")
        assert session.id.startswith("chat_")
        assert session.user_email == "alice@example.com"
        assert session.title == "Q4 revenue"
        assert session.message_count == 0
        assert session.archived is False
        assert session.started_at is not None

    def test_create_with_null_title(self, repo):
        session = repo.create_session("alice@example.com")
        assert session.title is None

    def test_get_returns_none_for_unknown_id(self, repo):
        assert repo.get_session("chat_doesnotexist") is None

    def test_list_sessions_scoped_by_email(self, repo):
        a1 = repo.create_session("alice@example.com", title="A1")
        b1 = repo.create_session("bob@example.com", title="B1")
        a2 = repo.create_session("alice@example.com", title="A2")
        alice_sessions = repo.list_sessions("alice@example.com")
        assert {s.id for s in alice_sessions} == {a1.id, a2.id}
        bob_sessions = repo.list_sessions("bob@example.com")
        assert {s.id for s in bob_sessions} == {b1.id}

    def test_list_sessions_excludes_archived_by_default(self, repo):
        a = repo.create_session("alice@example.com", title="Active")
        b = repo.create_session("alice@example.com", title="Archived")
        repo.archive_session(b.id)
        visible = repo.list_sessions("alice@example.com")
        assert {s.id for s in visible} == {a.id}
        all_sessions = repo.list_sessions("alice@example.com", include_archived=True)
        assert {s.id for s in all_sessions} == {a.id, b.id}

    def test_set_title_persists(self, repo):
        session = repo.create_session("alice@example.com")
        repo.set_title(session.id, "Renamed thread")
        reloaded = repo.get_session(session.id)
        assert reloaded is not None
        assert reloaded.title == "Renamed thread"


class TestMessages:
    def test_add_message_bumps_counter_and_last_at(self, repo):
        session = repo.create_session("alice@example.com")
        msg = repo.add_message(session.id, role="user", content="hello")
        assert msg.id.startswith("msg_")
        assert msg.content == "hello"

        reloaded = repo.get_session(session.id)
        assert reloaded is not None
        assert reloaded.message_count == 1
        assert reloaded.last_message_at is not None

    def test_add_message_with_tool_calls_roundtrips(self, repo):
        session = repo.create_session("alice@example.com")
        tc = [{"tool": "run_query", "args": {"sql": "SELECT 1"}}]
        repo.add_message(
            session.id,
            role="assistant",
            content="here it is",
            tool_calls=tc,
            tokens_in=12,
            tokens_out=8,
            model="claude-haiku-4-5-20251001",
        )
        msgs = repo.list_messages(session.id)
        assert len(msgs) == 1
        m = msgs[0]
        assert m.role == "assistant"
        assert m.tool_calls == tc
        assert m.tokens_in == 12
        assert m.tokens_out == 8
        assert m.model == "claude-haiku-4-5-20251001"

    def test_list_messages_ordered_chronologically(self, repo):
        session = repo.create_session("alice@example.com")
        repo.add_message(session.id, role="user", content="first")
        repo.add_message(session.id, role="assistant", content="second")
        repo.add_message(session.id, role="user", content="third")
        contents = [m.content for m in repo.list_messages(session.id)]
        assert contents == ["first", "second", "third"]

    def test_list_messages_empty_for_unknown_session(self, repo):
        assert repo.list_messages("chat_nope") == []
