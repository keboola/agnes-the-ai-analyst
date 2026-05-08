"""PostHog client behavior when POSTHOG_API_KEY is set.

The underlying ``posthog.Posthog`` class is patched so the suite runs
without a network. We assert on the calls our wrapper forwards, plus
shape of the identify-mode payloads.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def posthog_env(monkeypatch):
    """Set up POSTHOG_API_KEY and reset the singleton."""
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_test_key")
    monkeypatch.delenv("POSTHOG_HOST", raising=False)
    monkeypatch.delenv("POSTHOG_IDENTIFY_PII", raising=False)
    monkeypatch.delenv("POSTHOG_REPLAY", raising=False)
    monkeypatch.delenv("POSTHOG_LLM_PAYLOADS", raising=False)
    from src.observability import reset_posthog
    reset_posthog()
    yield
    reset_posthog()


def test_enabled_when_key_set(posthog_env):
    with patch("posthog.Posthog") as posthog_ctor:
        from src.observability import get_posthog

        pc = get_posthog()

        assert pc.enabled is True
        assert pc.host == "https://eu.i.posthog.com"
        assert pc.identify_mode == "email"
        assert pc.replay_enabled is True
        assert pc.llm_payloads_enabled is False
        posthog_ctor.assert_called_once()
        kwargs = posthog_ctor.call_args.kwargs
        assert kwargs["project_api_key"] == "phc_test_key"
        assert kwargs["host"] == "https://eu.i.posthog.com"


def test_host_override(monkeypatch):
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_x")
    monkeypatch.setenv("POSTHOG_HOST", "https://us.i.posthog.com")
    from src.observability import reset_posthog, get_posthog
    reset_posthog()
    with patch("posthog.Posthog"):
        assert get_posthog().host == "https://us.i.posthog.com"
    reset_posthog()


def test_capture_exception_forwards_to_sdk(posthog_env):
    sdk = MagicMock()
    with patch("posthog.Posthog", return_value=sdk):
        from src.observability import get_posthog

        pc = get_posthog()
        request = SimpleNamespace(
            url=SimpleNamespace(path="/dashboard"),
            method="GET",
            state=SimpleNamespace(user={"id": "u-42", "email": "a@example.com"}),
        )
        pc.capture_exception(RuntimeError("boom"), request=request, properties={"k": "v"})

        sdk.capture_exception.assert_called_once()
        args, kwargs = sdk.capture_exception.call_args
        # Exception is positional (PostHog SDK ≥ 3.7).
        assert isinstance(args[0], RuntimeError)
        assert kwargs["distinct_id"] == "u-42"
        props = kwargs["properties"]
        assert props["path"] == "/dashboard"
        assert props["method"] == "GET"
        assert props["k"] == "v"


def test_capture_exception_falls_back_when_sdk_lacks_native(posthog_env):
    """Older posthog SDKs miss capture_exception — wrapper sends $exception."""
    sdk = MagicMock(spec=["capture", "shutdown"])
    with patch("posthog.Posthog", return_value=sdk):
        from src.observability import get_posthog

        pc = get_posthog()
        pc.capture_exception(ValueError("x"), distinct_id="u-1")

        sdk.capture.assert_called_once()
        kwargs = sdk.capture.call_args.kwargs
        assert kwargs["event"] == "$exception"
        assert kwargs["distinct_id"] == "u-1"
        assert kwargs["properties"]["$exception_type"] == "ValueError"
        assert kwargs["properties"]["$exception_message"] == "x"


def test_is_feature_enabled_returns_default_on_sdk_error(posthog_env):
    sdk = MagicMock()
    # Wrapper prefers the v7 name `feature_enabled`. Patch both so either
    # SDK version routes through the failing path.
    sdk.feature_enabled.side_effect = RuntimeError("network down")
    sdk.is_feature_enabled.side_effect = RuntimeError("network down")
    with patch("posthog.Posthog", return_value=sdk):
        from src.observability import get_posthog

        assert get_posthog().is_feature_enabled("flag-x", "u-1", default=True) is True


def test_invalid_identify_mode_falls_back_to_email(monkeypatch, caplog):
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_x")
    monkeypatch.setenv("POSTHOG_IDENTIFY_PII", "completely-bogus")
    from src.observability import reset_posthog, get_posthog
    reset_posthog()
    with patch("posthog.Posthog"):
        assert get_posthog().identify_mode == "email"
    reset_posthog()


def test_template_user_block_respects_identify_modes(monkeypatch):
    """The Jinja helper produces id-only / email / full payloads on demand."""
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_x")
    from app.web.router import _posthog_user_block
    from src.observability import reset_posthog
    reset_posthog()
    with patch("posthog.Posthog"):
        request = SimpleNamespace(state=SimpleNamespace(
            user={"id": "u-7", "email": "a@b.test", "name": "Ada"},
        ))

        monkeypatch.setenv("POSTHOG_IDENTIFY_PII", "id")
        reset_posthog()
        block = _posthog_user_block(request)
        assert block == {"distinct_id": "u-7", "props": {}}

        monkeypatch.setenv("POSTHOG_IDENTIFY_PII", "email")
        reset_posthog()
        block = _posthog_user_block(request)
        assert block == {"distinct_id": "u-7", "props": {"email": "a@b.test"}}

        monkeypatch.setenv("POSTHOG_IDENTIFY_PII", "full")
        reset_posthog()
        block = _posthog_user_block(request)
        assert block == {"distinct_id": "u-7", "props": {"email": "a@b.test", "name": "Ada"}}

        monkeypatch.setenv("POSTHOG_IDENTIFY_PII", "none")
        reset_posthog()
        assert _posthog_user_block(request) is None
    reset_posthog()


def test_template_user_block_anonymous_returns_none(monkeypatch):
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_x")
    from app.web.router import _posthog_user_block
    from src.observability import reset_posthog
    reset_posthog()
    with patch("posthog.Posthog"):
        request = SimpleNamespace(state=SimpleNamespace())  # no user attribute
        # `getattr` falls back to None — block should be None.
        assert _posthog_user_block(request) is None
    reset_posthog()
