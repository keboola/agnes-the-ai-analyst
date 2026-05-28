"""Tests for the /chat web route — HTML rendering and redirect-when-disabled.

Fixture pattern: build a minimal FastAPI app with the web router attached,
set app.state.chat_config manually, and override get_current_user.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth.dependencies import get_current_user


TEST_USER = {"id": "user1", "email": "alice@test.com", "is_admin": False}


def _make_app(*, chat_enabled: bool = True) -> FastAPI:
    """Build a minimal FastAPI test app with the web router attached."""
    from app.web.router import router as web_router

    app = FastAPI()
    app.include_router(web_router)

    # Wire chat_config so the /chat route can check .enabled
    app.state.chat_config = SimpleNamespace(enabled=chat_enabled)

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
# Tests (per Task 9.1 Step 1)
# ---------------------------------------------------------------------------

def test_chat_route_html(api_client: TestClient, logged_in_user):
    r = api_client.get("/chat")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "<title>Agnes — Chat</title>" in r.text


def test_chat_route_redirects_when_disabled(api_client_chat_disabled: TestClient, logged_in_user):
    r = api_client_chat_disabled.get("/chat", follow_redirects=False)
    assert r.status_code in (302, 307)
