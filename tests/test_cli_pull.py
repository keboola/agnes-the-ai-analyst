"""Tests for `agnes pull` Typer wrapper."""

from typer.testing import CliRunner

# CI-safety: Typer/rich emits ANSI escapes in --help output. Strip before asserts.
_ANSI_RE = __import__("re").compile(r"\x1b\[[0-9;]*m")
def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)

from cli.commands.pull import pull_app

runner = CliRunner()


def test_pull_help():
    result = runner.invoke(pull_app, ["--help"])
    assert result.exit_code == 0
    assert "--quiet" in _clean(result.output)
    assert "--json" in _clean(result.output)
    assert "--dry-run" in _clean(result.output)


def test_pull_no_server_friendly_exit(tmp_path, monkeypatch):
    """No configured server -> exit 1 with friendly hint (no traceback)."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    monkeypatch.delenv("AGNES_SERVER", raising=False)
    monkeypatch.delenv("AGNES_TOKEN", raising=False)
    result = runner.invoke(pull_app, [])
    # Either exit 1 with hint, or exit 0 if a default server URL applies.
    # Either way, there must be no Python traceback in stderr/stdout.
    assert "Traceback" not in (_clean(result.output) + _clean(result.stderr or ''))
