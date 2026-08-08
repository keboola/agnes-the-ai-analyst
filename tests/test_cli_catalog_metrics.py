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


# ---------------------------------------------------------------------------
# The CLI is a rendering surface too (#1216 follow-up)
# ---------------------------------------------------------------------------


def test_show_prints_the_servers_plain_text_projection(monkeypatch, capsys):
    """`agnes catalog --metrics --show` printed the raw column.

    That column holds two dialects, and for the HTML one the terminal got
    `<p><strong>…` verbatim. It matters more here than on a web page:
    CLAUDE.md's agent rails send agents to this exact command to read the
    canonical definition before computing a metric, so the tags land in an
    agent's reasoning rather than in someone's eyes.
    """
    import cli.commands.catalog as cat

    payload = {
        "id": "revenue/mrr",
        "name": "mrr",
        "display_name": "MRR",
        "description": "<p><strong>Live Deals</strong> — deals currently live</p>",
        "description_text": "Live Deals — deals currently live",
    }

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return payload

    monkeypatch.setattr(cat, "api_get", lambda *a, **k: _Resp())
    cat._show_one_metric("revenue/mrr", as_json=False)
    out = capsys.readouterr().out
    assert "Live Deals — deals currently live" in out
    assert "<strong>" not in out, "the raw HTML dialect must not reach the terminal"


def test_show_falls_back_to_the_raw_column_on_an_older_server(monkeypatch, capsys):
    """A CLI newer than its server must still print a description."""
    import cli.commands.catalog as cat

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"id": "m", "name": "m", "display_name": "M", "description": "plain markdown"}

    monkeypatch.setattr(cat, "api_get", lambda *a, **k: _Resp())
    cat._show_one_metric("m", as_json=False)
    assert "plain markdown" in capsys.readouterr().out
