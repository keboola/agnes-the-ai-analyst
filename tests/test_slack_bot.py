"""Tests for Slack identity binding (verification code flow).

Fixture note: the plan's spec names ``open_db`` / ``migrate`` but those don't
exist in src/db.py.  The real equivalents are:
  - ``duckdb.connect(":memory:")``   to open an in-memory connection
  - ``_ensure_schema(conn)``         to migrate it to the current version
"""
from pathlib import Path

import duckdb
import pytest
from src.db import _ensure_schema

from services.slack_bot.binding import (
    issue_verification_code,
    lookup_user_email,
    redeem_verification_code,
)


@pytest.fixture
def conn():
    c = duckdb.connect(":memory:")
    _ensure_schema(c)
    c.execute("INSERT INTO users(id, email, name) VALUES ('uid1', 'u@x', 'U')")
    return c


def test_issue_and_redeem(conn):
    code = issue_verification_code(conn, slack_user_id="U123")
    assert len(code) == 6 and code.isdigit()
    ok = redeem_verification_code(conn, user_email="u@x", code=code)
    assert ok is True
    assert lookup_user_email(_RepoStub(conn), "U123") == "u@x"


def test_redeem_rejects_bad_code(conn):
    issue_verification_code(conn, slack_user_id="U123")
    assert redeem_verification_code(conn, user_email="u@x", code="000000") is False


def test_redeem_rejects_expired(conn, monkeypatch):
    import services.slack_bot.binding as b
    monkeypatch.setattr(b, "_CODE_TTL_SECONDS", -1)
    code = issue_verification_code(conn, slack_user_id="U123")
    assert redeem_verification_code(conn, user_email="u@x", code=code) is False


class _RepoStub:
    def __init__(self, conn): self._conn = conn


# ---------------------------------------------------------------------------
# SlackSinkBridge unit tests (architect finding #4)
# ---------------------------------------------------------------------------

def test_slack_sink_forwards_assistant_message(monkeypatch):
    """assistant_message frames hit send_thread_reply with the content body."""
    import asyncio
    from services.slack_bot import sink as sink_mod

    sent: list[tuple[str, str, str]] = []

    async def fake_send(ch, ts, text):
        sent.append((ch, ts, text))

    monkeypatch.setattr(sink_mod, "send_thread_reply", fake_send)

    async def _run():
        bridge = sink_mod.SlackSinkBridge(channel="D1", thread_ts="1.1")
        await bridge.send_json({"type": "assistant_message", "content": "hello"})
        await bridge.send_json({"type": "token", "text": "noisy"})  # dropped
        await bridge.send_json({"type": "ready"})  # dropped
        await bridge.close()

    asyncio.run(_run())
    assert sent == [("D1", "1.1", "hello")]


def test_slack_sink_forwards_error_and_cancelled(monkeypatch):
    """error + cancelled produce visible Slack posts so the user knows."""
    import asyncio
    from services.slack_bot import sink as sink_mod

    sent: list[tuple[str, str, str]] = []

    async def fake_send(ch, ts, text):
        sent.append((ch, ts, text))

    monkeypatch.setattr(sink_mod, "send_thread_reply", fake_send)

    async def _run():
        bridge = sink_mod.SlackSinkBridge(channel="D1", thread_ts="1.1")
        await bridge.send_json({"type": "error", "kind": "daily_budget", "message": "exhausted"})
        await bridge.send_json({"type": "cancelled"})
        await bridge.close()

    asyncio.run(_run())
    assert len(sent) == 2
    assert "daily_budget" in sent[0][2]
    assert "exhausted" in sent[0][2]
    assert "stopped" in sent[1][2]


# ---------------------------------------------------------------------------
# _handle_dm tests — verification code + assistant-back pump
# ---------------------------------------------------------------------------

def _build_slack_app_state():
    """Build an app-shaped object with .state.chat_repo + .state.chat_manager.

    Uses a real ChatRepository over an in-memory DuckDB so the binding
    table CREATE works. ChatManager is mocked — we only need
    `list_live()`, `create_session()`, `attach()`, `send_user_message()`.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.chat.persistence import ChatRepository
    from app.chat.types import ChatSession, Surface
    from datetime import datetime, timezone

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    conn.execute(
        "INSERT INTO users(id, email, name) VALUES ('uid1', 'bob@example.com', 'Bob')"
    )
    repo = ChatRepository(conn)

    created_sessions: list[ChatSession] = []
    attached: list = []
    sent_msgs: list[tuple[str, str]] = []

    async def create_session(*, user_email, surface, slack_channel_id=None, **kw):
        s = ChatSession(
            id="sess-1",
            user_email=user_email,
            surface=surface,
            slack_channel_id=slack_channel_id,
            slack_thread_ts=None,
            title=None,
            started_at=datetime.now(timezone.utc),
            last_message_at=None,
            message_count=0,
            archived=False,
        )
        created_sessions.append(s)
        return s

    async def attach(chat_id, sink):
        attached.append((chat_id, sink))
        # Simulate one assistant_message round-trip through the sink so
        # the test can assert on the reply path.
        await sink.send_json({"type": "ready"})
        await sink.send_json({"type": "assistant_message", "content": "echo: hello agnes"})

    async def send_user_message(chat_id, text):
        sent_msgs.append((chat_id, text))

    mgr = SimpleNamespace(
        list_live=lambda: [],
        create_session=create_session,
        attach=attach,
        send_user_message=send_user_message,
        _created=created_sessions,
        _attached=attached,
        _sent=sent_msgs,
    )

    state = SimpleNamespace(
        chat_repo=repo, chat_manager=mgr, public_url="https://agnes.example.com"
    )
    app = SimpleNamespace(state=state)
    return app, repo, mgr, conn


def test_slack_dm_unbound_user_gets_verification_code(monkeypatch):
    """First DM from an unbound user → bot DMs a 6-digit code."""
    import asyncio
    import re

    from services.slack_bot import events as ev

    sent: list[tuple[str, str, str]] = []

    async def fake_send(ch, ts, text):
        sent.append((ch, ts, text))

    monkeypatch.setattr(ev, "send_thread_reply", fake_send)

    app, _repo, _mgr, conn = _build_slack_app_state()
    # The binding tables are created lazily by `issue_verification_code`
    # on first call, but `lookup_user_email` runs *before* that and needs
    # the slack_user_id column on `users`. Force-init now.
    from services.slack_bot.binding import _ensure_table

    _ensure_table(conn)

    event = {
        "type": "message", "channel_type": "im", "channel": "D2",
        "user": "U999", "ts": "2.2", "text": "hello",
    }

    asyncio.run(ev.dispatch_event(app, event))

    assert sent, "bot must reply to the unbound user"
    # The plan asserts a 6-digit code wrapped in *...* (Slack bold).
    assert any(
        "6-digit" in text and re.search(r"\*\d{6}\*", text)
        for _ch, _ts, text in sent
    ), sent


def test_slack_dm_bound_user_attaches_sink_and_sends(monkeypatch):
    """Bound DM → no verification code; bridge attached + user_msg forwarded."""
    import asyncio

    from services.slack_bot import events as ev

    sent: list[tuple[str, str, str]] = []

    async def fake_send(ch, ts, text):
        sent.append((ch, ts, text))

    monkeypatch.setattr(ev, "send_thread_reply", fake_send)
    # Chat is a default-deny RBAC resource; the Slack DM handler checks the
    # bound user's grant before spawning. These tests cover the sink/spawn
    # plumbing, not the gate, so grant access. (Default-deny is covered by
    # test_chat_api::test_chat_requires_rbac_grant.)
    import app.auth.access as _access
    monkeypatch.setattr(_access, "can_access", lambda *a, **k: True)

    app, _repo, mgr, conn = _build_slack_app_state()

    # binding._ensure_table adds the column lazily; force it now.
    from services.slack_bot.binding import _ensure_table

    _ensure_table(conn)
    conn.execute(
        "UPDATE users SET slack_user_id = 'U123' WHERE email = 'bob@example.com'"
    )

    event = {
        "type": "message", "channel_type": "im", "channel": "D1",
        "user": "U123", "ts": "1.1", "text": "hello agnes",
    }

    asyncio.run(ev.dispatch_event(app, event))

    # Created exactly one session, attached the bridge, forwarded the text.
    assert len(mgr._created) == 1
    assert mgr._created[0].user_email == "bob@example.com"
    assert len(mgr._attached) == 1
    assert mgr._attached[0][0] == "sess-1"
    # The bridge is the second tuple element — it should be a
    # SlackSinkBridge instance.
    from services.slack_bot.sink import SlackSinkBridge
    assert isinstance(mgr._attached[0][1], SlackSinkBridge)
    assert mgr._sent == [("sess-1", "hello agnes")]


def test_slack_dm_assistant_message_reaches_thread(monkeypatch):
    """End-to-end: bound DM → assistant_message frame → send_thread_reply."""
    import asyncio

    from services.slack_bot import events as ev
    from services.slack_bot import sink as sink_mod

    # The sink talks to send_thread_reply; events also calls it for the
    # binding flow. Patch both so we capture everything.
    sent: list[tuple[str, str, str]] = []

    async def fake_send(ch, ts, text):
        sent.append((ch, ts, text))

    monkeypatch.setattr(ev, "send_thread_reply", fake_send)
    monkeypatch.setattr(sink_mod, "send_thread_reply", fake_send)
    # Grant chat access — see note in the sibling bound-user test.
    import app.auth.access as _access
    monkeypatch.setattr(_access, "can_access", lambda *a, **k: True)

    app, _repo, _mgr, conn = _build_slack_app_state()
    from services.slack_bot.binding import _ensure_table

    _ensure_table(conn)
    conn.execute(
        "UPDATE users SET slack_user_id = 'U123' WHERE email = 'bob@example.com'"
    )

    event = {
        "type": "message", "channel_type": "im", "channel": "D1",
        "user": "U123", "ts": "1.1", "text": "hello agnes",
    }

    async def _run():
        await ev.dispatch_event(app, event)
        # The attach() task scheduled by _handle_dm runs in the same loop.
        # Give it a beat to drain the simulated assistant_message frame.
        await asyncio.sleep(0.2)

    asyncio.run(_run())

    assert any(
        text == "echo: hello agnes" and ch == "D1"
        for ch, _ts, text in sent
    ), sent


class TestChannelAllowlist:
    def _everyone_gid(self, conn):
        return conn.execute(
            "SELECT id FROM user_groups WHERE name = 'Everyone'"
        ).fetchone()[0]

    def test_default_deny(self, conn):
        from services.slack_bot.binding import is_channel_allowlisted
        assert is_channel_allowlisted(conn, "C_NEW") is False

    def test_true_after_everyone_grant(self, conn):
        from services.slack_bot.binding import is_channel_allowlisted
        gid = self._everyone_gid(conn)
        conn.execute(
            "INSERT INTO resource_grants(id, group_id, resource_type, resource_id) "
            "VALUES ('rg_a', ?, 'slack_channel', 'C_OK')",
            [gid],
        )
        assert is_channel_allowlisted(conn, "C_OK") is True

    def test_admin_grant_does_not_open_channel(self, conn):
        """A grant to the Admin group (not Everyone) must NOT allowlist —
        proves we do not use can_access (no admin short-circuit)."""
        from services.slack_bot.binding import is_channel_allowlisted
        admin_gid = conn.execute(
            "SELECT id FROM user_groups WHERE name = 'Admin'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO resource_grants(id, group_id, resource_type, resource_id) "
            "VALUES ('rg_admin', ?, 'slack_channel', 'C_ADMIN')",
            [admin_gid],
        )
        assert is_channel_allowlisted(conn, "C_ADMIN") is False


def test_slack_app_mention_emits_log_record(caplog):
    """`app_mention` events must produce an INFO log so operators can see
    that the bot is receiving channel mentions even though channel scope
    is deferred (DM-only MVP).
    """
    import asyncio
    import logging
    import services.slack_bot.events as ev

    caplog.set_level(logging.INFO, logger="services.slack_bot.events")
    event = {
        "type": "app_mention", "channel": "C1", "thread_ts": "1.1",
        "user": "U999", "text": "<@AGNES> hello",
    }

    async def _run():
        await ev.dispatch_event(app=object(), event=event)

    asyncio.run(_run())

    matched = [
        r for r in caplog.records
        if "app_mention received" in r.message
    ]
    assert matched, f"expected app_mention log record; got {caplog.records!r}"
