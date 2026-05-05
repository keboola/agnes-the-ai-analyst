"""LLM provider env-var fallback + fail-fast behavior.

When no ai: block is configured, `connectors.llm.factory.create_extractor`
must:

1. Build an extractor from `ANTHROPIC_API_KEY` / `LLM_API_KEY` if either
   env var is set (so an operator who only edited .env still gets a
   working pipeline).
2. Fail fast with a clear error if neither config nor env is available.
   No silent skip.

Closes one of five defects in #176.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from connectors.llm.factory import create_extractor, create_extractor_from_env_or_config


class TestEnvFallback:
    """Mocks the AnthropicExtractor constructor so the tests don't try to
    open a live SDK client (which loads system SSL certs at __init__ time
    and is unhappy on machines with corporate CA-bundle env vars pointing
    at a non-existent file). The test surface is the factory routing logic,
    not the SDK wiring — that's covered by tests/test_llm_providers_full.py.
    """

    def test_anthropic_env_fallback_builds_extractor(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env-aaa")
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        with patch("connectors.llm.factory.AnthropicExtractor") as mock_cls:
            ex = create_extractor_from_env_or_config(ai_config=None)
        assert ex is mock_cls.return_value
        kwargs = mock_cls.call_args.kwargs
        assert kwargs["api_key"] == "sk-ant-from-env-aaa"

    def test_llm_api_key_env_fallback_builds_extractor(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("LLM_API_KEY", "sk-proxy-from-env-bbb")
        with patch("connectors.llm.factory.AnthropicExtractor") as mock_cls:
            ex = create_extractor_from_env_or_config(ai_config=None)
        assert ex is mock_cls.return_value
        kwargs = mock_cls.call_args.kwargs
        assert kwargs["api_key"] == "sk-proxy-from-env-bbb"

    def test_no_config_no_env_fails_fast(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        with pytest.raises(ValueError) as excinfo:
            create_extractor_from_env_or_config(ai_config=None)
        msg = str(excinfo.value)
        # Error must mention BOTH config + env paths so operators know how to fix it.
        assert "instance.yaml" in msg or "ai:" in msg
        assert "ANTHROPIC_API_KEY" in msg

    def test_explicit_ai_config_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "ignored-because-config-wins")
        ai_cfg = {
            "provider": "anthropic",
            "api_key": "sk-ant-from-cfg-zzz",
            "model": "claude-haiku-4-5-20251001",
        }
        with patch("connectors.llm.factory.AnthropicExtractor") as mock_cls:
            ex = create_extractor_from_env_or_config(ai_config=ai_cfg)
        assert ex is mock_cls.return_value
        kwargs = mock_cls.call_args.kwargs
        assert kwargs["api_key"] == "sk-ant-from-cfg-zzz"

    def test_empty_dict_falls_through_to_env(self, monkeypatch):
        """ai: {} is treated the same as no block — fall through to env vars."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env-ccc")
        with patch("connectors.llm.factory.AnthropicExtractor") as mock_cls:
            ex = create_extractor_from_env_or_config(ai_config={})
        assert ex is mock_cls.return_value
        kwargs = mock_cls.call_args.kwargs
        assert kwargs["api_key"] == "sk-ant-from-env-ccc"
