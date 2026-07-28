"""CLI tests for `agnes glossary search` / `agnes glossary show`."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from cli.main import app

runner = CliRunner()


def _mock_response(status_code, json_body):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    return resp


def test_glossary_search_human_readable():
    fake = _mock_response(
        200,
        {"query": "churn", "terms": [{"id": "a", "term": "Churn Rate", "definition": "Percent lost."}], "count": 1},
    )
    with patch("cli.commands.glossary.api_get", return_value=fake) as mock_get:
        result = runner.invoke(app, ["glossary", "search", "churn"])
    assert result.exit_code == 0
    assert "Churn Rate" in result.stdout
    mock_get.assert_called_once()
    assert mock_get.call_args.args[0] == "/api/glossary/search"
    assert mock_get.call_args.kwargs["params"]["q"] == "churn"


def test_glossary_search_json():
    fake = _mock_response(200, {"query": "churn", "terms": [{"id": "a", "term": "Churn Rate"}], "count": 1})
    with patch("cli.commands.glossary.api_get", return_value=fake):
        result = runner.invoke(app, ["glossary", "search", "churn", "--json"])
    assert result.exit_code == 0
    assert '"id": "a"' in result.stdout


def test_glossary_show_by_id():
    fake = _mock_response(
        200, {"id": "kb/m/mrr", "term": "MRR", "definition": "Monthly recurring revenue.", "see_also": []}
    )
    with patch("cli.commands.glossary.api_get", return_value=fake) as mock_get:
        result = runner.invoke(app, ["glossary", "show", "kb/m/mrr"])
    assert result.exit_code == 0
    assert "Monthly recurring revenue." in result.stdout
    assert mock_get.call_args.args[0] == "/api/glossary/kb/m/mrr"


def test_glossary_show_not_found():
    fake = _mock_response(404, {"detail": "Glossary term 'x' not found"})
    with patch("cli.commands.glossary.api_get", return_value=fake):
        result = runner.invoke(app, ["glossary", "show", "x"])
    assert result.exit_code == 1
    output = result.output + str(result.stderr_bytes or b"")
    assert "not found" in output.lower()
    # Command-UX standard: "not found" must hint the next step.
    assert "agnes glossary search" in output


def test_glossary_search_no_results_hints_next_step():
    fake = _mock_response(200, {"query": "xyzzy", "terms": [], "count": 0})
    with patch("cli.commands.glossary.api_get", return_value=fake):
        result = runner.invoke(app, ["glossary", "search", "xyzzy"])
    assert result.exit_code == 0
    assert "No glossary terms found" in result.stdout
    # Command-UX standard: "not found" must hint the next step.
    assert "sync" in result.stdout.lower()
