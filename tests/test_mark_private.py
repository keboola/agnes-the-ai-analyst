"""Tests for `agnes mark-private` — slash-command-driven private flag.

mark-private anchors the private list to the `workspace_root` config key (the
same anchor `agnes push` uses), falling back to cwd only when it's unset.
"""

from typer.testing import CliRunner

from cli.commands.mark_private import mark_private_app
from cli.lib.private_list import is_private

runner = CliRunner()


def _anchor(monkeypatch, workspace) -> None:
    monkeypatch.setattr("cli.commands.mark_private.get_workspace_root", lambda: str(workspace))


def test_mark_private_requires_session_id_env(tmp_path, monkeypatch):
    """Without CLAUDE_CODE_SESSION_ID set (= ran outside a Claude session),
    the command must error out with exit 1, NOT silently no-op."""
    _anchor(monkeypatch, tmp_path)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    result = runner.invoke(mark_private_app, [])
    assert result.exit_code == 1
    assert "CLAUDE_CODE_SESSION_ID" in result.output


def test_mark_private_writes_to_workspace_root_list(tmp_path, monkeypatch):
    _anchor(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc-123")

    result = runner.invoke(mark_private_app, [])
    assert result.exit_code == 0
    assert is_private(tmp_path, "abc-123")
    assert "abc-123" in result.output


def test_mark_private_is_idempotent(tmp_path, monkeypatch):
    _anchor(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc-123")

    runner.invoke(mark_private_app, [])
    result = runner.invoke(mark_private_app, [])
    assert result.exit_code == 0
    assert "already marked" in result.output.lower()


def test_mark_private_blank_session_id_treated_as_missing(tmp_path, monkeypatch):
    """Whitespace-only env var is the same as unset."""
    _anchor(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "   ")
    result = runner.invoke(mark_private_app, [])
    assert result.exit_code == 1


def test_mark_private_falls_back_to_cwd_without_workspace_root(tmp_path, monkeypatch):
    """No workspace_root in config → anchor on cwd (via AGNES_LOCAL_DIR),
    so /agnes-private still works on a fresh client before the first init."""
    monkeypatch.setattr("cli.commands.mark_private.get_workspace_root", lambda: None)
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "fresh-1")

    result = runner.invoke(mark_private_app, [])
    assert result.exit_code == 0
    assert is_private(tmp_path, "fresh-1")
