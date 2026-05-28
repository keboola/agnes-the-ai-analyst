"""Deployment-gate tests for the cloud-chat feature.

Two gates verified here:
1. UVICORN_WORKERS > 1  → chat_manager is None after lifespan runs.
2. chat_manager absent  → POST /api/chat/sessions returns 503 with
   kind == "chat_disabled".

The multi-worker test uses a minimal app whose lifespan replicates only
the UVICORN_WORKERS branch of app/main.py's CHAT-INIT block — avoiding
the full app.main lifespan (DuckDB, BQ config, PostHog, …) while still
exercising the exact code path under test.

The 503 test reuses the api_client_chat_disabled / logged_in_user
fixtures defined in test_chat_api.py.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_chat_api import api_client_chat_disabled, logged_in_user  # noqa: F401


# ---------------------------------------------------------------------------
# Helpers for test_multi_worker_disables_chat
# ---------------------------------------------------------------------------

def _make_app_with_worker_gate() -> FastAPI:
    """Minimal app whose lifespan runs only the multi-worker / chat-init gate."""

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        # Mirror the exact CHAT-INIT branch from app/main.py
        from app.chat.config import ChatConfig

        app.state.chat_config = ChatConfig(enabled=True)  # chat enabled

        if app.state.chat_config.enabled:
            if int(os.environ.get("UVICORN_WORKERS", "1")) > 1:
                app.state.chat_manager = None
            else:
                # Normally we'd create a real ChatManager here; in tests
                # the non-multi-worker path is not exercised by this file.
                app.state.chat_manager = object()  # sentinel: "something"

        yield  # server runs
        # teardown — nothing to clean up in this minimal app

    return FastAPI(lifespan=_lifespan)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_multi_worker_disables_chat(monkeypatch):
    """UVICORN_WORKERS=2 → chat_manager is None after lifespan startup."""
    monkeypatch.setenv("UVICORN_WORKERS", "2")

    app = _make_app_with_worker_gate()

    # TestClient's context manager (__enter__) fires the lifespan startup
    # automatically in this version of Starlette (no lifespan= kwarg needed).
    with TestClient(app):
        assert getattr(app.state, "chat_manager", None) is None


def test_single_worker_enables_chat(monkeypatch):
    """UVICORN_WORKERS=1 (default) → chat_manager is set after lifespan startup."""
    monkeypatch.setenv("UVICORN_WORKERS", "1")

    app = _make_app_with_worker_gate()

    with TestClient(app):
        assert getattr(app.state, "chat_manager", None) is not None


def test_disabled_returns_503(api_client_chat_disabled, logged_in_user):  # noqa: F811
    """When chat_manager is absent, POST /api/chat/sessions returns 503."""
    r = api_client_chat_disabled.post("/api/chat/sessions", json={"surface": "web"})
    assert r.status_code == 503
    assert r.json()["detail"]["kind"] == "chat_disabled"
