"""`agnes schema` / `agnes describe` 404 hints for a typo'd table id.

Pins the fix: a 404 `V2ClientError` from the registry now prints a
targeted hint (list-tables / search suggestions) instead of the bare
`Error: ... failed: <exc>` message. Non-404 errors keep today's
generic message verbatim.
"""

from unittest.mock import patch

from typer.testing import CliRunner

from cli.main import app
from cli.v2_client import V2ClientError


def _hint(table_id: str) -> str:
    return (
        f"Table '{table_id}' not found in the registry.\n"
        "  - List available tables:  agnes catalog\n"
        f'  - Search everything:      agnes search "{table_id}"'
    )


def test_schema_404_prints_hint_and_keeps_exit_code():
    with patch(
        "cli.commands.schema.api_get_json",
        side_effect=V2ClientError(status_code=404, body={"detail": "not found"}),
    ):
        result = CliRunner().invoke(app, ["schema", "bogus_table"])
    assert result.exit_code == 2, result.output
    assert _hint("bogus_table") in result.output


def test_schema_500_keeps_generic_message():
    with patch(
        "cli.commands.schema.api_get_json",
        side_effect=V2ClientError(status_code=500, body={"detail": "boom"}),
    ):
        result = CliRunner().invoke(app, ["schema", "bogus_table"])
    assert result.exit_code == 5, result.output
    assert "not found in the registry" not in result.output
    assert "Error: schema fetch failed:" in result.output


def test_describe_404_prints_hint():
    with patch(
        "cli.commands.describe.api_get_json",
        side_effect=V2ClientError(status_code=404, body={"detail": "not found"}),
    ):
        result = CliRunner().invoke(app, ["describe", "bogus_table"])
    assert _hint("bogus_table") in result.output


def test_describe_500_keeps_generic_message():
    with patch(
        "cli.commands.describe.api_get_json",
        side_effect=V2ClientError(status_code=500, body={"detail": "boom"}),
    ):
        result = CliRunner().invoke(app, ["describe", "bogus_table"])
    assert "not found in the registry" not in result.output
    assert "Error: describe failed:" in result.output
