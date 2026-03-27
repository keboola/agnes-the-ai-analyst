"""Explore commands — da explore {table}."""

import json
import os
from pathlib import Path

import typer

explore_app = typer.Typer(help="Explore data tables")


@explore_app.callback(invoke_without_command=True)
def explore(
    table: str = typer.Argument(..., help="Table name to explore"),
    remote: bool = typer.Option(False, "--remote", help="Fetch from server"),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Show profile and sample data for a table."""
    if remote:
        _explore_remote(table, as_json)
    else:
        _explore_local(table, as_json)


def _explore_local(table: str, as_json: bool):
    import duckdb

    local_dir = Path(os.environ.get("DA_LOCAL_DIR", "."))
    db_path = local_dir / "user" / "duckdb" / "analytics.duckdb"
    if not db_path.exists():
        typer.echo("Local DuckDB not found. Run: da sync", err=True)
        raise typer.Exit(1)

    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        # Check table exists
        tables = [r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name = ?", [table]
        ).fetchall()]
        if not tables:
            # Also check views
            tables = [r[0] for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_name = ? AND table_type='VIEW'", [table]
            ).fetchall()]
        if not tables:
            typer.echo(f"Table '{table}' not found. Available:", err=True)
            for r in conn.execute("SELECT table_name FROM information_schema.tables ORDER BY table_name").fetchall():
                typer.echo(f"  {r[0]}")
            raise typer.Exit(1)

        # Row count
        count = conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]

        # Column info
        columns = conn.execute(f"DESCRIBE \"{table}\"").fetchall()
        col_info = [{"name": c[0], "type": c[1], "nullable": c[2]} for c in columns]

        # Sample rows
        sample = conn.execute(f'SELECT * FROM "{table}" LIMIT 5').fetchall()
        sample_cols = [desc[0] for desc in conn.description]

        info = {
            "table": table,
            "row_count": count,
            "columns": col_info,
            "sample_rows": [dict(zip(sample_cols, row)) for row in sample],
        }

        if as_json:
            typer.echo(json.dumps(info, indent=2, default=str))
        else:
            typer.echo(f"Table: {table}")
            typer.echo(f"Rows: {count:,}")
            typer.echo(f"Columns ({len(col_info)}):")
            for c in col_info:
                typer.echo(f"  {c['name']:30s} {c['type']}")
            typer.echo(f"\nSample ({min(5, count)} rows):")
            from rich.console import Console
            from rich.table import Table
            console = Console()
            t = Table()
            for c in sample_cols:
                t.add_column(c)
            for row in sample:
                t.add_row(*(str(v) if v is not None else "" for v in row))
            console.print(t)
    finally:
        conn.close()


def _explore_remote(table: str, as_json: bool):
    from cli.client import api_get

    resp = api_get(f"/api/catalog/profile/{table}")
    if resp.status_code != 200:
        typer.echo(f"Profile not found: {resp.json().get('detail', resp.text)}", err=True)
        raise typer.Exit(1)

    if as_json:
        typer.echo(json.dumps(resp.json(), indent=2))
    else:
        profile = resp.json()
        typer.echo(f"Table: {table}")
        typer.echo(json.dumps(profile, indent=2, default=str))
