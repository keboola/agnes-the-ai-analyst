"""Metrics commands — da metrics."""

import json
from pathlib import Path
from typing import Optional

import typer

from cli.client import api_get

metrics_app = typer.Typer(help="Metric definitions — list, show, import, export, validate")


@metrics_app.command("list")
def list_metrics(
    category: Optional[str] = typer.Option(None, "--category", help="Filter by category"),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List metric definitions from the server."""
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
        typer.echo(json.dumps(metrics, indent=2, default=str))
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


@metrics_app.command("show")
def show_metric(
    metric_id: str = typer.Argument(..., help="Metric ID (e.g. revenue/mrr)"),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Show details for a single metric."""
    resp = api_get(f"/api/metrics/{metric_id}")
    if resp.status_code == 404:
        typer.echo(f"Metric not found: {metric_id}", err=True)
        raise typer.Exit(1)
    if resp.status_code != 200:
        typer.echo(f"Failed: {resp.json().get('detail', resp.text)}", err=True)
        raise typer.Exit(1)

    m = resp.json()

    if as_json:
        typer.echo(json.dumps(m, indent=2, default=str))
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


@metrics_app.command("import")
def import_metrics(
    path: str = typer.Argument(..., help="Path to a YAML file or directory of YAML files"),
):
    """Import metric definitions from YAML into DuckDB (direct, no API)."""
    from src.db import get_system_db
    from src.repositories.metrics import MetricRepository

    import_path = Path(path)
    if not import_path.exists():
        typer.echo(f"Path not found: {path}", err=True)
        raise typer.Exit(1)

    conn = get_system_db()
    try:
        repo = MetricRepository(conn)
        count = repo.import_from_yaml(import_path)
        typer.echo(f"Imported {count} metric(s) from {path}")
    finally:
        conn.close()


@metrics_app.command("export")
def export_metrics(
    output_dir: str = typer.Option("./export/", "--dir", help="Output directory for YAML files"),
):
    """Export metric definitions from DuckDB to YAML files (direct, no API)."""
    from src.db import get_system_db
    from src.repositories.metrics import MetricRepository

    conn = get_system_db()
    try:
        repo = MetricRepository(conn)
        count = repo.export_to_yaml(output_dir)
        typer.echo(f"Exported {count} metric(s) to {output_dir}")
    finally:
        conn.close()


@metrics_app.command("validate")
def validate_metrics():
    """Check each metric's table reference against registered tables (direct, no API)."""
    from src.db import get_system_db
    from src.repositories.metrics import MetricRepository
    from src.repositories.table_registry import TableRegistryRepository

    conn = get_system_db()
    try:
        metric_repo = MetricRepository(conn)
        registry_repo = TableRegistryRepository(conn)

        metrics = metric_repo.list()
        registered_tables = {t["name"] for t in registry_repo.list_all()}

        ok_count = 0
        warn_count = 0

        for m in metrics:
            name = m.get("name", m.get("id", "?"))
            table = m.get("table_name")
            if not table:
                typer.echo(f"  OK   {name:30s} (no table reference)")
                ok_count += 1
            elif table in registered_tables:
                typer.echo(f"  OK   {name:30s} table={table}")
                ok_count += 1
            else:
                typer.echo(f"  WARN {name:30s} table={table} (not registered)")
                warn_count += 1

        typer.echo(f"\nTotal: {len(metrics)} metric(s) — {ok_count} OK, {warn_count} WARN")
        if warn_count > 0:
            raise typer.Exit(1)
    finally:
        conn.close()
