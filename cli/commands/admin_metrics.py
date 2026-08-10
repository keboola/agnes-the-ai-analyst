"""`agnes admin metrics {import,export,validate}` — lifted from cli/commands/metrics.py.

Write paths to metric definitions live under `admin` because they mutate the
server-side metric registry (DuckDB direct, no API). Read paths (list/show)
live in `agnes catalog --metrics`.
"""

from pathlib import Path
from typing import Optional

import typer

from src.repositories import (
    metric_repo,
    table_registry_repo,
    use_pg,
)

admin_metrics_app = typer.Typer(help="Admin: metric definition management")


@admin_metrics_app.command("import")
def import_metrics(
    path: str = typer.Argument(..., help="Path to a YAML file or directory of YAML files"),
    source_ref: Optional[str] = typer.Option(
        None,
        "--source-ref",
        help="Label this export. Narrows --prune to metrics from the same label, so two exports can coexist.",
    ),
    prune: bool = typer.Option(
        False,
        "--prune",
        help="Delete previously imported metrics the directory no longer contains (never touches other sources).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Report what would be added, updated and deleted. Writes nothing.",
    ),
):
    """Import metric definitions from YAML into DuckDB (direct, no API).

    Upsert-only by default, so the registry can grow but never shrink. Add
    ``--prune`` to make it MATCH the directory — scoped to the rows this
    importer wrote, so a metric authored in the UI or created by another
    connector is out of reach by construction. Run ``--dry-run`` first: a
    rename is indistinguishable from delete + create at the id level.
    """
    from src.db import get_system_db

    import_path = Path(path)
    if not import_path.exists():
        typer.echo(f"Path not found: {path}", err=True)
        raise typer.Exit(1)

    # Repo work routes through the factory (PG or DuckDB); the system DuckDB
    # must never be opened on a Postgres instance (get_system_db raises there).
    conn = None if use_pg() else get_system_db()
    try:
        repo = metric_repo()
        try:
            report = repo.reconcile_from_yaml(
                import_path,
                source_ref=source_ref,
                prune=prune,
                dry_run=dry_run,
                on_delete=lambda mid: _audit_prune(mid, source_ref=source_ref),
            )
        except ValueError as e:
            # The repo refuses prune shapes that would delete the whole scope.
            # Surface the reason, not a traceback — the operator is one flag
            # away from the safe form.
            typer.echo(f"Error: {e}", err=True)
            raise typer.Exit(1)
        _echo_report(report, path=path, dry_run=dry_run, prune=prune)
    finally:
        if conn is not None:
            conn.close()


def _echo_report(report: dict, *, path: str, dry_run: bool, prune: bool) -> None:
    """Print the reconcile outcome, naming what was NOT done as well as what was.

    A silent cap reads as "covered everything": without the closing hint, an
    operator who forgot ``--prune`` sees a clean import and no sign that stale
    metrics are still there.
    """
    prefix = "Would import" if dry_run else "Imported"
    typer.echo(f"{prefix} {len(report['written'])} metric(s) from {path}")
    typer.echo(f"  added   {len(report['added'])}")
    typer.echo(f"  updated {len(report['updated'])}")
    for metric_id in report.get("adopted", []):
        typer.echo(f"  {'would take over' if dry_run else 'took over'} {metric_id} (was owned by another source)")
    for metric_id in report["deleted"]:
        typer.echo(f"  {'would delete' if dry_run else 'deleted'} {metric_id}")
    if not prune:
        typer.echo("  (--prune not set: metrics missing from this directory were left in place)")
    elif not report["deleted"]:
        typer.echo("  nothing to prune")


def _audit_prune(metric_id: str, *, source_ref: Optional[str]) -> None:
    """Audit one deletion, called by the repo BEFORE the row goes.

    This is the only destructive metric path and it runs direct against the
    repo, with no API request behind it to be audited instead — so the record
    has to be written here, and written first: an interruption between the two
    otherwise leaves a metric deleted with nothing saying so.
    """
    from src.repositories import audit_repo

    try:
        audit_repo().log(
            user_id=None,
            action="metrics.prune",
            resource=f"metric:{metric_id}"[:256],
            params={"source_ref": source_ref},
            result="success",
            client_kind="cli",
        )
    except Exception:  # noqa: BLE001 - an audit outage must not abort the import
        typer.echo(f"  (audit write failed for {metric_id})", err=True)


@admin_metrics_app.command("export")
def export_metrics(
    output_dir: str = typer.Option("./export/", "--dir", help="Output directory for YAML files"),
):
    """Export metric definitions from DuckDB to YAML files (direct, no API)."""
    from src.db import get_system_db

    # Repo work routes through the factory (PG or DuckDB); the system DuckDB
    # must never be opened on a Postgres instance (get_system_db raises there).
    conn = None if use_pg() else get_system_db()
    try:
        repo = metric_repo()
        count = repo.export_to_yaml(output_dir)
        typer.echo(f"Exported {count} metric(s) to {output_dir}")
    finally:
        if conn is not None:
            conn.close()


@admin_metrics_app.command("validate")
def validate_metrics():
    """Check each metric's table reference against registered tables (direct, no API)."""
    from src.db import get_system_db

    # Repo work routes through the factory (PG or DuckDB); the system DuckDB
    # must never be opened on a Postgres instance (get_system_db raises there).
    conn = None if use_pg() else get_system_db()
    try:
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
    finally:
        if conn is not None:
            conn.close()
