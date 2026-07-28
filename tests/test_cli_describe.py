"""CLI parsing regression tests for `agnes describe`.

Pins the fix for the Typer.Typer subcommand-group → flat @app.command
switch. Pre-fix, `agnes describe TABLE -n 5` failed with
`Missing argument 'TABLE_ID'` because Typer ate the positional as the
short-option's INTEGER value. Now all four invocation orders parse.
"""

from unittest.mock import patch

from typer.testing import CliRunner


_SCHEMA_PAYLOAD = {
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
_SAMPLE_PAYLOAD = {"table_id": "orders", "rows": [{"id": 1}], "columns": ["id"]}


def _fake_get(path, **kwargs):
    if "schema" in path:
        return _SCHEMA_PAYLOAD
    return _SAMPLE_PAYLOAD


def _invoke(args):
    with patch("cli.commands.describe.api_get_json", side_effect=_fake_get):
        from cli.main import app
        return CliRunner().invoke(app, args)


def test_describe_accepts_short_n_after_positional():
    """`agnes describe TABLE -n 5` — pre-fix this hit Typer's
    `Missing argument 'TABLE_ID'` because the short INTEGER option
    swallowed the positional in subcommand-group mode."""
    result = _invoke(["describe", "orders", "-n", "5"])
    assert result.exit_code == 0, result.stdout
    assert "Missing argument" not in result.stdout


def test_describe_accepts_n_before_positional():
    """`agnes describe -n 5 TABLE` — already worked pre-fix; pinned
    to make sure the move to a flat command kept it working."""
    result = _invoke(["describe", "-n", "5", "orders"])
    assert result.exit_code == 0, result.stdout


def test_describe_accepts_long_rows_with_equals():
    """`agnes describe TABLE --rows=5` — long-option-with-= form."""
    result = _invoke(["describe", "orders", "--rows=5"])
    assert result.exit_code == 0, result.stdout


def test_describe_default_n_is_5():
    """`agnes describe TABLE` (no -n) defaults to 5; passes the param
    through to /api/v2/sample. Verified by capturing the n= kwarg."""
    captured = {}

    def fake_get(path, **kwargs):
        if "sample" in path:
            captured["n"] = kwargs.get("n")
        return _SCHEMA_PAYLOAD if "schema" in path else _SAMPLE_PAYLOAD

    with patch("cli.commands.describe.api_get_json", side_effect=fake_get):
        from cli.main import app
        result = CliRunner().invoke(app, ["describe", "orders"])
    assert result.exit_code == 0, result.stdout
    assert captured["n"] == 5
