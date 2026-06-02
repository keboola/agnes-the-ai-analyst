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

@pytest.fixture(autouse=True)
def _grant_chat_access(monkeypatch):
    """Chat is an RBAC resource (default-deny); the /chat route redirects users
    without the grant, and the nav link only shows with an explicit grant.
    These tests cover HTML rendering + the disabled-redirect + nav consistency,
    not the gate, so simulate "access granted" by patching both the route guard
    (`can_access`) and the nav-visibility check (`has_explicit_grant`). The
    default-deny gate is covered by test_chat_api::test_chat_requires_rbac_grant.
    """
    import app.auth.access as _access

    monkeypatch.setattr(_access, "can_access", lambda *a, **k: True)
    monkeypatch.setattr(_access, "has_explicit_grant", lambda *a, **k: True)


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
    # Template renders `Chat — {{ config.INSTANCE_NAME or 'Agnes' }}` — the
    # 'or Agnes' fallback fires here because the test env has no
    # instance.yaml. Substring assertion so a real INSTANCE_NAME (e.g.
    # "Agnes Dev") in a deployed env also passes.
    assert "<title>Chat — " in r.text
    # Page must go through _build_context so the Agnes chrome renders —
    # otherwise the four base stylesheets get empty href= and the nav
    # block short-circuits on `{% if session.user %}`. Pin both.
    assert 'class="app-header"' in r.text
    assert "/static/style-custom.css" in r.text
    assert 'class="chat-page-body"' in r.text


def test_chat_route_redirects_when_disabled(api_client_chat_disabled: TestClient, logged_in_user):
    r = api_client_chat_disabled.get("/chat", follow_redirects=False)
    assert r.status_code in (302, 307)


def test_can_chat_computed_without_conn_threaded():
    """Regression: the Chat nav link must render on EVERY page for a user with
    access — not only routes that thread a ``conn`` into ``_build_context``.

    The bug: ``_build_context`` set ``can_chat`` from the *passed* ``conn``, but
    most page routes call it with only ``user=`` (no conn). So ``can_chat`` was
    True on /chat + /dashboard (which thread conn) and False everywhere else
    (e.g. /marketplace), making the nav link flicker in and out as you moved
    between pages. The fix opens a short-lived system-db cursor when no conn is
    passed, so visibility is consistent. ``has_explicit_grant`` is patched True
    by the autouse fixture, so this isolates the conn-threading behavior.
    """
    from types import SimpleNamespace as _NS

    from starlette.requests import Request
    from app.web.router import _build_context

    # Minimal ASGI scope + an app whose state carries an enabled chat_config.
    app = _NS(state=_NS(chat_config=_NS(enabled=True)))
    scope = {
        "type": "http", "app": app, "method": "GET", "path": "/",
        "query_string": b"", "headers": [], "server": ("test", 80),
        "scheme": "http", "client": ("1.2.3.4", 9),
    }
    request = Request(scope)

    # Mirror the common route call: user supplied, but NO conn threaded.
    ctx = _build_context(request, user=TEST_USER)
    assert ctx["can_chat"] is True


def test_can_chat_hidden_for_admin_without_explicit_grant(monkeypatch):
    """The nav link tracks the explicit grant, NOT god-mode: an admin with no
    chat grant on any of their groups does not see the Chat link, even though
    `can_access` would let them reach /chat by URL. Pins the decoupling done
    in `_build_context` (has_explicit_grant, not can_access)."""
    from types import SimpleNamespace as _NS

    from starlette.requests import Request
    import app.auth.access as _access
    from app.web.router import _build_context

    # Override the autouse "granted" patch: no group holds a chat grant, but
    # god-mode WOULD grant effective access. The nav must still hide the link.
    monkeypatch.setattr(_access, "has_explicit_grant", lambda *a, **k: False)
    monkeypatch.setattr(_access, "can_access", lambda *a, **k: True)

    app = _NS(state=_NS(chat_config=_NS(enabled=True)))
    scope = {
        "type": "http", "app": app, "method": "GET", "path": "/",
        "query_string": b"", "headers": [], "server": ("test", 80),
        "scheme": "http", "client": ("1.2.3.4", 9),
    }
    request = Request(scope)

    admin_user = {"id": "admin1", "email": "admin@test.com", "is_admin": True}
    ctx = _build_context(request, user=admin_user)
    assert ctx["can_chat"] is False
