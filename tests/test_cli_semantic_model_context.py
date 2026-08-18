"""CLI tests for `agnes semantic-model context` and `agnes semantic-model
schema` — the agent read-parity tools (parity spec §4/§5), non-admin group.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    yield tmp_path


def _resp(status_code=200, json_data=None, text=""):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data if json_data is not None else {}
    r.text = text
    return r


class TestContext:
    def test_compact_lookup_lists_selections_by_type(self):
        body = {
            "results": [
                {
                    "semantic_type": "dataset",
                    "mode": "compact",
                    "objects": [{"name": "orders", "summary": "Order data.", "model": "retail"}],
                }
            ],
            "unknown_types": [],
        }
        with patch("cli.commands.semantic_model.api_get", return_value=_resp(200, body)) as m:
            result = runner.invoke(app, ["semantic-model", "context", "dataset"])
        assert result.exit_code == 0
        assert "orders" in result.output
        assert "Order data." in result.output
        params = m.call_args.kwargs["params"]
        selections = json.loads(params["selections"])
        assert selections == [{"semantic_type": "dataset", "ids": None}]
        assert "model_ids" not in params

    def test_explicit_ids_are_passed_through(self):
        body = {
            "results": [
                {"semantic_type": "metric", "mode": "full", "objects": [{"name": "revenue", "model": "retail"}]}
            ],
            "unknown_types": [],
        }
        with patch("cli.commands.semantic_model.api_get", return_value=_resp(200, body)) as m:
            result = runner.invoke(app, ["semantic-model", "context", "metric", "--id", "revenue", "--id", "mrr"])
        assert result.exit_code == 0
        params = m.call_args.kwargs["params"]
        selections = json.loads(params["selections"])
        assert selections == [{"semantic_type": "metric", "ids": ["revenue", "mrr"]}]

    def test_model_option_is_forwarded(self):
        body = {"results": [{"semantic_type": "dataset", "mode": "compact", "objects": []}], "unknown_types": []}
        with patch("cli.commands.semantic_model.api_get", return_value=_resp(200, body)) as m:
            result = runner.invoke(
                app, ["semantic-model", "context", "dataset", "--model", "retail", "--model", "finance"]
            )
        assert result.exit_code == 0
        assert m.call_args.kwargs["params"]["model_ids"] == ["retail", "finance"]

    def test_limit_caps_the_printed_objects_and_states_the_truncation(self):
        """Devin #1398 r3 / command-UX: --limit slices client-side and the
        partial scope is stated out loud, never silent."""
        body = {
            "results": [
                {
                    "semantic_type": "dataset",
                    "mode": "compact",
                    "objects": [{"name": f"d{i}", "summary": "s", "model": "m"} for i in range(3)],
                }
            ],
            "unknown_types": [],
        }
        with patch("cli.commands.semantic_model.api_get", return_value=_resp(200, body)):
            result = runner.invoke(app, ["semantic-model", "context", "dataset", "--limit", "2"])
        assert result.exit_code == 0
        assert "2 of 3 object(s)" in result.output
        assert "1 more" in result.output
        assert "d0" in result.output and "d1" in result.output and "d2" not in result.output

    def test_unmatched_id_hints_the_compact_listing(self):
        """Command-UX: a 'not found' path points at the next step."""
        body = {
            "results": [{"semantic_type": "metric", "mode": "full", "objects": []}],
            "unknown_types": [],
        }
        with patch("cli.commands.semantic_model.api_get", return_value=_resp(200, body)):
            result = runner.invoke(app, ["semantic-model", "context", "metric", "--id", "nope"])
        assert result.exit_code == 0
        assert "omit --id" in result.output

    def test_json_flag_emits_raw_json(self):
        body = {"results": [{"semantic_type": "dataset", "mode": "compact", "objects": []}], "unknown_types": []}
        with patch("cli.commands.semantic_model.api_get", return_value=_resp(200, body)):
            result = runner.invoke(app, ["semantic-model", "context", "dataset", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout) == body

    def test_unknown_type_is_reported(self):
        body = {"results": [], "unknown_types": ["glossary"]}
        with patch("cli.commands.semantic_model.api_get", return_value=_resp(200, body)):
            result = runner.invoke(app, ["semantic-model", "context", "glossary"])
        assert result.exit_code == 0
        assert "Unknown semantic type" in result.output
        assert "glossary" in result.output

    def test_server_error_exits_nonzero(self):
        with patch(
            "cli.commands.semantic_model.api_get",
            return_value=_resp(400, {"detail": "selections is not valid JSON"}),
        ):
            result = runner.invoke(app, ["semantic-model", "context", "dataset"])
        assert result.exit_code == 1
        assert "selections is not valid JSON" in result.output


class TestSchema:
    def test_shows_ref_and_schema_body(self):
        body = {
            "$defs": {"Dataset": {"type": "object", "required": ["name", "source"]}},
            "types": {"dataset": {"$ref": "#/$defs/Dataset"}},
            "unknown_types": [],
        }
        with patch("cli.commands.semantic_model.api_get", return_value=_resp(200, body)) as m:
            result = runner.invoke(app, ["semantic-model", "schema", "dataset"])
        assert result.exit_code == 0
        assert "Dataset" in result.output
        assert "required" in result.output
        assert m.call_args.kwargs["params"]["semantic_types"] == ["dataset"]

    def test_multiple_types_are_forwarded(self):
        body = {
            "$defs": {"Dataset": {}, "Metric": {}},
            "types": {"dataset": {"$ref": "#/$defs/Dataset"}, "metric": {"$ref": "#/$defs/Metric"}},
            "unknown_types": [],
        }
        with patch("cli.commands.semantic_model.api_get", return_value=_resp(200, body)) as m:
            result = runner.invoke(app, ["semantic-model", "schema", "dataset", "metric"])
        assert result.exit_code == 0
        assert m.call_args.kwargs["params"]["semantic_types"] == ["dataset", "metric"]

    def test_json_flag_emits_raw_json(self):
        body = {"$defs": {"Dataset": {}}, "types": {"dataset": {"$ref": "#/$defs/Dataset"}}, "unknown_types": []}
        with patch("cli.commands.semantic_model.api_get", return_value=_resp(200, body)):
            result = runner.invoke(app, ["semantic-model", "schema", "dataset", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout) == body

    def test_unknown_type_is_reported(self):
        body = {"$defs": {}, "types": {}, "unknown_types": ["glossary"]}
        with patch("cli.commands.semantic_model.api_get", return_value=_resp(200, body)):
            result = runner.invoke(app, ["semantic-model", "schema", "glossary"])
        assert result.exit_code == 0
        assert "Unknown semantic type" in result.output

    def test_server_error_exits_nonzero(self):
        with patch("cli.commands.semantic_model.api_get", return_value=_resp(500, {"detail": "boom"})):
            result = runner.invoke(app, ["semantic-model", "schema", "dataset"])
        assert result.exit_code == 1
        assert "boom" in result.output
