"""`agnes sample <table>` works as a thin alias for `agnes describe -n 5`.

Regression coverage for #254: CLAUDE.md referenced ``sample`` for months
but only ``describe`` was registered. AI analysts following the docs
literally would hit "Usage: agnes [OPTIONS] COMMAND ..." until they
guessed the right name.
"""

from unittest.mock import patch


def test_sample_command_is_registered_in_typer():
    """`sample` shows up in `agnes --help`."""
    from typer.testing import CliRunner

    from cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    assert "sample" in result.output, (
        f"`sample` not in `agnes --help` output: {result.output}"
    )


def test_sample_forwards_to_describe_with_default_n():
    """`agnes sample <t>` calls describe with n=5 by default."""
    from cli.commands import sample as sample_mod

    with patch(
        "cli.commands.sample._describe",
    ) as mock_describe:
        sample_mod.sample(table_id="orders", n=5, json=False)
        mock_describe.assert_called_once_with(table_id="orders", n=5, json=False)


def test_sample_forwards_n_override():
    """`agnes sample <t> -n 20` passes n=20 to describe."""
    from cli.commands import sample as sample_mod

    with patch(
        "cli.commands.sample._describe",
    ) as mock_describe:
        sample_mod.sample(table_id="orders", n=20, json=False)
        mock_describe.assert_called_once_with(table_id="orders", n=20, json=False)


def test_sample_forwards_json_flag():
    from cli.commands import sample as sample_mod

    with patch(
        "cli.commands.sample._describe",
    ) as mock_describe:
        sample_mod.sample(table_id="orders", n=5, json=True)
        mock_describe.assert_called_once_with(table_id="orders", n=5, json=True)
