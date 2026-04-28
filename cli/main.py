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
from cli.commands.catalog import catalog_app
from cli.commands.schema import schema_app
from cli.commands.describe import describe_app
from cli.commands.fetch import fetch_app
from cli.commands.snapshot import snapshot_app
from cli.commands.disk_info import disk_info_app


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
    """Root callback — carries the --version option and fires the auto-update check.

    Update check runs before subcommand dispatch but after the --version flag
    (which exits early). It's best-effort: any failure is swallowed so a bad
    network never blocks a working `da` command. Disable with
    `DA_NO_UPDATE_CHECK=1`.
    """
    _maybe_warn_outdated()


def _maybe_warn_outdated() -> None:
    """Hit /cli/latest on the configured server (cached 24h) and emit a
    one-line stderr warning if the installed CLI is older. Never raises."""
    try:
        from cli.config import get_server_url
        from cli.update_check import check, format_outdated_notice
        info = check(get_server_url())
        if info and info.is_outdated():
            typer.echo(format_outdated_notice(info), err=True)
    except Exception:
        pass  # best-effort: never fail a command on the probe

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
app.add_typer(catalog_app, name="catalog")
app.add_typer(schema_app, name="schema")
app.add_typer(describe_app, name="describe")
app.add_typer(fetch_app, name="fetch")
app.add_typer(snapshot_app, name="snapshot")
app.add_typer(disk_info_app, name="disk-info")


if __name__ == "__main__":
    app()
