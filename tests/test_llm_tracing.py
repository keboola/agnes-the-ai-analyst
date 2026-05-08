"""LLM tracing emits well-formed $ai_generation events."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def enabled_posthog(monkeypatch):
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_x")
    monkeypatch.delenv("POSTHOG_LLM_PAYLOADS", raising=False)
    from src.observability import reset_posthog
    reset_posthog()
    yield
    reset_posthog()


def test_success_emits_ai_generation_with_token_counts(enabled_posthog):
    sdk = MagicMock()
    with patch("posthog.Posthog", return_value=sdk):
        from src.observability import trace_generation

        with trace_generation(provider="anthropic", model="claude-test", distinct_id="u-1") as t:
            t.set_input("hello")
            t.set_tokens(input_tokens=5, output_tokens=10)

        # The wrapper calls sdk.capture exactly once.
        sdk.capture.assert_called_once()
        kwargs = sdk.capture.call_args.kwargs
        assert kwargs["event"] == "$ai_generation"
        assert kwargs["distinct_id"] == "u-1"
        props = kwargs["properties"]
        assert props["$ai_provider"] == "anthropic"
        assert props["$ai_model"] == "claude-test"
        assert props["$ai_input_tokens"] == 5
        assert props["$ai_output_tokens"] == 10
        assert "$ai_latency" in props
        assert "$ai_trace_id" in props
        # Payloads off by default — neither input nor output bodies leak.
        assert "$ai_input" not in props
        assert "$ai_output_choices" not in props
        assert "$ai_is_error" not in props


def test_payloads_flag_enables_prompt_and_completion(enabled_posthog, monkeypatch):
    monkeypatch.setenv("POSTHOG_LLM_PAYLOADS", "1")
    from src.observability import reset_posthog
    reset_posthog()
    sdk = MagicMock()
    with patch("posthog.Posthog", return_value=sdk):
        from src.observability import trace_generation

        with trace_generation(provider="openai_compat", model="gpt-x") as t:
            t.set_input("the prompt")
            t.set_output("the completion")
            t.set_tokens(input_tokens=1, output_tokens=2)

        kwargs = sdk.capture.call_args.kwargs
        props = kwargs["properties"]
        assert props["$ai_input"] == "the prompt"
        assert props["$ai_output_choices"] == "the completion"


def test_exception_emits_error_event_and_reraises(enabled_posthog):
    sdk = MagicMock()
    with patch("posthog.Posthog", return_value=sdk):
        from src.observability import trace_generation

        with pytest.raises(RuntimeError, match="api down"):
            with trace_generation(provider="anthropic", model="claude-test") as t:
                t.set_input("x")
                raise RuntimeError("api down")

        sdk.capture.assert_called_once()
        props = sdk.capture.call_args.kwargs["properties"]
        assert props["$ai_is_error"] is True
        assert "api down" in props["$ai_error"]
        assert props["$ai_provider"] == "anthropic"
        assert "$ai_latency" in props


def test_set_output_from_anthropic_extracts_tokens(enabled_posthog):
    sdk = MagicMock()
    with patch("posthog.Posthog", return_value=sdk):
        from src.observability import trace_generation

        # Build a fake Anthropic response object.
        block = SimpleNamespace(text="some output text")
        response = SimpleNamespace(
            usage=SimpleNamespace(input_tokens=11, output_tokens=22),
            content=[block],
        )

        with trace_generation(provider="anthropic", model="claude-test") as t:
            t.set_output_from_anthropic(response)

        props = sdk.capture.call_args.kwargs["properties"]
        assert props["$ai_input_tokens"] == 11
        assert props["$ai_output_tokens"] == 22


def test_set_output_from_openai_extracts_tokens(enabled_posthog):
    sdk = MagicMock()
    with patch("posthog.Posthog", return_value=sdk):
        from src.observability import trace_generation

        msg = SimpleNamespace(content="hi")
        choice = SimpleNamespace(message=msg)
        response = SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=7),
            choices=[choice],
        )

        with trace_generation(provider="openai_compat", model="gpt-x") as t:
            t.set_output_from_openai(response)

        props = sdk.capture.call_args.kwargs["properties"]
        assert props["$ai_input_tokens"] == 3
        assert props["$ai_output_tokens"] == 7
