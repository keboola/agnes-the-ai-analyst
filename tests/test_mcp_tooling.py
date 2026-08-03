"""Unit tests for src/mcp_tooling.py — MCP wire-description summarizer and
output-size guard shared by the HTTP foundation and CLI stdio MCP servers."""

from __future__ import annotations

from datetime import date

import pytest

from src.mcp_tooling import (
    DEFAULT_MAX_OUTPUT_CHARS,
    MAX_OUTPUT_CHARS_ENV,
    MCPOutputTooLarge,
    ensure_output_size,
    max_output_chars,
    summarize_docstring,
)


class TestSummarizeDocstring:
    def test_single_paragraph_no_more(self):
        assert summarize_docstring("One line only.") == ("One line only.", False)

    def test_multi_paragraph_returns_first_and_flags_more(self):
        doc = "First para line one\ncontinues here.\n\nSecond paragraph."
        summary, has_more = summarize_docstring(doc)
        assert summary == "First para line one continues here."
        assert has_more is True

    def test_indented_docstring_is_cleaned(self):
        doc = """Show schema plus sample rows.

        Args:
            table_id: Table ID.
        """
        summary, has_more = summarize_docstring(doc)
        assert summary == "Show schema plus sample rows."
        assert has_more is True

    def test_none_and_empty(self):
        assert summarize_docstring(None) == ("", False)
        assert summarize_docstring("   ") == ("", False)


class TestEnsureOutputSize:
    def test_under_cap_returns_payload_unchanged(self):
        payload = {"rows": [[1, 2]]}
        assert ensure_output_size(payload, "query", cap=1000) is payload

    def test_over_cap_raises_with_guidance(self):
        payload = {"rows": [["x" * 500]]}
        with pytest.raises(MCPOutputTooLarge) as exc:
            ensure_output_size(payload, "query", cap=100)
        msg = str(exc.value)
        assert "query response" in msg
        assert "output cap" in msg
        assert "WHERE" in msg  # default hint mentions narrowing options

    def test_custom_hint_lands_in_message(self):
        with pytest.raises(MCPOutputTooLarge) as exc:
            ensure_output_size({"x": "y" * 200}, "describe", hint="lower `rows`", cap=50)
        assert "lower `rows`" in str(exc.value)

    def test_cap_zero_disables(self):
        payload = {"rows": [["x" * 10_000]]}
        assert ensure_output_size(payload, "query", cap=0) is payload

    def test_non_json_values_measured_via_str(self):
        payload = {"rows": [[date(2026, 1, 1)]]}
        assert ensure_output_size(payload, "query", cap=1000) is payload


class TestMaxOutputChars:
    def test_default(self, monkeypatch):
        monkeypatch.delenv(MAX_OUTPUT_CHARS_ENV, raising=False)
        assert max_output_chars() == DEFAULT_MAX_OUTPUT_CHARS

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv(MAX_OUTPUT_CHARS_ENV, "5000")
        assert max_output_chars() == 5000

    def test_invalid_env_falls_back(self, monkeypatch):
        monkeypatch.setenv(MAX_OUTPUT_CHARS_ENV, "banana")
        assert max_output_chars() == DEFAULT_MAX_OUTPUT_CHARS

    def test_env_zero_disables_guard(self, monkeypatch):
        monkeypatch.setenv(MAX_OUTPUT_CHARS_ENV, "0")
        big = {"rows": [["x" * (DEFAULT_MAX_OUTPUT_CHARS + 10)]]}
        assert ensure_output_size(big, "query") is big
