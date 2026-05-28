"""WebSocket end-to-end test: REST session create → WS connect → user_msg
→ real subprocess (SubprocessProvider, unjailed) → fake-agent runner →
assistant_message back over the WS.

Reuses the fixture pattern from test_chat_api.py but wires a REAL
SubprocessProvider(require_isolation=False) and a real WorkdirManager
backed by tmp_path so subprocess actually spawns.
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.db import _ensure_schema
from app.chat.persistence import ChatRepository
from app.chat.config import ChatConfig
from app.chat.manager import ChatManager
from app.chat.subprocess_provider import SubprocessProvider
from app.chat.workdir import WorkdirManager
from app.auth.dependencies import get_current_user

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEST_USER = {"id": "user1", "email": "alice@test.com", "is_admin": False}

_BUNDLED_TEMPLATE = (
    Path(__file__).parent.parent / "app" / "initial_workspace_default"
)


def _make_real_manager(repo: ChatRepository, data_dir: Path) -> ChatManager:
    """Return a ChatManager wired with a real SubprocessProvider and WorkdirManager."""
    provider = SubprocessProvider(nsjail_path=None, require_isolation=False)
    workdir_mgr = WorkdirManager(
        data_dir=data_dir,
        repo=repo,
        bundled_template_dir=_BUNDLED_TEMPLATE,
        server_url="http://127.0.0.1:8000",
        agnes_version="test",
        get_marketplace_sha=lambda: "test-sha",
        get_template_status=lambda: None,
    )
    config = ChatConfig(enabled=True, concurrency_per_user=3)
    return ChatManager(
        provider=provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        config=config,
    )


def _make_real_app(data_dir: Path) -> FastAPI:
    """Build a minimal FastAPI app backed by a real subprocess provider."""
    from app.api.chat import router as chat_router

    app = FastAPI()
    app.include_router(chat_router)

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)

    mgr = _make_real_manager(repo, data_dir)
    app.state.chat_manager = mgr
    app.state.chat_repo = repo

    app.dependency_overrides[get_current_user] = lambda: TEST_USER
    return app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def logged_in_user():
    """Dummy fixture — auth is overridden via dependency_overrides."""
    return TEST_USER


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

def test_ws_token_streaming_with_fake_runner(tmp_path, logged_in_user, monkeypatch):
    """Full-stack WS smoke: real subprocess spawns runner in fake-agent mode
    and returns an assistant_message echoing the user text."""
    # Force the fake-agent runner so we don't need an Anthropic key.
    monkeypatch.setenv("AGNES_RUNNER_FAKE_AGENT", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "dev-secret-at-least-32-chars-long-aaaa")
    # Ensure the subprocess can import the app package from this worktree.
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parent.parent))

    # AGNES_RUNNER_FAKE_AGENT must reach the subprocess env.
    # SubprocessProvider._scrub_env only forwards the allowlist; patch it to
    # also pass through the fake-agent flag.
    import app.chat.subprocess_provider as _sp
    original_scrub = _sp._scrub_env

    def _scrub_with_fake_agent(env: dict) -> dict:
        result = original_scrub(env)
        if "AGNES_RUNNER_FAKE_AGENT" in env:
            result["AGNES_RUNNER_FAKE_AGENT"] = env["AGNES_RUNNER_FAKE_AGENT"]
        if "PYTHONPATH" in env:
            result["PYTHONPATH"] = env["PYTHONPATH"]
        return result

    monkeypatch.setattr(_sp, "_scrub_env", _scrub_with_fake_agent)

    app = _make_real_app(tmp_path)
    with TestClient(app, raise_server_exceptions=True) as api_client:
        create = api_client.post("/api/chat/sessions", json={"surface": "web"}).json()
        assert "ws_url" in create, f"unexpected response: {create}"

        with api_client.websocket_connect(create["ws_url"]) as ws:
            # Consume frames until we see runner_ready or ready
            first = ws.receive_json()
            assert first["type"] in ("ready", "runner_ready"), f"unexpected first frame: {first}"

            ws.send_json({"type": "user_msg", "text": "hello"})

            # Pump frames until assistant_message arrives (fake agent echoes
            # synchronously so it should be the very next frame or close to it).
            for _ in range(50):
                frame = ws.receive_json()
                if frame.get("type") == "assistant_message":
                    assert "hello" in frame["content"], (
                        f"expected 'hello' in content, got: {frame['content']!r}"
                    )
                    break
            else:
                raise AssertionError("never saw assistant_message after 50 frames")
