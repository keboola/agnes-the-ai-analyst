"""`agnes admin metrics {import,export,validate}` — lifted from cli/commands/metrics.py.

Write paths to metric definitions live under `admin` because they mutate the
server-side metric registry (DuckDB direct, no API). Read paths (list/show)
live in `agnes catalog --metrics`.
"""

from pathlib import Path

import typer

admin_metrics_app = typer.Typer(help="Admin: metric definition management")


@admin_metrics_app.command("import")
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


@admin_metrics_app.command("export")
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


@admin_metrics_app.command("validate")
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
