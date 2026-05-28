"""HTTP-level tests for /api/chat.

The SSE streaming path is exercised by mocking ``_build_anthropic_client``
to return a fake client. We don't go end-to-end on streaming bytes —
``test_chat_loop.py`` already covers the event sequence; here we focus
on the surfaces specific to the HTTP layer (auth, persistence side
effects, session-ownership boundaries).
"""

from dataclasses import dataclass, field

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# Fake Anthropic client (re-used from test_chat_loop)
# --------------------------------------------------------------------------- #


@dataclass
class _FakeBlock:
    type: str
    text: str = ""
    name: str = ""
    id: str = ""
    input: dict = field(default_factory=dict)


@dataclass
class _FakeUsage:
    input_tokens: int = 10
    output_tokens: int = 4


@dataclass
class _FakeMessage:
    content: list
    usage: _FakeUsage = field(default_factory=_FakeUsage)
    stop_reason: str = "end_turn"


class _FakeStreamContext:
    def __init__(self, text_chunks, final_message):
        self._chunks = text_chunks
        self._final = final_message

    async def __aenter__(self):
        async def _gen():
            for c in self._chunks:
                yield c
        self.text_stream = _gen()
        return self

    async def __aexit__(self, *exc):
        return None

    async def get_final_message(self):
        return self._final


class _FakeMessages:
    def __init__(self, scripted):
        self._scripted = list(scripted)

    def stream(self, **kwargs):
        chunks, msg = self._scripted.pop(0)
        return _FakeStreamContext(chunks, msg)


class _FakeClient:
    def __init__(self, scripted):
        self.messages = _FakeMessages(scripted)


def _terminal_text(text: str) -> tuple[list[str], _FakeMessage]:
    return (list(text), _FakeMessage(content=[_FakeBlock(type="text", text=text)]))


# --------------------------------------------------------------------------- #
# Session CRUD
# --------------------------------------------------------------------------- #


class TestSessionsList:
    def test_empty_for_new_user(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/api/chat/sessions", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        assert resp.json() == {"sessions": []}

    def test_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/api/chat/sessions")
        assert resp.status_code == 401


class TestSessionGet:
    def test_returns_404_for_unknown(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get(
            "/api/chat/sessions/chat_does_not_exist",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 404


class TestSessionArchive:
    def test_returns_404_for_unknown(self, seeded_app):
        c = seeded_app["client"]
        resp = c.delete(
            "/api/chat/sessions/chat_does_not_exist",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# POST /api/chat (SSE)
# --------------------------------------------------------------------------- #


class TestChatTurn:
    def test_creates_session_and_persists_messages(self, seeded_app, monkeypatch):
        """First request with no session_id creates a fresh session and
        returns a stream that includes the new session_id in the done event."""
        scripted = [_terminal_text("Hello!")]
        monkeypatch.setattr(
            "app.api.chat._build_anthropic_client",
            lambda: _FakeClient(scripted),
        )
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        with c.stream("POST", "/api/chat", json={"message": "hi"}, headers=_auth(token)) as resp:
            assert resp.status_code == 200
            body = b"".join(resp.iter_bytes()).decode()
        assert "event: token" in body
        assert "event: assistant_message" in body
        assert "event: done" in body
        # Session row exists in DB and is owned by the caller.
        sessions = c.get("/api/chat/sessions", headers=_auth(token)).json()["sessions"]
        assert len(sessions) == 1
        sid = sessions[0]["id"]
        # Detail endpoint returns both user message + assistant message.
        detail = c.get(f"/api/chat/sessions/{sid}", headers=_auth(token)).json()
        roles = [m["role"] for m in detail["messages"]]
        assert roles == ["user", "assistant"]
        assert detail["messages"][0]["content"] == "hi"
        assert detail["messages"][1]["content"] == "Hello!"
        assert detail["messages"][1]["tokens_in"] == 10
        assert detail["messages"][1]["tokens_out"] == 4

    def test_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post("/api/chat", json={"message": "hi"})
        assert resp.status_code == 401

    def test_continues_existing_session(self, seeded_app, monkeypatch):
        scripted = [_terminal_text("First"), _terminal_text("Second")]
        monkeypatch.setattr(
            "app.api.chat._build_anthropic_client",
            lambda: _FakeClient(scripted),
        )
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        with c.stream("POST", "/api/chat", json={"message": "one"}, headers=_auth(token)) as resp:
            list(resp.iter_bytes())
        sid = c.get("/api/chat/sessions", headers=_auth(token)).json()["sessions"][0]["id"]
        with c.stream(
            "POST", "/api/chat",
            json={"message": "two", "session_id": sid},
            headers=_auth(token),
        ) as resp:
            list(resp.iter_bytes())
        detail = c.get(f"/api/chat/sessions/{sid}", headers=_auth(token)).json()
        # 4 messages: user/assistant for turn1, user/assistant for turn2.
        assert [m["role"] for m in detail["messages"]] == [
            "user", "assistant", "user", "assistant",
        ]

    def test_cannot_post_to_other_users_session(self, seeded_app, monkeypatch):
        """Alice creates a session, Bob tries to use her session_id —
        the server treats it as a 404 (don't leak existence)."""
        scripted = [_terminal_text("Hi")]
        monkeypatch.setattr(
            "app.api.chat._build_anthropic_client",
            lambda: _FakeClient(scripted),
        )
        c = seeded_app["client"]
        with c.stream(
            "POST", "/api/chat", json={"message": "hi"},
            headers=_auth(seeded_app["analyst_token"]),
        ) as resp:
            list(resp.iter_bytes())
        alice_sid = c.get(
            "/api/chat/sessions",
            headers=_auth(seeded_app["analyst_token"]),
        ).json()["sessions"][0]["id"]
        # Bob (viewer) tries to continue Alice's (analyst) session.
        resp = c.post(
            "/api/chat",
            json={"message": "intrude", "session_id": alice_sid},
            headers=_auth(seeded_app["viewer_token"]),
        )
        assert resp.status_code == 404

    def test_missing_api_key_returns_500(self, seeded_app, monkeypatch):
        """If no LLM key is configured, /api/chat responds with a clear
        500 rather than crashing mid-stream."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        c = seeded_app["client"]
        resp = c.post(
            "/api/chat",
            json={"message": "hi"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 500
        assert "ANTHROPIC_API_KEY" in resp.json()["detail"]
