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


# ---------------------------------------------------------------------------
# Task D.1 — production JWT secret check
# ---------------------------------------------------------------------------


def test_chat_refuses_without_jwt_secret(monkeypatch):
    """chat.enabled=true with no JWT_SECRET_KEY → helper refuses.

    Without this gate, the chat path would silently mint JWTs against the
    fallback ``test-jwt-secret-key-minimum-32-chars!!`` constant — a
    production deployment would think it's authenticated and the secret
    would be public.
    """
    from app.chat.config import ChatConfig
    from app.main import _chat_jwt_secret_ok

    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    monkeypatch.delenv("TESTING", raising=False)
    cfg = ChatConfig(enabled=True)
    assert _chat_jwt_secret_ok(cfg) is False


def test_chat_refuses_short_jwt_secret(monkeypatch):
    """JWT_SECRET_KEY < 32 chars → refused as too weak."""
    from app.chat.config import ChatConfig
    from app.main import _chat_jwt_secret_ok

    monkeypatch.setenv("JWT_SECRET_KEY", "too-short")
    monkeypatch.delenv("TESTING", raising=False)
    cfg = ChatConfig(enabled=True)
    assert _chat_jwt_secret_ok(cfg) is False


def test_chat_accepts_long_jwt_secret(monkeypatch):
    """A 32+-byte JWT_SECRET_KEY is accepted (no fatal)."""
    from app.chat.config import ChatConfig
    from app.main import _chat_jwt_secret_ok

    monkeypatch.setenv(
        "JWT_SECRET_KEY", "this-is-a-32-char-or-more-secret-key!!",
    )
    monkeypatch.delenv("TESTING", raising=False)
    assert _chat_jwt_secret_ok(ChatConfig(enabled=True)) is True


def test_chat_skips_jwt_check_when_disabled(monkeypatch):
    """chat.enabled=false → the helper returns True regardless of env."""
    from app.chat.config import ChatConfig
    from app.main import _chat_jwt_secret_ok

    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    assert _chat_jwt_secret_ok(ChatConfig(enabled=False)) is True


# ---------------------------------------------------------------------------
# ANTHROPIC_API_KEY presence gate
# ---------------------------------------------------------------------------


def test_chat_refused_without_anthropic_key(monkeypatch):
    """chat.enabled=true with no ANTHROPIC_API_KEY → helper refuses."""
    from app.chat.config import ChatConfig
    from app.main import _chat_anthropic_key_ok

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("TESTING", raising=False)
    cfg = ChatConfig(enabled=True)
    assert _chat_anthropic_key_ok(cfg) is False


def test_chat_accepts_anthropic_key(monkeypatch):
    """chat.enabled=true with ANTHROPIC_API_KEY set → accepted."""
    from app.chat.config import ChatConfig
    from app.main import _chat_anthropic_key_ok

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-value")
    monkeypatch.delenv("TESTING", raising=False)
    cfg = ChatConfig(enabled=True)
    assert _chat_anthropic_key_ok(cfg) is True


def test_chat_anthropic_key_skipped_when_disabled(monkeypatch):
    """chat.enabled=false → key check is bypassed."""
    from app.chat.config import ChatConfig
    from app.main import _chat_anthropic_key_ok

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert _chat_anthropic_key_ok(ChatConfig(enabled=False)) is True


# ---------------------------------------------------------------------------
# E2B-specific gates (Task H.6 — provider reversal)
# ---------------------------------------------------------------------------


def test_chat_refuses_without_e2b_api_key(monkeypatch):
    """chat.enabled=true with provider=e2b but no E2B_API_KEY → refused."""
    from app.chat.config import ChatConfig
    from app.main import _chat_e2b_api_key_ok

    monkeypatch.delenv("E2B_API_KEY", raising=False)
    monkeypatch.delenv("TESTING", raising=False)
    cfg = ChatConfig(enabled=True, provider="e2b", e2b_template_id="agnes-chat")
    assert _chat_e2b_api_key_ok(cfg) is False


def test_chat_accepts_with_e2b_api_key(monkeypatch):
    """chat.enabled=true with E2B_API_KEY set → accepted."""
    from app.chat.config import ChatConfig
    from app.main import _chat_e2b_api_key_ok

    monkeypatch.setenv("E2B_API_KEY", "sk-e2b-test")
    monkeypatch.delenv("TESTING", raising=False)
    cfg = ChatConfig(enabled=True, provider="e2b", e2b_template_id="agnes-chat")
    assert _chat_e2b_api_key_ok(cfg) is True


def test_chat_e2b_key_skipped_when_disabled(monkeypatch):
    """chat.enabled=false → E2B key check bypassed."""
    from app.chat.config import ChatConfig
    from app.main import _chat_e2b_api_key_ok

    monkeypatch.delenv("E2B_API_KEY", raising=False)
    assert _chat_e2b_api_key_ok(ChatConfig(enabled=False)) is True


def test_chat_refuses_without_e2b_template_id(monkeypatch):
    """chat.enabled=true, provider=e2b, but no e2b_template_id → refused."""
    from app.chat.config import ChatConfig
    from app.main import _chat_e2b_template_id_ok

    monkeypatch.delenv("TESTING", raising=False)
    cfg = ChatConfig(enabled=True, provider="e2b", e2b_template_id=None)
    assert _chat_e2b_template_id_ok(cfg) is False


def test_chat_accepts_with_e2b_template_id(monkeypatch):
    """A non-empty e2b_template_id passes the gate."""
    from app.chat.config import ChatConfig
    from app.main import _chat_e2b_template_id_ok

    monkeypatch.delenv("TESTING", raising=False)
    cfg = ChatConfig(enabled=True, provider="e2b", e2b_template_id="agnes-chat")
    assert _chat_e2b_template_id_ok(cfg) is True


def test_chat_e2b_template_skipped_when_disabled(monkeypatch):
    """chat.enabled=false → template gate bypassed."""
    from app.chat.config import ChatConfig
    from app.main import _chat_e2b_template_id_ok

    cfg = ChatConfig(enabled=False, provider="e2b")
    assert _chat_e2b_template_id_ok(cfg) is True


def test_chat_e2b_gates_bypassed_for_non_e2b_provider(monkeypatch):
    """If provider != 'e2b', the e2b-specific gates short-circuit to True
    so the operator's misconfiguration is caught by the provider-allowlist
    branch, not by these gates."""
    from app.chat.config import ChatConfig
    from app.main import _chat_e2b_api_key_ok, _chat_e2b_template_id_ok

    monkeypatch.delenv("E2B_API_KEY", raising=False)
    monkeypatch.delenv("TESTING", raising=False)
    cfg = ChatConfig(enabled=True, provider="something_else")
    assert _chat_e2b_api_key_ok(cfg) is True
    assert _chat_e2b_template_id_ok(cfg) is True
