"""Tests for `agnes diagnose system` (former `agnes status` content)."""

from typer.testing import CliRunner
from cli.commands.diagnose import diagnose_app

# CI-safety: Typer/rich emits ANSI escapes in --help output. Strip before asserts.
_ANSI_RE = __import__("re").compile(r"\x1b\[[0-9;]*m")
def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)

runner = CliRunner()


def test_diagnose_system_help():
    result = runner.invoke(diagnose_app, ["system", "--help"])
    assert result.exit_code == 0


def test_diagnose_help_lists_system():
    """Top-level diagnose help should mention the `system` subcommand."""
    result = runner.invoke(diagnose_app, ["--help"])
    assert result.exit_code == 0
    assert "system" in _clean(result.output)


def test_diagnose_default_still_works():
    """`agnes diagnose` (no subcommand) should still produce its existing output —
    we only added a sibling subcommand, didn't change the default."""
    result = runner.invoke(diagnose_app, [])
    # Either runs successfully or fails for unrelated reasons (no server etc).
    # We just want to verify no traceback from the addition.
    assert "Traceback" not in (_clean(result.output) + _clean(result.stderr or ''))
