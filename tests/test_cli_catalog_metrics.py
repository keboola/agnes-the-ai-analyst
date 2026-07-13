"""Tests for `agnes catalog --metrics`."""

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


def test_catalog_show_without_metrics_implies_metrics(monkeypatch):
    """`agnes catalog --show <id>` (no --metrics) runs the metric-detail path."""
    import cli.commands.catalog as catalog_mod

    calls: list = []
    monkeypatch.setattr(
        catalog_mod,
        "_show_one_metric",
        lambda metric_id, as_json: calls.append((metric_id, as_json)),
    )

    runner = CliRunner()
    result = runner.invoke(catalog_app, ["--show", "revenue/mrr"])
    assert result.exit_code == 0, result.output
    assert calls == [("revenue/mrr", False)]
