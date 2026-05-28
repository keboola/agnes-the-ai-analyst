"""Tests for the chat REST API — POST/GET/DELETE sessions, 503 when disabled.

Fixture pattern: build a minimal FastAPI app with the chat router attached,
set up app.state manually (chat_manager + chat_repo), and override the
get_current_user dependency to inject a test user dict.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import duckdb
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.db import _ensure_schema
from app.chat.persistence import ChatRepository
from app.chat.config import ChatConfig
from app.chat.manager import ChatManager
from app.chat.types import Surface
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

    # Override auth so we don't need a running DuckDB system.db
    app.dependency_overrides[get_current_user] = lambda: TEST_USER

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
