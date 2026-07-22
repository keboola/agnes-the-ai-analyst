"""Tests for the chat REST API — POST/GET/DELETE sessions, 503 when disabled.

Fixture pattern: build a minimal FastAPI app with the chat router attached,
set up app.state manually (chat_manager + chat_repo), and override the
get_current_user dependency to inject a test user dict.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import duckdb
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from src.db import _ensure_schema
from app.chat.persistence import ChatRepository
from app.chat.config import ChatConfig
from app.chat.manager import ChatManager
from app.auth.dependencies import get_current_user


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEST_USER = {"id": "user1", "email": "alice@test.com", "is_admin": False}


def _make_mock_manager(repo: ChatRepository) -> ChatManager:
    """Return a ChatManager wired to a real repo but with a no-op provider."""
    from app.chat.workdir import WorkdirManager

    provider = MagicMock()
    provider.spawn = AsyncMock()

    workdir_mgr = MagicMock(spec=WorkdirManager)
    workdir_mgr.ensure_user_workdir = MagicMock()
    workdir_mgr.prepare_session_dir = MagicMock(return_value="/tmp/fake")

    config = ChatConfig(enabled=True, concurrency_per_user=3)
    return ChatManager(
        provider=provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        config=config,
    )


def _make_app(*, chat_enabled: bool = True) -> FastAPI:
    """Build a minimal FastAPI test app with the chat router attached."""
    from app.api.chat import router as chat_router

    app = FastAPI()
    app.include_router(chat_router)

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)

    if chat_enabled:
        mgr = _make_mock_manager(repo)
        app.state.chat_manager = mgr
    # When chat_enabled=False we intentionally leave chat_manager absent.

    app.state.chat_repo = repo

    # Override auth so we don't need a running DuckDB system.db. Chat is now an
    # RBAC resource, so the endpoints depend on ``require_chat_access`` (which
    # internally resolves the user + checks the grant). Override that gate to
    # *delegate* to whatever get_current_user returns — skipping only the
    # access check (these tests exercise endpoint behavior, and some switch the
    # user mid-test). Default-deny is covered by test_chat_requires_rbac_grant.
    from app.api.chat import require_chat_access

    async def _granted_user(user: dict = Depends(get_current_user)) -> dict:
        return user

    app.dependency_overrides[get_current_user] = lambda: TEST_USER
    app.dependency_overrides[require_chat_access] = _granted_user

    return app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def api_client() -> TestClient:
    return TestClient(_make_app(chat_enabled=True))


@pytest.fixture
def api_client_chat_disabled() -> TestClient:
    return TestClient(_make_app(chat_enabled=False))


@pytest.fixture
def logged_in_user():
    """Dummy fixture referenced by plan tests — value unused, auth is overridden."""
    return TEST_USER


# ---------------------------------------------------------------------------
# Tests (5 per plan Step 1)
# ---------------------------------------------------------------------------


def test_create_web_session(api_client: TestClient, logged_in_user):
    r = api_client.post("/api/chat/sessions", json={"surface": "web"})
    assert r.status_code == 201
    data = r.json()
    assert data["id"].startswith("chat_")
    assert "/stream" in data["ws_url"]
    assert data["ws_ticket"]


def test_list_sessions(api_client: TestClient, logged_in_user):
    api_client.post("/api/chat/sessions", json={"surface": "web"})
    r = api_client.get("/api/chat/sessions")
    assert r.status_code == 200
    arr = r.json()
    assert len(arr) == 1
    assert arr[0]["surface"] == "web"


def test_create_session_accepts_known_profile(api_client: TestClient, logged_in_user):
    r = api_client.post("/api/chat/sessions", json={"surface": "web", "profile": "data-package-builder"})
    assert r.status_code == 201, r.text


def test_create_session_rejects_unknown_profile(api_client: TestClient, logged_in_user):
    r = api_client.post("/api/chat/sessions", json={"surface": "web", "profile": "nope"})
    assert r.status_code == 400
    assert r.json()["detail"]["kind"] == "unknown_profile"


def test_get_messages_empty(api_client: TestClient, logged_in_user):
    c = api_client.post("/api/chat/sessions", json={"surface": "web"}).json()
    r = api_client.get(f"/api/chat/sessions/{c['id']}/messages")
    assert r.status_code == 200
    assert r.json() == []


def test_archive_session(api_client: TestClient, logged_in_user):
    c = api_client.post("/api/chat/sessions", json={"surface": "web"}).json()
    r = api_client.delete(f"/api/chat/sessions/{c['id']}")
    assert r.status_code == 204
    r2 = api_client.get("/api/chat/sessions")
    assert r2.json() == []  # archived sessions excluded


def test_create_when_disabled(api_client_chat_disabled: TestClient, logged_in_user):
    r = api_client_chat_disabled.post("/api/chat/sessions", json={"surface": "web"})
    assert r.status_code == 503
    assert r.json()["detail"]["kind"] == "chat_disabled"


def test_reissue_ticket_for_existing_session(api_client: TestClient, logged_in_user):
    """``POST /sessions/{id}/ticket`` mints a fresh WS ticket against the
    SAME chat_id — used by the frontend when the user clicks an old
    conversation in the sidebar after their WS dropped. Resuming via
    the existing session preserves message history threading. Without
    this endpoint the frontend can only ``POST /sessions`` which creates
    a brand-new session each time, defeating the point of the sidebar."""
    created = api_client.post("/api/chat/sessions", json={"surface": "web"}).json()
    chat_id = created["id"]
    original_ticket = created["ws_ticket"]

    r = api_client.post(f"/api/chat/sessions/{chat_id}/ticket")
    assert r.status_code == 201
    body = r.json()
    assert body["id"] == chat_id
    assert body["ws_ticket"]
    assert body["ws_ticket"] != original_ticket  # fresh
    assert body["ws_url"].startswith(f"/api/chat/sessions/{chat_id}/stream?ticket=")


def test_reissue_ticket_404_for_unknown_session(api_client: TestClient, logged_in_user):
    r = api_client.post("/api/chat/sessions/chat_nonexistent/ticket")
    assert r.status_code == 404


def test_reissue_ticket_404_for_other_users_session(api_client: TestClient, logged_in_user):
    """Ticket re-issue is auth-scoped — Alice cannot mint a ticket for Bob's
    chat. The session_email check inside the handler matches ``get_session``
    against ``user["email"]`` and 404s on mismatch (same shape as the
    messages endpoint, so we don't disclose existence)."""
    created = api_client.post("/api/chat/sessions", json={"surface": "web"}).json()
    chat_id = created["id"]

    # Re-override auth as a DIFFERENT user; ticket endpoint must refuse.
    app = api_client.app
    app.dependency_overrides[get_current_user] = lambda: {
        "id": "user2",
        "email": "bob@test.com",
        "is_admin": False,
    }
    try:
        r = api_client.post(f"/api/chat/sessions/{chat_id}/ticket")
        assert r.status_code == 404
    finally:
        # Restore Alice for any subsequent tests sharing the fixture.
        app.dependency_overrides[get_current_user] = lambda: TEST_USER


# ---------------------------------------------------------------------------
# RBAC gate — chat is default-deny
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Task 10: ws disconnect detaches (not kills) + paused flag in session list
# ---------------------------------------------------------------------------


def _make_app_with_fake_provider() -> "FastAPI":
    """Like _make_app but wires a real FakeProvider so attach/detach_sink work."""
    from fastapi import FastAPI
    from app.api.chat import router as chat_router
    from app.chat.config import ChatConfig
    from app.chat.workdir import WorkdirManager

    import duckdb
    from src.db import _ensure_schema
    from tests.chat_fakes import FakeProvider

    app = FastAPI()
    app.include_router(chat_router)

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)

    fake_provider = FakeProvider()
    workdir_mgr = MagicMock(spec=WorkdirManager)
    workdir_mgr.ensure_user_workdir = MagicMock()
    workdir_mgr.prepare_session_dir = MagicMock(return_value="/tmp/fake")

    config = ChatConfig(
        enabled=True,
        concurrency_per_user=3,
        on_detach="pause",
        detach_linger_seconds=0,
    )
    mgr = ChatManager(
        provider=fake_provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        config=config,
    )
    app.state.chat_manager = mgr
    app.state.chat_repo = repo
    app.state._fake_provider = fake_provider

    from app.api.chat import require_chat_access
    from app.auth.dependencies import get_current_user

    async def _granted_user(user: dict = Depends(get_current_user)) -> dict:
        return user

    app.dependency_overrides[get_current_user] = lambda: TEST_USER
    app.dependency_overrides[require_chat_access] = _granted_user
    return app


@pytest.fixture
def fake_provider_client():
    return TestClient(_make_app_with_fake_provider())


def test_ws_disconnect_detaches_but_does_not_kill(fake_provider_client: TestClient):
    """After WS closes, the manager session stays in _live (ACTIVE or linger),
    the runner handle is not killed — only the sink is detached."""
    app = fake_provider_client.app
    mgr = app.state.chat_manager
    provider = app.state._fake_provider

    created = fake_provider_client.post("/api/chat/sessions", json={"surface": "web"}).json()
    chat_id = created["id"]
    ticket_resp = fake_provider_client.post(f"/api/chat/sessions/{chat_id}/ticket").json()
    ws_url = ticket_resp["ws_url"]

    with fake_provider_client.websocket_connect(ws_url) as ws:
        # Drain the ready frame emitted by _seat_sink.
        frame = ws.receive_json()
        assert frame["type"] == "ready"
        # WS disconnects here (context manager exit).

    # Session must still be in the live registry (not killed).
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        live = mgr._live.get(chat_id)
        assert live is not None, "session was removed from _live on WS close — should stay"
        # Handle must not have been killed (linger_task may or may not have fired
        # yet, but if it fired with linger=0 and paused, handle becomes None after
        # pause — the important invariant is we never called handle.kill()).
        provider_handle = provider.spawned[0] if provider.spawned else None
        if provider_handle is not None:
            assert not provider_handle.killed, "handle was hard-killed on WS disconnect"
    finally:
        loop.close()


def test_sessions_list_exposes_paused(fake_provider_client: TestClient):
    """GET /api/chat/sessions includes 'paused': true for paused sessions."""
    created = fake_provider_client.post("/api/chat/sessions", json={"surface": "web"}).json()
    chat_id = created["id"]

    # Directly set paused_at on the repo row to simulate a paused session.
    from datetime import datetime, timezone

    repo = fake_provider_client.app.state.chat_repo
    repo.set_sandbox_paused_at(chat_id, datetime.now(timezone.utc))

    r = fake_provider_client.get("/api/chat/sessions")
    assert r.status_code == 200
    sessions = r.json()
    assert len(sessions) == 1
    assert sessions[0]["paused"] is True


def test_sessions_list_not_paused_for_active(fake_provider_client: TestClient):
    """GET /api/chat/sessions includes 'paused': false for non-paused sessions."""
    fake_provider_client.post("/api/chat/sessions", json={"surface": "web"})
    r = fake_provider_client.get("/api/chat/sessions")
    assert r.status_code == 200
    sessions = r.json()
    assert len(sessions) == 1
    assert sessions[0]["paused"] is False


def test_chat_requires_rbac_grant():
    """Default-deny: a user with no chat grant (and not admin) is refused 403
    by the chat API. This is the whole-feature RBAC gate — chat is off for
    everyone until an admin grants `(group, chat, chat)`."""
    from app.api.chat import router as chat_router
    from app.auth.dependencies import _get_db

    app = FastAPI()
    app.include_router(chat_router)

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    app.state.chat_repo = repo
    app.state.chat_manager = _make_mock_manager(repo)

    # Real require_chat_access (NOT overridden): the user belongs to no group
    # with a chat grant, so can_access returns False.
    app.dependency_overrides[get_current_user] = lambda: TEST_USER
    app.dependency_overrides[_get_db] = lambda: conn

    client = TestClient(app)
    for method, path in [
        ("post", "/api/chat/sessions"),
        ("get", "/api/chat/sessions"),
    ]:
        r = client.request(method, path, json={"surface": "web"})
        assert r.status_code == 403, f"{method} {path} -> {r.status_code}"


# ---------------------------------------------------------------------------
# GET/PUT /api/chat/journey — per-user onboarding journey state
# ---------------------------------------------------------------------------


def test_get_journey_defaults(api_client: TestClient, logged_in_user):
    from src.repositories import user_journey_repo

    user_journey_repo().reset(TEST_USER["id"])
    r = api_client.get("/api/chat/journey")
    assert r.status_code == 200
    assert r.json() == {
        "first_asked": False,
        "stack_setup_done": False,
        "explored_stack": False,
        "catalog_discovered": False,
        "use_anywhere": False,
        "onboarded": False,
        "successful_answers": 0,
    }


def test_put_journey_partial_update(api_client: TestClient, logged_in_user):
    from src.repositories import user_journey_repo

    user_journey_repo().reset(TEST_USER["id"])
    r = api_client.put("/api/chat/journey", json={"first_asked": True})
    assert r.status_code == 200
    assert r.json()["first_asked"] is True
    assert r.json()["onboarded"] is False

    r2 = api_client.put("/api/chat/journey", json={"onboarded": True, "successful_answers": 2})
    assert r2.status_code == 200
    assert r2.json()["first_asked"] is True  # previous update preserved
    assert r2.json()["onboarded"] is True
    assert r2.json()["successful_answers"] == 2

    r3 = api_client.get("/api/chat/journey")
    assert r3.json() == r2.json()


def test_journey_requires_rbac_grant():
    """Same default-deny gate as the rest of chat — no grant means 403."""
    from app.api.chat import router as chat_router
    from app.auth.dependencies import _get_db

    app = FastAPI()
    app.include_router(chat_router)

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    app.state.chat_repo = repo
    app.state.chat_manager = _make_mock_manager(repo)

    app.dependency_overrides[get_current_user] = lambda: TEST_USER
    app.dependency_overrides[_get_db] = lambda: conn

    client = TestClient(app)
    r = client.get("/api/chat/journey")
    assert r.status_code == 403
