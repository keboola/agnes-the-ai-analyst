"""`da describe <table>` — schema + sample rows (spec §4.1)."""

import json as json_lib
import typer
from cli.v2_client import api_get_json, V2ClientError

describe_app = typer.Typer(help="Show schema + sample rows for a table")


@describe_app.callback(invoke_without_command=True)
def describe(
    ctx: typer.Context,
    table_id: str = typer.Argument(...),
    n: int = typer.Option(5, "-n", "--rows", help="Sample rows count"),
    json: bool = typer.Option(False, "--json"),
):
    """Show schema + sample rows for a table."""
    if ctx.invoked_subcommand is not None:
        return
    try:
        sch = api_get_json(f"/api/v2/schema/{table_id}")
        sam = api_get_json(f"/api/v2/sample/{table_id}", n=n)
    except V2ClientError as e:
        typer.echo(f"Error: describe failed: {e}", err=True)
        raise typer.Exit(5 if e.status_code >= 500 else 8 if e.status_code == 403 else 2)

    if json:
        typer.echo(json_lib.dumps({"schema": sch, "sample": sam}, indent=2, default=str))
        return

    typer.echo(f"Table: {sch['table_id']}")
    typer.echo("")
    typer.echo("Schema:")
    for c in sch.get("columns", []):
        typer.echo(f"  {c['name']:30s} {c['type']}")
    typer.echo("")
    typer.echo(f"Sample ({len(sam.get('rows', []))} rows):")
    for row in sam.get("rows", []):
        typer.echo(f"  {row}")
