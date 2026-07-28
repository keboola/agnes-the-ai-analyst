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
            state=SimpleNamespace(user={"id": "u-42", "email": "a@example.com", "name": "Ada"}),
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
        # User attributes inlined on the event itself per default identify mode (email).
        assert props["user_id"] == "u-42"
        assert props["user_email"] == "a@example.com"
        # name only at identify mode 'full'.
        assert "user_name" not in props


def test_capture_exception_user_props_full_mode(monkeypatch):
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_x")
    monkeypatch.setenv("POSTHOG_IDENTIFY_PII", "full")
    from src.observability import reset_posthog, get_posthog
    reset_posthog()
    sdk = MagicMock()
    with patch("posthog.Posthog", return_value=sdk):
        pc = get_posthog()
        request = SimpleNamespace(
            url=SimpleNamespace(path="/x"), method="POST",
            state=SimpleNamespace(user={"id": "u-1", "email": "a@b.test", "name": "Ada"}),
        )
        pc.capture_exception(RuntimeError("e"), request=request)
        props = sdk.capture_exception.call_args.kwargs["properties"]
        assert props["user_id"] == "u-1"
        assert props["user_email"] == "a@b.test"
        assert props["user_name"] == "Ada"
    reset_posthog()


def test_capture_exception_user_props_none_mode_emits_nothing(monkeypatch):
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_x")
    monkeypatch.setenv("POSTHOG_IDENTIFY_PII", "none")
    from src.observability import reset_posthog, get_posthog
    reset_posthog()
    sdk = MagicMock()
    with patch("posthog.Posthog", return_value=sdk):
        pc = get_posthog()
        request = SimpleNamespace(
            url=SimpleNamespace(path="/x"), method="POST",
            state=SimpleNamespace(user={"id": "u-1", "email": "a@b.test"}),
        )
        pc.capture_exception(RuntimeError("e"), request=request)
        props = sdk.capture_exception.call_args.kwargs["properties"]
        assert "user_id" not in props
        assert "user_email" not in props
        assert "user_name" not in props
    reset_posthog()


def test_capture_exception_anonymous_request_no_user_props(posthog_env):
    sdk = MagicMock()
    with patch("posthog.Posthog", return_value=sdk):
        from src.observability import get_posthog
        pc = get_posthog()
        request = SimpleNamespace(
            url=SimpleNamespace(path="/x"), method="GET",
            state=SimpleNamespace(),  # no .user attribute
        )
        pc.capture_exception(RuntimeError("e"), request=request)
        kwargs = sdk.capture_exception.call_args.kwargs
        assert kwargs["distinct_id"] == "anonymous"
        props = kwargs["properties"]
        assert "user_id" not in props
        assert "user_email" not in props


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


def test_environment_resolution_explicit_wins(monkeypatch):
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_x")
    monkeypatch.setenv("POSTHOG_ENVIRONMENT", "qa-7")
    monkeypatch.setenv("LOCAL_DEV_MODE", "1")  # would otherwise resolve to "local"
    monkeypatch.setenv("RELEASE_CHANNEL", "stable")
    from src.observability import reset_posthog, get_posthog
    reset_posthog()
    with patch("posthog.Posthog") as ctor:
        pc = get_posthog()
        assert pc.environment == "qa-7"
        kwargs = ctor.call_args.kwargs
        assert kwargs["super_properties"]["environment"] == "qa-7"
    reset_posthog()


def test_environment_resolution_local_dev_short_circuit(monkeypatch):
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_x")
    monkeypatch.delenv("POSTHOG_ENVIRONMENT", raising=False)
    monkeypatch.setenv("LOCAL_DEV_MODE", "1")
    monkeypatch.setenv("RELEASE_CHANNEL", "stable")  # ignored when LOCAL_DEV_MODE wins
    from src.observability import reset_posthog, get_posthog
    reset_posthog()
    with patch("posthog.Posthog"):
        assert get_posthog().environment == "local"
    reset_posthog()


def test_environment_release_channel_fallback(monkeypatch):
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_x")
    monkeypatch.delenv("POSTHOG_ENVIRONMENT", raising=False)
    monkeypatch.delenv("LOCAL_DEV_MODE", raising=False)
    monkeypatch.setenv("RELEASE_CHANNEL", "stable")
    from src.observability import reset_posthog, get_posthog
    reset_posthog()
    with patch("posthog.Posthog") as ctor:
        pc = get_posthog()
        assert pc.environment == "stable"
        # release also surfaces from AGNES_VERSION → RELEASE_CHANNEL fallback
        assert ctor.call_args.kwargs["super_properties"]["release"] == "stable"
    reset_posthog()


def test_environment_unknown_when_nothing_set(monkeypatch):
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_x")
    for var in ("POSTHOG_ENVIRONMENT", "LOCAL_DEV_MODE", "RELEASE_CHANNEL", "AGNES_DEPLOYMENT_ENV", "AGNES_VERSION"):
        monkeypatch.delenv(var, raising=False)
    from src.observability import reset_posthog, get_posthog
    reset_posthog()
    with patch("posthog.Posthog") as ctor:
        pc = get_posthog()
        assert pc.environment == "unknown"
        assert pc.release is None
        assert "release" not in ctor.call_args.kwargs["super_properties"]
    reset_posthog()


def _render_snippet(user_block):
    """Render `_posthog.html` directly with stub Jinja globals.

    Avoids spinning up the full TestClient for what is effectively a
    template-output assertion.
    """
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader("app/web/templates"), autoescape=False)
    return env.get_template("_posthog.html").render(
        request=None,
        posthog_config={
            "enabled": True,
            "host": "https://eu.i.posthog.com",
            "api_key_public": "phc_test",
            "replay_enabled": True,
            "replay_mask_selector_extra": "",
            "environment": "local",
            "release": "0.99.0",
        },
        posthog_user_block=lambda _r: user_block,
    )


def test_browser_snippet_registers_user_id_and_email_when_logged_in():
    out = _render_snippet({
        "distinct_id": "u-99",
        "props": {"email": "ada@example.com"},
    })

    # Super-properties: env + release always, plus user_id/email when logged in.
    assert "_superProps.user_id = \"u-99\"" in out
    assert "_superProps.user_email = \"ada@example.com\"" in out
    # identify() still fires alongside register() so person profiles get linked.
    assert "ph.identify(\"u-99\"" in out
    assert "\"email\": \"ada@example.com\"" in out
    # Environment + release land on the same super-prop bag.
    assert "environment: \"local\"" in out
    assert "release: \"0.99.0\"" in out


def test_browser_snippet_includes_user_name_in_full_mode():
    out = _render_snippet({
        "distinct_id": "u-99",
        "props": {"email": "ada@example.com", "name": "Ada Lovelace"},
    })

    assert "_superProps.user_name = \"Ada Lovelace\"" in out


def test_browser_snippet_omits_user_props_when_anonymous():
    out = _render_snippet(None)

    assert "_superProps.user_id" not in out
    assert "_superProps.user_email" not in out
    assert "_superProps.user_name" not in out
    assert "ph.identify(" not in out
    # Environment still registers so anonymous events are tagged too.
    assert "environment: \"local\"" in out


def test_browser_snippet_omits_email_when_id_only_mode():
    """Caller passes a block with only distinct_id → no email/name in output."""
    out = _render_snippet({"distinct_id": "u-1", "props": {}})

    assert "_superProps.user_id = \"u-1\"" in out
    assert "_superProps.user_email" not in out
    assert "_superProps.user_name" not in out


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
