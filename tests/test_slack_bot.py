"""Tests for Slack identity binding (verification code flow).

Fixture note: the plan's spec names ``open_db`` / ``migrate`` but those don't
exist in src/db.py.  The real equivalents are:
  - ``duckdb.connect(":memory:")``   to open an in-memory connection
  - ``_ensure_schema(conn)``         to migrate it to the current version
"""
from pathlib import Path

import duckdb
import pytest
from src.db import _ensure_schema, get_system_db

from services.slack_bot.binding import (
    issue_verification_code,
    lookup_user_email,
    redeem_verification_code,
)


@pytest.fixture(autouse=True)
def _shared_slack_db(monkeypatch):
    """Slack identity binding now reads/writes through the repo factory
    (``users_repo()`` → ``get_system_db()``), not the test's standalone conn.
    Point both the test module's ``get_system_db`` and the factory's at one
    shared in-memory DuckDB so a user seeded in a test is visible to the
    binding lookup/redeem on the (default DuckDB) backend."""
    shared = duckdb.connect(":memory:")
    _ensure_schema(shared)
    monkeypatch.setattr(
        "tests.test_slack_bot.get_system_db", lambda: shared, raising=False
    )
    monkeypatch.setattr("src.repositories.get_system_db", lambda: shared)
    yield shared


@pytest.fixture
def conn():
    c = get_system_db()
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

    def get_slack_thread_session(self, slack_channel_id, slack_thread_ts):
        """Minimal impl: look up a chat_sessions row by channel+thread_ts."""
        from app.chat.types import ChatSession, Surface
        from datetime import datetime
        row = self._conn.execute(
            "SELECT id, user_email, surface, slack_channel_id, slack_thread_ts, "
            "title, started_at, last_message_at, message_count, archived "
            "FROM chat_sessions "
            "WHERE surface = 'slack_thread' "
            "AND slack_channel_id = ? AND slack_thread_ts = ? AND archived = FALSE",
            [slack_channel_id, slack_thread_ts],
        ).fetchone()
        if not row:
            return None
        return ChatSession(
            id=row[0], user_email=row[1],
            surface=Surface(row[2]),
            slack_channel_id=row[3], slack_thread_ts=row[4],
            title=row[5], started_at=row[6] or datetime.now(),
            last_message_at=row[7], message_count=row[8] or 0,
            archived=bool(row[9]),
        )


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
    assert sent[0][2].startswith(":warning:")  # intact emoji (leading colon)
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

    conn = get_system_db()
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
    # Now that _handle_dm wires chat_id, the sink uses post_thread_reply_with_blocks
    # for the first assistant turn. Capture those too.
    async def fake_post_blocks(ch, ts, text, blocks):
        sent.append((ch, ts, text))
        return "msg-1"
    monkeypatch.setattr(sink_mod, "post_thread_reply_with_blocks", fake_post_blocks)
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


class TestSinkBridgeChatId:
    def test_chat_id_stored_and_optional(self):
        from services.slack_bot.sink import SlackSinkBridge
        b1 = SlackSinkBridge(channel="C1", thread_ts="111.0", chat_id="sess_1")
        assert b1._chat_id == "sess_1"
        b2 = SlackSinkBridge(channel="C1", thread_ts="111.0")
        assert not b2._chat_id  # empty string or None — no chat_id means no buttons


class TestResolveBotUserId:
    def test_returns_user_id_on_ok(self, monkeypatch):
        import asyncio
        import services.slack_bot.identity as ident
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

        class _Resp:
            def json(self): return {"ok": True, "user_id": "U07BOT"}

        class _FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, headers=None):
                assert url.endswith("/auth.test")
                return _Resp()

        monkeypatch.setattr(ident.httpx, "AsyncClient", _FakeClient)
        assert asyncio.run(ident.resolve_bot_user_id()) == "U07BOT"

    def test_returns_none_on_not_ok(self, monkeypatch):
        import asyncio
        import services.slack_bot.identity as ident
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

        class _Resp:
            def json(self): return {"ok": False, "error": "invalid_auth"}

        class _FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, headers=None): return _Resp()

        monkeypatch.setattr(ident.httpx, "AsyncClient", _FakeClient)
        assert asyncio.run(ident.resolve_bot_user_id()) is None

    def test_returns_none_without_token(self, monkeypatch):
        import asyncio
        import services.slack_bot.identity as ident
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        assert asyncio.run(ident.resolve_bot_user_id()) is None


class TestSendEphemeralToUser:
    def test_posts_ephemeral_with_user_and_token(self, monkeypatch):
        import asyncio
        import services.slack_bot.sender as sender_mod

        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        captured = {}

        class _FakeResp:
            pass

        class _FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, headers=None, json=None):
                captured["url"] = url
                captured["headers"] = headers
                captured["json"] = json
                return _FakeResp()

        monkeypatch.setattr(sender_mod.httpx, "AsyncClient", _FakeClient)
        asyncio.run(sender_mod.send_ephemeral_to_user("C1", "U1", "nope"))
        assert captured["url"].endswith("/chat.postEphemeral")
        assert captured["json"] == {"channel": "C1", "user": "U1", "text": "nope"}
        assert captured["headers"]["Authorization"] == "Bearer xoxb-test"

    def test_no_token_is_noop(self, monkeypatch):
        import asyncio
        import services.slack_bot.sender as sender_mod
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        # Must not raise even though no HTTP client is patched.
        asyncio.run(sender_mod.send_ephemeral_to_user("C1", "U1", "nope"))


class TestStripBotMention:
    def test_strips_leading_mention(self):
        from services.slack_bot.events import _strip_bot_mention
        assert _strip_bot_mention("<@U07BOT> what is revenue?", "U07BOT") == "what is revenue?"

    def test_strips_mid_text_mention(self):
        from services.slack_bot.events import _strip_bot_mention
        assert _strip_bot_mention("hey <@U07BOT> hello", "U07BOT") == "hey  hello".strip()

    def test_no_bot_id_returns_trimmed(self):
        from services.slack_bot.events import _strip_bot_mention
        assert _strip_bot_mention("  hello  ", None) == "hello"

    def test_handles_angle_with_label(self):
        from services.slack_bot.events import _strip_bot_mention
        assert _strip_bot_mention("<@U07BOT|agnes> hi", "U07BOT") == "hi"


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


class _FakeApp:
    """Mimics the bits of `app` _handle_mention touches."""
    class _State:
        pass

    def __init__(self, conn, mgr, *, bot_user_id="U07BOT", public_url="https://example.com"):
        self.state = _FakeApp._State()
        self.state.chat_repo = _RepoStub(conn)
        self.state.chat_manager = mgr
        self.state.slack_bot_user_id = bot_user_id
        self.state.public_url = public_url


class _FakeMgr:
    def __init__(self):
        self.created = []
        self.sent = []
        self.attached = []
        self._live = []

    def list_live(self):
        return self._live

    async def create_session(self, **kw):
        from app.chat.types import ChatSession, Surface
        from datetime import datetime
        sess = ChatSession(
            id="sess_new",
            user_email=kw["user_email"],
            surface=kw["surface"],
            slack_channel_id=kw.get("slack_channel_id"),
            slack_thread_ts=kw.get("slack_thread_ts"),
            title=None,
            started_at=datetime.now(),
            last_message_at=None,
            message_count=0,
            archived=False,
        )
        self.created.append(sess)
        return sess

    async def attach(self, chat_id, sink):
        self.attached.append((chat_id, sink))

    async def send_user_message(self, chat_id, text):
        self.sent.append((chat_id, text))


def test_mention_bot_loop_guard_returns_silently(monkeypatch):
    import asyncio
    import services.slack_bot.events as ev
    posts = []
    monkeypatch.setattr(ev, "send_ephemeral_to_user", lambda *a, **k: posts.append(a))
    conn = get_system_db(); _ensure_schema(conn)
    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)
    asyncio.run(ev._handle_mention(app, {"bot_id": "B1", "channel": "C1", "ts": "1.0", "user": "U07BOT"}))
    assert posts == [] and mgr.created == []


def test_mention_self_user_loop_guard_returns_silently(monkeypatch):
    import asyncio
    import services.slack_bot.events as ev
    posts = []
    monkeypatch.setattr(ev, "send_ephemeral_to_user", lambda *a, **k: posts.append(a))
    conn = get_system_db(); _ensure_schema(conn)
    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)
    asyncio.run(ev._handle_mention(app, {"channel": "C1", "ts": "1.0", "user": "U07BOT", "text": "<@U07BOT> hi"}))
    assert posts == [] and mgr.created == []


def test_mention_not_allowlisted_ephemeral_deny(monkeypatch):
    import asyncio
    import services.slack_bot.events as ev
    from services.slack_bot.binding import _ensure_table
    posts = []
    async def _fake_ep(ch, u, txt): posts.append((ch, u, txt))
    monkeypatch.setattr(ev, "send_ephemeral_to_user", _fake_ep)
    conn = get_system_db(); _ensure_schema(conn); _ensure_table(conn)
    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)
    asyncio.run(ev._handle_mention(app, {"channel": "C_X", "ts": "1.0", "user": "U1", "text": "<@U07BOT> hi"}))
    assert posts and "isn't enabled" in posts[0][2]
    assert mgr.created == []


def test_mention_unbound_user_gets_code(monkeypatch):
    import asyncio
    import services.slack_bot.events as ev
    from services.slack_bot.binding import _ensure_table
    posts = []
    async def _fake_ep(ch, u, txt): posts.append((ch, u, txt))
    monkeypatch.setattr(ev, "send_ephemeral_to_user", _fake_ep)
    conn = get_system_db(); _ensure_schema(conn); _ensure_table(conn)
    gid = conn.execute("SELECT id FROM user_groups WHERE name='Everyone'").fetchone()[0]
    conn.execute(
        "INSERT INTO resource_grants(id, group_id, resource_type, resource_id) "
        "VALUES ('rg1', ?, 'slack_channel', 'C_OK')", [gid])
    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)
    asyncio.run(ev._handle_mention(app, {"channel": "C_OK", "ts": "1.0", "user": "U_NEW", "text": "<@U07BOT> hi"}))
    assert posts and "6-digit code" in posts[0][2]
    assert mgr.created == []


def _seed_bound_chat_user(conn, *, email="u@x", slack_id="U_OK"):
    """Seed a user bound to slack_id, in Everyone, with a CHAT grant.
    Primes the lazy users.slack_user_id column first (binding._ensure_table)."""
    from services.slack_bot.binding import _ensure_table
    _ensure_table(conn)  # adds users.slack_user_id if missing
    uid = f"uid_{slack_id}"
    conn.execute("DELETE FROM users WHERE email = ?", [email])
    conn.execute(
        "INSERT INTO users(id, email, name, slack_user_id) VALUES (?, ?, 'U', ?)",
        [uid, email, slack_id],
    )
    egid = conn.execute("SELECT id FROM user_groups WHERE name='Everyone'").fetchone()[0]
    conn.execute(
        "INSERT INTO user_group_members(user_id, group_id, source) VALUES (?, ?, 'system_seed')",
        [uid, egid],
    )
    conn.execute(
        "INSERT INTO resource_grants(id, group_id, resource_type, resource_id) "
        "VALUES ('rg_chat', ?, 'chat', 'chat') ON CONFLICT DO NOTHING", [egid])
    return uid


def _allow_channel(conn, channel="C_OK"):
    egid = conn.execute("SELECT id FROM user_groups WHERE name='Everyone'").fetchone()[0]
    conn.execute(
        "INSERT INTO resource_grants(id, group_id, resource_type, resource_id) "
        "VALUES ('rg_ch', ?, 'slack_channel', ?)", [egid, channel])


def test_mention_happy_path_creates_thread_and_sends(monkeypatch):
    import asyncio
    import services.slack_bot.events as ev
    monkeypatch.setattr(ev, "send_ephemeral_to_user", lambda *a, **k: None)
    conn = get_system_db(); _ensure_schema(conn)
    _seed_bound_chat_user(conn)
    _allow_channel(conn)
    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)
    asyncio.run(ev._handle_mention(app, {"channel": "C_OK", "ts": "9.1", "user": "U_OK", "text": "<@U07BOT> revenue?"}))
    assert len(mgr.created) == 1
    assert mgr.created[0].surface.value == "slack_thread"
    assert mgr.created[0].slack_thread_ts == "9.1"
    assert mgr.attached and mgr.attached[0][0] == "sess_new"
    assert mgr.sent and mgr.sent[0][1] == "revenue?"
    # The mention-path sink must carry the starter's owner + web_base so the
    # owner-gated Stop button and Continue-on-web link work on thread sessions
    # (regression: the sink was previously built without owner/web_base, which
    # made the Stop button encode an empty owner and always deny).
    sink = mgr.attached[0][1]
    assert sink._owner == "u@x"
    assert sink._web_base == "https://example.com"


def test_mention_ownership_reject_ephemeral(monkeypatch):
    import asyncio
    import services.slack_bot.events as ev
    posts = []
    async def _fake_ep(ch, u, txt): posts.append(txt)
    monkeypatch.setattr(ev, "send_ephemeral_to_user", _fake_ep)
    conn = get_system_db(); _ensure_schema(conn)
    _seed_bound_chat_user(conn, email="owner@x", slack_id="U_OWNER")
    _seed_bound_chat_user(conn, email="other@x", slack_id="U_OTHER")
    _allow_channel(conn)
    # pre-existing thread session owned by owner@x (column is started_at)
    conn.execute(
        "INSERT INTO chat_sessions(id, user_email, surface, slack_channel_id, "
        "slack_thread_ts, title, started_at) VALUES "
        "('s_owned', 'owner@x', 'slack_thread', 'C_OK', '9.2', NULL, current_timestamp)"
    )
    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)
    asyncio.run(ev._handle_mention(app, {"channel": "C_OK", "ts": "9.2", "user": "U_OTHER", "text": "<@U07BOT> hi"}))
    # owner has a bound slack id → rendered as <@U_OWNER>
    assert posts and "belongs to <@U_OWNER>" in posts[0]
    assert mgr.created == []


def test_mention_same_thread_reuses_session(monkeypatch):
    """A second mention in the same thread by the OWNER must NOT be rejected
    (no ownership reject) and proceeds to send. (Real dedup to a single row is
    ChatManager.create_session's job via get_slack_thread_session; the handler
    only enforces the owner check.)"""
    import asyncio
    import services.slack_bot.events as ev
    monkeypatch.setattr(ev, "send_ephemeral_to_user", lambda *a, **k: None)
    conn = get_system_db(); _ensure_schema(conn)
    _seed_bound_chat_user(conn)
    _allow_channel(conn)
    # existing session owned by the SAME user (column is started_at)
    conn.execute(
        "INSERT INTO chat_sessions(id, user_email, surface, slack_channel_id, "
        "slack_thread_ts, title, started_at) VALUES "
        "('s_mine', 'u@x', 'slack_thread', 'C_OK', '9.3', NULL, current_timestamp)"
    )
    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)
    asyncio.run(ev._handle_mention(app, {"channel": "C_OK", "ts": "9.3", "user": "U_OK", "text": "<@U07BOT> again"}))
    assert mgr.sent and mgr.sent[0][1] == "again"


def test_mention_attach_not_awaited_returns_under_budget(monkeypatch):
    """attach() blocks forever; the handler must still return promptly because
    attach is create_task'd, not awaited (3s-ack contract)."""
    import asyncio
    import services.slack_bot.events as ev
    monkeypatch.setattr(ev, "send_ephemeral_to_user", lambda *a, **k: None)
    conn = get_system_db(); _ensure_schema(conn)
    _seed_bound_chat_user(conn)
    _allow_channel(conn)

    blocker = asyncio.Event()  # never set

    class _BlockingMgr(_FakeMgr):
        async def attach(self, chat_id, sink):
            self.attached.append((chat_id, sink))
            await blocker.wait()  # would hang if awaited

    mgr = _BlockingMgr()
    app = _FakeApp(conn=conn, mgr=mgr)

    async def _run():
        await asyncio.wait_for(
            ev._handle_mention(app, {"channel": "C_OK", "ts": "9.4", "user": "U_OK", "text": "<@U07BOT> q"}),
            timeout=2.0,
        )
    asyncio.run(_run())
    assert mgr.sent  # handler reached step 9 despite attach blocking


def test_slack_app_mention_dispatches(monkeypatch):
    """`app_mention` events are dispatched to `_handle_mention` without error.

    The stub log-based check is replaced now that the full handler is
    implemented. This test verifies dispatch_event routes `app_mention` to
    the handler; the handler returns silently when the channel isn't
    allowlisted (default-deny) — no session is created, no exception raised.
    """
    import asyncio
    import services.slack_bot.events as ev

    posts = []
    async def _fake_ep(ch, u, txt): posts.append((ch, u, txt))
    monkeypatch.setattr(ev, "send_ephemeral_to_user", _fake_ep)

    conn = get_system_db(); _ensure_schema(conn)
    from services.slack_bot.binding import _ensure_table
    _ensure_table(conn)
    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)

    event = {
        "type": "app_mention", "channel": "C1", "thread_ts": "1.1",
        "user": "U999", "text": "<@U07BOT> hello",
    }

    asyncio.run(ev.dispatch_event(app=app, event=event))

    # Channel not allowlisted → ephemeral "isn't enabled" deny, no session created.
    assert posts and "isn't enabled" in posts[0][2]
    assert mgr.created == []
