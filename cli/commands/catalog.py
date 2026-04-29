"""`da catalog` — list registered tables (spec §4.1)."""

import json as json_lib
import typer
from cli.v2_client import api_get_json, V2ClientError

catalog_app = typer.Typer(help="List tables visible to you")


@catalog_app.callback(invoke_without_command=True)
def catalog(
    ctx: typer.Context,
    json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
    refresh: bool = typer.Option(False, "--refresh", help="Bypass client-side cache"),
):
    """List tables visible to you (RBAC-filtered)."""
    if ctx.invoked_subcommand is not None:
        return
    try:
        data = api_get_json("/api/v2/catalog", refresh=int(refresh))
    except V2ClientError as e:
        typer.echo(f"Error: catalog fetch failed: {e}", err=True)
        raise typer.Exit(5)

    if json:
        typer.echo(json_lib.dumps(data, indent=2))
        return
    # Human-readable table
    typer.echo(f"{'ID':30s}  {'SOURCE':10s}  {'MODE':8s}  {'FLAVOR':10s}  NAME")
    for t in data.get("tables", []):
        typer.echo(
            f"{t['id']:30s}  {t['source_type']:10s}  {t['query_mode']:8s}  "
            f"{t['sql_flavor']:10s}  {t.get('name', '')}"
        )
