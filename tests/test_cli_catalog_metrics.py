"""Tests for `agnes catalog --metrics` (folded from `da metrics list/show`)."""

from typer.testing import CliRunner

# CI-safety: Typer/rich emits ANSI escapes in --help output. Strip before asserts.
_ANSI_RE = __import__("re").compile(r"\x1b\[[0-9;]*m")
def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)

from cli.commands.catalog import catalog_app


def test_catalog_metrics_help():
    runner = CliRunner()
    result = runner.invoke(catalog_app, ["--help"])
    assert result.exit_code == 0
    assert "--metrics" in _clean(result.output)
    assert "--show" in _clean(result.output)


def test_catalog_default_still_works():
    """Existing `agnes catalog` (no flags) behavior unchanged."""
    runner = CliRunner()
    # Help should still mention the default tables view
    result = runner.invoke(catalog_app, ["--help"])
    assert result.exit_code == 0
    # No traceback
    assert "Traceback" not in _clean(result.output)
