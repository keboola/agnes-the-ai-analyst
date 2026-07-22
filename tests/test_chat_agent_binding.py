"""Task 6: `chat_sessions.agent_id` threading + `Surface.API` + default-agent
web attribution.

(a) Repo-level roundtrip on the DuckDB ``ChatRepository`` — create a session
with an explicit ``agent_id``, read it back; a NULL ``agent_id`` row hydrates
to ``None``.
(b) API-level — POST the web session-create endpoint and assert the
persisted row's ``agent_id`` equals the caller's default agent id (the
route resolves ``agents_repo().get_or_create_default(user["id"])`` and
threads it through ``ChatManager.create_session``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import duckdb
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.chat.persistence import ChatRepository
from app.chat.types import Surface
from src.db import _ensure_schema

# ---------------------------------------------------------------------------
# (a) Repo-level roundtrip
# ---------------------------------------------------------------------------


@pytest.fixture
def repo() -> ChatRepository:
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    return ChatRepository(conn)


def test_create_session_persists_agent_id(repo: ChatRepository) -> None:
    s = repo.create_session(user_email="a@x.com", surface=Surface.WEB, agent_id="a1")
    assert s.agent_id == "a1"
    fetched = repo.get_session(s.id)
    assert fetched is not None
    assert fetched.agent_id == "a1"


def test_create_session_without_agent_id_hydrates_none(repo: ChatRepository) -> None:
    s = repo.create_session(user_email="a@x.com", surface=Surface.WEB)
    assert s.agent_id is None
    fetched = repo.get_session(s.id)
    assert fetched is not None
    assert fetched.agent_id is None


def test_surface_api_exists() -> None:
    assert Surface.API.value == "api"


# ---------------------------------------------------------------------------
# (b) API-level — web session-create attributes to the caller's default agent
# ---------------------------------------------------------------------------

TEST_USER = {"id": "webuser1", "email": "webuser1@test.com", "is_admin": False}


def _make_app(tmp_path, monkeypatch) -> tuple[FastAPI, ChatRepository]:
    """Minimal FastAPI app with the chat router attached, wired to a
    DATA_DIR-backed system DuckDB (so agents_repo(), which the route calls,
    and the chat repo share the same underlying database — mirrors the
    tests/db_pg/test_chat_pg.py::_chat_env pattern)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for sub in ("extracts", "analytics", "state", "notifications"):
        (tmp_path / sub).mkdir(exist_ok=True)

    from app.api.chat import require_chat_access
    from app.api.chat import router as chat_router
    from app.auth.dependencies import get_current_user
    from app.chat.config import ChatConfig
    from app.chat.manager import ChatManager
    from app.chat.workdir import WorkdirManager
    from src.db import get_system_db

    conn = get_system_db()
    repo = ChatRepository(conn)

    provider = MagicMock()
    provider.spawn = AsyncMock()
    workdir_mgr = MagicMock(spec=WorkdirManager)
    workdir_mgr.ensure_user_workdir = MagicMock()
    workdir_mgr.prepare_session_dir = MagicMock(return_value=str(tmp_path))

    mgr = ChatManager(
        provider=provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        config=ChatConfig(enabled=True, concurrency_per_user=3),
    )

    app = FastAPI()
    app.include_router(chat_router)
    app.state.chat_manager = mgr
    app.state.chat_repo = repo

    async def _granted_user(user: dict = Depends(get_current_user)) -> dict:
        return user

    app.dependency_overrides[get_current_user] = lambda: TEST_USER
    app.dependency_overrides[require_chat_access] = _granted_user

    return app, repo


@pytest.fixture
def agent_binding_env(tmp_path, monkeypatch):
    app, repo = _make_app(tmp_path, monkeypatch)
    return TestClient(app), repo


def test_web_session_create_attributes_to_default_agent(agent_binding_env) -> None:
    client, repo = agent_binding_env
    from src.repositories import agents_repo

    expected_default = agents_repo().get_or_create_default(TEST_USER["id"])["id"]

    r = client.post("/api/chat/sessions", json={})
    assert r.status_code == 201
    session_id = r.json()["id"]

    persisted = repo.get_session(session_id)
    assert persisted is not None
    assert persisted.agent_id == expected_default
