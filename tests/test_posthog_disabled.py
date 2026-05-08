"""PostHog integration is fully off when POSTHOG_API_KEY is unset.

Pins the contract: zero network, zero side effects, snippet renders empty.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _clear_posthog_env(monkeypatch):
    """Drop every POSTHOG_* var so each test starts from disabled state."""
    for key in list(os.environ):
        if key.startswith("POSTHOG_"):
            monkeypatch.delenv(key, raising=False)
    from src.observability import reset_posthog
    reset_posthog()
    yield
    reset_posthog()


def test_get_posthog_returns_disabled_singleton():
    from src.observability import get_posthog

    pc = get_posthog()

    assert pc.enabled is False
    # singleton — second access returns same object
    assert get_posthog() is pc


def test_disabled_client_never_constructs_underlying_sdk():
    """With key unset, posthog.Posthog() must never be instantiated."""
    with patch("posthog.Posthog") as posthog_ctor:
        from src.observability import reset_posthog, get_posthog
        reset_posthog()
        pc = get_posthog()

        # Trigger every public method that could touch the SDK.
        pc.capture("custom_event", "user-1", {"k": "v"})
        pc.capture_exception(RuntimeError("boom"), distinct_id="user-1")
        assert pc.is_feature_enabled("flag", "user-1", default=False) is False
        assert pc.get_feature_flag_payload("flag", "user-1") is None
        pc.shutdown()

        posthog_ctor.assert_not_called()


def test_template_global_reports_disabled():
    """The Jinja-side `posthog_config` flag must be False when off."""
    from app.web.router import _posthog_config_global

    cfg = _posthog_config_global()

    assert cfg == {"enabled": False}


def test_template_user_block_returns_none_when_disabled():
    from app.web.router import _posthog_user_block

    assert _posthog_user_block(None) is None


def test_trace_generation_yields_noop_when_disabled():
    """The LLM-tracing context manager must not call into PostHog when off."""
    from src.observability import trace_generation

    with patch("src.observability.posthog_client.PosthogClient.capture") as cap:
        with trace_generation(provider="anthropic", model="claude-test") as t:
            t.set_input("hello")
            t.set_tokens(input_tokens=10, output_tokens=20)

        cap.assert_not_called()
