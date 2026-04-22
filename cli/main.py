"""da — CLI tool for AI Data Analyst.

Primary interface for AI agents. Install: uv tool install data-analyst
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

import typer

from cli.commands.auth import auth_app
from cli.commands.sync import sync_app
from cli.commands.query import query_command
from cli.commands.status import status_app
from cli.commands.admin import admin_app
from cli.commands.diagnose import diagnose_app
from cli.commands.skills import skills_app
from cli.commands.setup import setup_app
from cli.commands.server import server_app
from cli.commands.explore import explore_app
from cli.commands.metrics import metrics_app
from cli.commands.analyst import analyst_app


def _cli_version() -> str:
    """Return the installed CLI version from package metadata.

    Falls back to `"unknown"` when the package is not installed (e.g. running
    from a source checkout without `uv pip install -e .`). Deliberately does
    not read pyproject.toml at runtime — that file is not shipped with the
    wheel and the metadata lookup is the canonical source.
    """
    try:
        return _pkg_version("agnes-the-ai-analyst")
    except PackageNotFoundError:
        return "unknown"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"da {_cli_version()}")
        raise typer.Exit()


app = typer.Typer(
    name="da",
    help="AI Data Analyst CLI — data sync, queries, and admin for AI agents",
    no_args_is_help=True,
)


@app.callback()
def _root(
    version: bool = typer.Option(
        None,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show the CLI version and exit.",
    ),
) -> None:
    """Root callback — carries the --version option.

    Typer requires a callback for top-level options. The body is intentionally
    empty; the heavy lifting happens in `_version_callback` (eager, so it
    runs before any subcommand resolution).
    """

# Register subcommands
app.add_typer(auth_app, name="auth")
app.add_typer(sync_app, name="sync")
app.command("query")(query_command)
app.add_typer(status_app, name="status")
app.add_typer(admin_app, name="admin")
app.add_typer(diagnose_app, name="diagnose")
app.add_typer(skills_app, name="skills")
app.add_typer(setup_app, name="setup")
app.add_typer(server_app, name="server")
app.add_typer(explore_app, name="explore")
app.add_typer(metrics_app, name="metrics")
app.add_typer(analyst_app, name="analyst")


if __name__ == "__main__":
    app()
