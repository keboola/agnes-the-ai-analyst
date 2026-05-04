"""`agnes catalog` — list registered tables and metric definitions (spec §4.1)."""

import json as json_lib
from typing import Optional

import typer

from cli.client import api_get
from cli.v2_client import api_get_json, V2ClientError

catalog_app = typer.Typer(help="List tables (and metrics, with --metrics) visible to you")


@catalog_app.callback(invoke_without_command=True)
def catalog(
    ctx: typer.Context,
    json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
    refresh: bool = typer.Option(False, "--refresh", help="Bypass client-side cache"),
    metrics: bool = typer.Option(
        False,
        "--metrics",
        help="List metric definitions instead of tables. Combine with --show <id> for details.",
    ),
    show: Optional[str] = typer.Option(
        None,
        "--show",
        help="With --metrics: show details for one metric id (e.g. revenue/mrr).",
    ),
):
    """List tables visible to you (RBAC-filtered).

    With ``--metrics`` lists registered metric definitions; pair with
    ``--show <id>`` to dump one definition.
    """
    if ctx.invoked_subcommand is not None:
        return

    if metrics:
        if show:
            _show_one_metric(show, as_json=json)
        else:
            _list_metrics(as_json=json)
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


def _list_metrics(as_json: bool, category: Optional[str] = None) -> None:
    """List metric definitions from the server (lifted from `da metrics list`)."""
    params = {}
    if category:
        params["category"] = category

    resp = api_get("/api/metrics", params=params)
    if resp.status_code != 200:
        typer.echo(f"Failed: {resp.json().get('detail', resp.text)}", err=True)
        raise typer.Exit(1)

    data = resp.json()
    metrics = data if isinstance(data, list) else data.get("metrics", [])

    if as_json:
        typer.echo(json_lib.dumps(metrics, indent=2, default=str))
        return

    if not metrics:
        typer.echo("No metrics found.")
        return

    # Group by category for display
    by_category: dict = {}
    for m in metrics:
        cat = m.get("category", "uncategorized")
        by_category.setdefault(cat, []).append(m)

    for cat, items in sorted(by_category.items()):
        typer.echo(f"\n[{cat}]")
        for m in items:
            name = m.get("name", m.get("id", "?"))
            display = m.get("display_name", name)
            unit = m.get("unit", "")
            unit_str = f" ({unit})" if unit else ""
            typer.echo(f"  {name:30s} {display}{unit_str}")


def _show_one_metric(metric_id: str, as_json: bool) -> None:
    """Show details for a single metric (lifted from `da metrics show`)."""
    resp = api_get(f"/api/metrics/{metric_id}")
    if resp.status_code == 404:
        typer.echo(f"Metric not found: {metric_id}", err=True)
        raise typer.Exit(1)
    if resp.status_code != 200:
        typer.echo(f"Failed: {resp.json().get('detail', resp.text)}", err=True)
        raise typer.Exit(1)

    m = resp.json()

    if as_json:
        typer.echo(json_lib.dumps(m, indent=2, default=str))
        return

    typer.echo(f"ID:           {m.get('id', metric_id)}")
    typer.echo(f"Name:         {m.get('name', '')}")
    typer.echo(f"Display Name: {m.get('display_name', '')}")
    typer.echo(f"Category:     {m.get('category', '')}")
    typer.echo(f"Type:         {m.get('type', '')}")
    if m.get("unit"):
        typer.echo(f"Unit:         {m['unit']}")
    if m.get("grain"):
        typer.echo(f"Grain:        {m['grain']}")
    if m.get("table_name"):
        typer.echo(f"Table:        {m['table_name']}")
    if m.get("description"):
        typer.echo(f"Description:  {m['description']}")
    if m.get("sql"):
        typer.echo(f"SQL:\n  {m['sql']}")
    if m.get("synonyms"):
        typer.echo(f"Synonyms:     {', '.join(m['synonyms'])}")
