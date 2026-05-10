"""Tests for `agnes mark-private` — slash-command-driven private flag."""

from typer.testing import CliRunner

from cli.commands.mark_private import mark_private_app
from cli.lib.private_list import is_private

runner = CliRunner()


def test_mark_private_requires_session_id_env(tmp_path, monkeypatch):
    """Without CLAUDE_CODE_SESSION_ID set (= ran outside a Claude session),
    the command must error out with exit 1, NOT silently no-op."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    result = runner.invoke(mark_private_app, [])
    assert result.exit_code == 1
    assert "CLAUDE_CODE_SESSION_ID" in result.output


def test_mark_private_writes_to_private_list(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc-123")

    result = runner.invoke(mark_private_app, [])
    assert result.exit_code == 0
    assert is_private(tmp_path, "abc-123")
    assert "abc-123" in result.output


def test_mark_private_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc-123")

    runner.invoke(mark_private_app, [])
    result = runner.invoke(mark_private_app, [])
    assert result.exit_code == 0
    assert "already marked" in result.output.lower()


def test_mark_private_blank_session_id_treated_as_missing(tmp_path, monkeypatch):
    """Whitespace-only env var is the same as unset."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "   ")
    result = runner.invoke(mark_private_app, [])
    assert result.exit_code == 1
