"""Deep-link: /chat?session=<id> threads the param into the page DOM hook
without 404-ing on unknown/forbidden ids (RBAC is enforced later by the
session-scoped JS endpoints, not by the page route)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth.dependencies import get_current_user

TEST_USER = {"id": "user1", "email": "alice@test.com", "is_admin": False}


def _make_app(*, chat_enabled: bool = True) -> FastAPI:
    from app.web.router import router as web_router

    app = FastAPI()
    app.include_router(web_router)
    app.state.chat_config = SimpleNamespace(enabled=chat_enabled)
    app.dependency_overrides[get_current_user] = lambda: TEST_USER
    return app


@pytest.fixture(autouse=True)
def _grant_chat_access(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "sysdb"))
    import app.auth.access as _access

    monkeypatch.setattr(_access, "can_access", lambda *a, **k: True)
    monkeypatch.setattr(_access, "has_explicit_grant", lambda *a, **k: True)


@pytest.fixture
def api_client() -> TestClient:
    return TestClient(_make_app(chat_enabled=True))


def test_deep_link_param_renders_in_body_hook(api_client: TestClient):
    r = api_client.get("/chat?session=sess-abc123")
    assert r.status_code == 200
    assert 'data-initial-session="sess-abc123"' in r.text


def test_no_param_renders_empty_hook(api_client: TestClient):
    r = api_client.get("/chat")
    assert r.status_code == 200
    assert 'data-initial-session=""' in r.text


def test_unknown_session_id_does_not_404(api_client: TestClient):
    # The route NEVER validates the id — ownership is enforced by the
    # session-scoped endpoints the JS calls. Page always renders 200.
    r = api_client.get("/chat?session=does-not-exist-or-forbidden")
    assert r.status_code == 200
    assert 'data-initial-session="does-not-exist-or-forbidden"' in r.text
