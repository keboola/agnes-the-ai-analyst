"""`POST /api/v1/persona/dispatch` SSE bridge and `/persona` demo page."""

from __future__ import annotations

import uuid

import pytest

from app.chat.config import ChatConfig


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class FakeManager:
    """Fakes the attach/stream seam for the Persona bridge.

    Mirrors ``tests/test_agent_sessions_api.py::FakeManager`` but emits the
    real runner frame field ``text`` (not ``content``) for token deltas, so
    ``app.api.agent_sse.frame_to_agui`` produces a non-empty ``TEXT_MESSAGE_CONTENT``
    delta.
    """

    def __init__(self, *, frames=None, attach_raises=None, send_raises=None):
        self.attached: list[str] = []
        self.detached: list[str] = []
        self.sent_messages: list[tuple[str, str, str | None]] = []
        self.attach_raises = attach_raises
        self.send_raises = send_raises
        self.frames = frames or [
            {"type": "ready"},
            {"type": "token", "text": "Hello"},
            {"type": "done"},
        ]
        self.created_sessions: list[dict] = []

    async def create_session(self, *, user_email, surface, agent_id=None, **kwargs):
        from src.repositories import chat_session_repo

        self.created_sessions.append({"user_email": user_email, "agent_id": agent_id})
        return chat_session_repo().create_session(
            user_email=user_email,
            surface=surface,
            agent_id=agent_id,
        )

    async def attach(self, chat_id, sink, is_primary: bool = True) -> None:
        if self.attach_raises is not None:
            raise self.attach_raises
        self.attached.append(chat_id)
        for frame in self.frames:
            await sink.send_json(frame)

    async def send_user_message(self, chat_id, text, *, sender_email=None, **kwargs):
        if self.send_raises is not None:
            raise self.send_raises
        self.sent_messages.append((chat_id, text, sender_email))

    async def detach_sink(self, chat_id, sink) -> None:
        self.detached.append(chat_id)


class TestPersonaRoutes:
    """Route coverage for the Persona SSE bridge demo page.

    Detailed behavioral coverage lives in the module-level tests below.
    """

    COVERED_ROUTES = {
        "GET /persona",
        "POST /api/v1/persona/dispatch",
    }


@pytest.fixture
def persona_env(seeded_app, monkeypatch):
    """A seeded TestClient with chat enabled and a default agent for admin1."""
    client = seeded_app["client"]
    client.app.state.chat_config = ChatConfig(enabled=True)

    from src.repositories import agents_repo

    agent_id = str(uuid.uuid4())
    agents_repo().create(
        id=agent_id,
        owner_user_id="admin1",
        name="Support Bot",
        slug="support-bot",
        is_default=True,
    )

    import app.api.persona as persona_api

    def _patch_manager(manager: FakeManager) -> FakeManager:
        monkeypatch.setattr(persona_api, "get_current_chat_manager", lambda: manager)
        return manager

    return {
        "client": client,
        "admin_token": seeded_app["admin_token"],
        "viewer_token": seeded_app["viewer_token"],
        "patch": _patch_manager,
        "agent_id": agent_id,
    }


# ---------------------------------------------------------------------------
# GET /persona
# ---------------------------------------------------------------------------


def test_persona_page_renders(persona_env):
    r = persona_env["client"].get("/persona", headers=_auth(persona_env["admin_token"]))
    assert r.status_code == 200, r.text
    assert "persona-root" in r.text
    assert "@runtypelabs/persona@4.4.0" in r.text
    assert "/api/v1/persona/dispatch" in r.text


def test_persona_page_chat_disabled_redirects(persona_env):
    persona_env["client"].app.state.chat_config = ChatConfig(enabled=False)
    r = persona_env["client"].get("/persona", headers=_auth(persona_env["admin_token"]), follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/"


def test_persona_page_no_chat_grant_redirects(persona_env):
    r = persona_env["client"].get("/persona", headers=_auth(persona_env["viewer_token"]), follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/"


# ---------------------------------------------------------------------------
# POST /api/v1/persona/dispatch
# ---------------------------------------------------------------------------


def test_persona_dispatch_streams_events(persona_env):
    manager = persona_env["patch"](FakeManager())
    r = persona_env["client"].post(
        "/api/v1/persona/dispatch",
        json={"messages": [{"role": "user", "content": "hello"}]},
        headers=_auth(persona_env["admin_token"]),
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    body = r.text
    assert "event: execution_start" in body
    assert "event: turn_start" in body
    assert "event: text_start" in body
    assert "event: text_delta" in body
    assert '"delta": "Hello"' in body
    assert "event: text_complete" in body
    assert "event: turn_complete" in body
    assert "event: execution_complete" in body
    assert len(manager.sent_messages) == 1
    assert manager.sent_messages[0][1] == "hello"


def test_persona_dispatch_requires_auth(persona_env):
    r = persona_env["client"].post(
        "/api/v1/persona/dispatch",
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    assert r.status_code == 401


def test_persona_dispatch_chat_disabled(persona_env):
    persona_env["client"].app.state.chat_config = ChatConfig(enabled=False)
    r = persona_env["client"].post(
        "/api/v1/persona/dispatch",
        json={"messages": [{"role": "user", "content": "hello"}]},
        headers=_auth(persona_env["admin_token"]),
    )
    assert r.status_code == 503
    assert "chat_disabled" in r.text


def test_persona_dispatch_no_chat_grant(persona_env):
    r = persona_env["client"].post(
        "/api/v1/persona/dispatch",
        json={"messages": [{"role": "user", "content": "hello"}]},
        headers=_auth(persona_env["viewer_token"]),
    )
    assert r.status_code == 403


def test_persona_dispatch_agent_slug_resolution(persona_env):
    from src.repositories import agents_repo

    other_id = str(uuid.uuid4())
    agents_repo().create(
        id=other_id,
        owner_user_id="admin1",
        name="Other Agent",
        slug="other-agent",
    )
    manager = persona_env["patch"](FakeManager())
    r = persona_env["client"].post(
        "/api/v1/persona/dispatch",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "agent_slug": "other-agent",
        },
        headers=_auth(persona_env["admin_token"]),
    )
    assert r.status_code == 200, r.text
    assert manager.created_sessions[0]["agent_id"] == other_id


def test_persona_dispatch_agent_slug_in_metadata(persona_env):
    from src.repositories import agents_repo

    meta_id = str(uuid.uuid4())
    agents_repo().create(
        id=meta_id,
        owner_user_id="admin1",
        name="Meta Agent",
        slug="meta-agent",
    )
    manager = persona_env["patch"](FakeManager())
    r = persona_env["client"].post(
        "/api/v1/persona/dispatch",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "metadata": {"agent_slug": "meta-agent"},
        },
        headers=_auth(persona_env["admin_token"]),
    )
    assert r.status_code == 200, r.text
    assert manager.created_sessions[0]["agent_id"] == meta_id


def test_persona_dispatch_no_agent_returns_409(persona_env):
    from src.repositories import agents_repo

    repo = agents_repo()
    for a in repo.list_for_user("admin1"):
        repo.soft_delete(a["id"])

    manager = persona_env["patch"](FakeManager())
    r = persona_env["client"].post(
        "/api/v1/persona/dispatch",
        json={"messages": [{"role": "user", "content": "hello"}]},
        headers=_auth(persona_env["admin_token"]),
    )
    assert r.status_code == 409
