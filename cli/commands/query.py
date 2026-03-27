"""Query commands — da query."""

import json
import os
from pathlib import Path

import typer

def query_command(
    sql: str = typer.Argument(..., help="SQL query to execute"),
    remote: bool = typer.Option(False, "--remote", help="Execute on server instead of locally"),
    fmt: str = typer.Option("table", "--format", "-f", help="Output format: table, json, csv"),
    limit: int = typer.Option(1000, "--limit", help="Max rows to return"),
):
    """Execute SQL query against DuckDB."""
    if remote:
        _query_remote(sql, fmt, limit)
    else:
        _query_local(sql, fmt, limit)


def _query_local(sql: str, fmt: str, limit: int):
    """Run query against local DuckDB."""
    import duckdb

    local_dir = Path(os.environ.get("DA_LOCAL_DIR", "."))
    db_path = local_dir / "user" / "duckdb" / "analytics.duckdb"
    if not db_path.exists():
        typer.echo("Local DuckDB not found. Run: da sync", err=True)
        raise typer.Exit(1)

    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        result = conn.execute(sql).fetchmany(limit)
        columns = [desc[0] for desc in conn.description] if conn.description else []
        _output(columns, result, fmt)
    except Exception as e:
        typer.echo(f"Query error: {e}", err=True)
        raise typer.Exit(1)
    finally:
        conn.close()


def _query_remote(sql: str, fmt: str, limit: int):
    """Run query against server DuckDB via API."""
    from cli.client import api_post

    resp = api_post("/api/query", json={"sql": sql, "limit": limit})
    if resp.status_code != 200:
        typer.echo(f"Query failed: {resp.json().get('detail', resp.text)}", err=True)
        raise typer.Exit(1)

    data = resp.json()
    _output(data["columns"], data["rows"], fmt)
    if data.get("truncated"):
        typer.echo(f"(truncated at {limit} rows)", err=True)


def _output(columns: list, rows: list, fmt: str):
    if fmt == "json":
        output = [dict(zip(columns, row)) for row in rows]
        typer.echo(json.dumps(output, indent=2, default=str))
    elif fmt == "csv":
        typer.echo(",".join(columns))
        for row in rows:
            typer.echo(",".join(str(v) if v is not None else "" for v in row))
    else:
        # Table format using rich
        from rich.console import Console
        from rich.table import Table
        console = Console()
        table = Table()
        for col in columns:
            table.add_column(col)
        for row in rows:
            table.add_row(*(str(v) if v is not None else "" for v in row))
        console.print(table)
