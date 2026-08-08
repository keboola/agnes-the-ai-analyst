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
# `--show` renders the description, so it needs the server's plain-text
# projection: the column also holds descriptions imported verbatim from an
# external catalog, which are routinely rich HTML. This is the surface
# CLAUDE.md's agent rails send agents to for the canonical business
# definition, so tags here are read as part of the definition.
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: dict):
        self.status_code = 200
        self._payload = payload
        self.text = ""

    def json(self) -> dict:
        return self._payload


def test_show_prefers_the_servers_plain_text_projection(monkeypatch):
    import cli.commands.catalog as catalog_mod

    monkeypatch.setattr(
        catalog_mod,
        "api_get",
        lambda path: _FakeResponse(
            {
                "id": "keboola/live_deals",
                "name": "live_deals",
                "description": "<p><strong>Live Deals</strong> - deals currently live.</p>",
                "description_text": "Live Deals - deals currently live.",
            }
        ),
    )

    runner = CliRunner()
    result = runner.invoke(catalog_app, ["--show", "keboola/live_deals"])
    assert result.exit_code == 0, result.output
    out = _clean(result.output)
    assert "Description:  Live Deals - deals currently live." in out
    assert "<strong>" not in out
    assert "<p>" not in out


def test_show_falls_back_to_the_raw_column(monkeypatch):
    """An older server does not send `description_text`. Printing nothing
    would be a worse regression than printing the stored value."""
    import cli.commands.catalog as catalog_mod

    monkeypatch.setattr(
        catalog_mod,
        "api_get",
        lambda path: _FakeResponse({"id": "revenue/mrr", "name": "mrr", "description": "Total MRR."}),
    )

    runner = CliRunner()
    result = runner.invoke(catalog_app, ["--show", "revenue/mrr"])
    assert result.exit_code == 0, result.output
    assert "Description:  Total MRR." in _clean(result.output)
