"""Contract test for ``agnes server`` CLI surface.

Pre-cleanup this file ran 11 subprocess-mirror tests that each
mocked ``subprocess.run`` and asserted the command string contained
specific substrings (``"docker compose restart"`` /  ``"kamal deploy"``
/ ``"docker compose cp"`` ...). They mirrored the implementation —
any refactor of the wrapper (e.g. swapping ``docker compose`` for
``podman compose``) would fail every test without surfacing a real
regression. Codex adversarial-review finding #6.

Replaced with a single ``--help`` smoke that pins the CLI surface
operators discover by typing the command. Adding / removing a
subcommand still trips the test (real contract change); rewriting
the wrapper's subprocess call does not.
"""
from __future__ import annotations

from typer.testing import CliRunner

from cli.main import app

runner = CliRunner()


def test_server_subcommands_listed_in_help():
    """``agnes server --help`` must list every shipped subcommand.

    The list is the operator contract — what they expect to be able
    to type. Wire-level command shape (``docker compose restart`` vs
    something else) is implementation detail and intentionally not
    asserted here.
    """
    result = runner.invoke(app, ["server", "--help"])
    assert result.exit_code == 0, result.output
    for subcmd in ("status", "logs", "restart", "deploy", "rollback", "backup"):
        assert subcmd in result.output, (
            f"`agnes server --help` no longer lists {subcmd!r}; if this is "
            "intentional (subcommand renamed or dropped), update this test."
        )
