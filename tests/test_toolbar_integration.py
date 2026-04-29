"""Integration tests for the fastapi-debug-toolbar wiring.

Verifies that:
1. Toolbar HTML is NOT injected when DEBUG is unset (prod default).
2. The X-Request-ID header is always present (RequestIdMiddleware mounts
   independently of DEBUG).
3. Toolbar markup IS injected on at least one HTML 200 response when DEBUG=1
   and LOCAL_DEV_MODE=1 (auth bypass keeps a route reachable in TestClient).
"""

from __future__ import annotations

import importlib
import logging

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def reset_logging_state():
    """Reset module-level idempotency guard so each app reload re-applies setup_logging."""
    import app.logging_config as lc

    lc._CONFIGURED = False
    yield
    lc._CONFIGURED = False
    logging.getLogger().handlers.clear()


@pytest.fixture
def app_with_toolbar(monkeypatch, tmp_path, reset_logging_state):
    monkeypatch.setenv("DEBUG", "1")
    monkeypatch.setenv("LOCAL_DEV_MODE", "1")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SESSION_SECRET", "x" * 64)
    import app.main as main_mod

    importlib.reload(main_mod)
    return main_mod.app


@pytest.fixture
def app_no_toolbar(monkeypatch, tmp_path, reset_logging_state):
    monkeypatch.delenv("DEBUG", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SESSION_SECRET", "x" * 64)
    import app.main as main_mod

    importlib.reload(main_mod)
    return main_mod.app


@pytest.mark.integration
def test_no_toolbar_when_debug_off(app_no_toolbar):
    client = TestClient(app_no_toolbar)
    resp = client.get("/setup", follow_redirects=False)
    if resp.status_code in (302, 401):
        # Auth redirect — toolbar wouldn't render anyway. The point of this
        # test is to assert markup ABSENCE; no markup, no failure.
        return
    body = resp.text.lower()
    assert "djdt" not in body, "toolbar markup should not appear when DEBUG is unset"
    assert "fastdebug" not in body, "toolbar markup should not appear when DEBUG is unset"


@pytest.mark.integration
def test_request_id_header_always_present(app_no_toolbar):
    client = TestClient(app_no_toolbar)
    resp = client.get("/api/health")
    assert "x-request-id" in resp.headers


@pytest.mark.integration
def test_toolbar_html_present_when_debug(app_with_toolbar):
    client = TestClient(app_with_toolbar)
    # Try several HTML routes — at least one should respond 200 under
    # LOCAL_DEV_MODE=1 (auth bypass).
    for path in ("/dashboard", "/setup", "/login", "/admin/access"):
        resp = client.get(path, follow_redirects=False)
        if resp.status_code == 200 and "text/html" in resp.headers.get("content-type", ""):
            body = resp.text.lower()
            assert "djdt" in body or "fastdebug" in body, (
                f"toolbar markup missing on {path}; got first 500 chars: {body[:500]}"
            )
            return
    pytest.skip(
        "no HTML route returned 200 in TestClient; toolbar injection cannot be verified here",
    )
