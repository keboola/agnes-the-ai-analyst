# tests/test_cli_catalog.py
import json
from typer.testing import CliRunner
from unittest.mock import patch
import typer
import pytest


def test_da_catalog_json_output(monkeypatch):
    """`da catalog --json` emits the server's JSON verbatim."""
    payload = {
        "tables": [
            {"id": "orders", "name": "orders", "source_type": "keboola",
             "query_mode": "local", "sql_flavor": "duckdb",
             "where_examples": [], "fetch_via": "...", "rough_size_hint": None},
        ],
        "server_time": "2026-04-27T17:30:00Z",
    }
    with patch("cli.commands.catalog.api_get_json", return_value=payload):
        from cli.commands.catalog import catalog_app
        runner = CliRunner()
        result = runner.invoke(catalog_app, ["--json"])
    assert result.exit_code == 0
    out = json.loads(result.stdout)
    assert out["tables"][0]["id"] == "orders"


def test_da_catalog_table_output(monkeypatch):
    payload = {
        "tables": [
            {"id": "orders", "name": "orders", "source_type": "keboola",
             "query_mode": "local", "sql_flavor": "duckdb",
             "where_examples": [], "fetch_via": "...", "rough_size_hint": None},
        ],
        "server_time": "2026-04-27T17:30:00Z",
    }
    with patch("cli.commands.catalog.api_get_json", return_value=payload):
        from cli.commands.catalog import catalog_app
        runner = CliRunner()
        result = runner.invoke(catalog_app, [])
    assert result.exit_code == 0
    assert "orders" in result.stdout
    assert "keboola" in result.stdout


def test_da_schema_json_output():
    """da schema <table> --json emits column metadata as JSON."""
    payload = {
        "table_id": "orders",
        "source_type": "keboola",
        "sql_flavor": "duckdb",
        "columns": [
            {"name": "id", "type": "INTEGER", "nullable": False, "description": "Primary key"},
            {"name": "total", "type": "DOUBLE", "nullable": True, "description": "Order total"},
        ],
        "partition_by": None,
        "clustered_by": [],
        "where_dialect_hints": {},
    }
    with patch("cli.commands.schema.api_get_json", return_value=payload):
        from cli.commands.schema import schema_app
        runner = CliRunner()
        result = runner.invoke(schema_app, ["--json", "orders"])
    assert result.exit_code == 0
    out = json.loads(result.stdout)
    assert out["table_id"] == "orders"
    assert len(out["columns"]) == 2


def test_da_schema_human_output():
    """da schema <table> shows human-readable column listing."""
    payload = {
        "table_id": "orders",
        "source_type": "keboola",
        "sql_flavor": "duckdb",
        "columns": [
            {"name": "id", "type": "INTEGER", "nullable": False, "description": "PK"},
        ],
        "partition_by": None,
        "clustered_by": [],
        "where_dialect_hints": {},
    }
    with patch("cli.commands.schema.api_get_json", return_value=payload):
        from cli.commands.schema import schema_app
        runner = CliRunner()
        result = runner.invoke(schema_app, ["orders"])
    assert result.exit_code == 0
    assert "orders" in result.stdout
    assert "id" in result.stdout
    assert "INTEGER" in result.stdout


def test_da_schema_error_exits_nonzero():
    """da schema propagates V2ClientError and exits with non-zero code."""
    from cli.v2_client import V2ClientError
    with patch("cli.commands.schema.api_get_json", side_effect=V2ClientError(status_code=404, body="not found")):
        from cli.commands.schema import schema_app
        runner = CliRunner()
        result = runner.invoke(schema_app, ["nonexistent"])
    assert result.exit_code != 0


def test_da_describe_json_output():
    """da describe <table> --json emits schema + sample as JSON."""
    schema_payload = {
        "table_id": "orders",
        "source_type": "keboola",
        "sql_flavor": "duckdb",
        "columns": [
            {"name": "id", "type": "INTEGER", "nullable": False, "description": "PK"},
        ],
        "partition_by": None,
        "clustered_by": [],
        "where_dialect_hints": {},
    }
    sample_payload = {
        "table_id": "orders",
        "rows": [{"id": 1}, {"id": 2}],
        "columns": ["id"],
    }

    def fake_get(path, **kwargs):
        if "schema" in path:
            return schema_payload
        return sample_payload

    with patch("cli.commands.describe.api_get_json", side_effect=fake_get):
        from cli.commands.describe import describe_app
        runner = CliRunner()
        result = runner.invoke(describe_app, ["--json", "orders"])
    assert result.exit_code == 0
    out = json.loads(result.stdout)
    assert "schema" in out
    assert "sample" in out
    assert out["schema"]["table_id"] == "orders"


def test_da_describe_human_output():
    """da describe <table> shows schema + sample in human-readable form."""
    schema_payload = {
        "table_id": "orders",
        "source_type": "keboola",
        "sql_flavor": "duckdb",
        "columns": [
            {"name": "id", "type": "INTEGER", "nullable": False, "description": "PK"},
        ],
        "partition_by": None,
        "clustered_by": [],
        "where_dialect_hints": {},
    }
    sample_payload = {
        "table_id": "orders",
        "rows": [{"id": 1}],
        "columns": ["id"],
    }

    def fake_get(path, **kwargs):
        if "schema" in path:
            return schema_payload
        return sample_payload

    with patch("cli.commands.describe.api_get_json", side_effect=fake_get):
        from cli.commands.describe import describe_app
        runner = CliRunner()
        result = runner.invoke(describe_app, ["orders"])
    assert result.exit_code == 0
    assert "orders" in result.stdout
    assert "id" in result.stdout
