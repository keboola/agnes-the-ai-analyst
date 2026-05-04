"""Tests for `agnes admin metrics {import,export,validate}` (lifted from `da metrics`)."""

from typer.testing import CliRunner

from cli.commands.admin import admin_app


def test_admin_metrics_subcommands_present():
    runner = CliRunner()
    result = runner.invoke(admin_app, ["metrics", "--help"])
    assert result.exit_code == 0
    assert "import" in result.output
    assert "export" in result.output
    assert "validate" in result.output
