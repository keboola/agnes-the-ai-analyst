"""`agnes admin metrics {import,export,validate}` — lifted from cli/commands/metrics.py.

Write paths to metric definitions live under `admin` because they mutate the
server-side metric registry, routed through the backend-aware
`src.repositories` factory (DuckDB or Postgres — no HTTP round-trip needed
since the CLI runs against the same local install as the server). Read
paths (list/show) live in `agnes catalog --metrics`.
"""

from pathlib import Path

import typer

from src.repositories import (
    metric_repo,
    table_registry_repo,
)

admin_metrics_app = typer.Typer(help="Admin: metric definition management")


@admin_metrics_app.command("import")
def import_metrics(
    path: str = typer.Argument(..., help="Path to a YAML file or directory of YAML files"),
):
    """Import metric definitions from YAML into the active backend (direct, no API)."""
    import_path = Path(path)
    if not import_path.exists():
        typer.echo(f"Path not found: {path}", err=True)
        raise typer.Exit(1)

    repo = metric_repo()
    count = repo.import_from_yaml(import_path)
    typer.echo(f"Imported {count} metric(s) from {path}")


@admin_metrics_app.command("export")
def export_metrics(
    output_dir: str = typer.Option("./export/", "--dir", help="Output directory for YAML files"),
):
    """Export metric definitions from the active backend to YAML files (direct, no API)."""
    repo = metric_repo()
    count = repo.export_to_yaml(output_dir)
    typer.echo(f"Exported {count} metric(s) to {output_dir}")


@admin_metrics_app.command("validate")
def validate_metrics():
    """Check each metric's table reference against registered tables (direct, no API)."""
    metrics_r = metric_repo()
    registry_repo = table_registry_repo()

    metrics = metrics_r.list()
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
