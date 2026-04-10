"""da — CLI tool for AI Data Analyst.

Primary interface for AI agents. Install: uv tool install data-analyst
"""

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

app = typer.Typer(
    name="da",
    help="AI Data Analyst CLI — data sync, queries, and admin for AI agents",
    no_args_is_help=True,
)

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


if __name__ == "__main__":
    app()
