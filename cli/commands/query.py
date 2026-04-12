"""Query commands — da query."""

import json
import os
import sys
from pathlib import Path
from typing import List, Optional

import typer


def query_command(
    sql: Optional[str] = typer.Argument(None, help="SQL query to execute (positional)"),
    sql_opt: Optional[str] = typer.Option(None, "--sql", help="SQL query to execute (named option)"),
    remote: bool = typer.Option(False, "--remote", help="Execute on server instead of locally"),
    fmt: str = typer.Option("table", "--format", "-f", help="Output format: table, json, csv"),
    limit: int = typer.Option(1000, "--limit", help="Max rows to return"),
    register_bq: Optional[List[str]] = typer.Option(
        None,
        "--register-bq",
        help="Register a BigQuery result as a DuckDB view. Format: alias=BQ_SQL. Can be repeated.",
    ),
    stdin: bool = typer.Option(False, "--stdin", help="Read SQL from stdin as JSON {\"sql\": \"...\"}"),
):
    """Execute SQL query against DuckDB."""
    # Resolve SQL from exactly one of: positional, --sql, or --stdin
    sources_provided = sum([
        sql is not None,
        sql_opt is not None,
        stdin,
    ])
    if sources_provided == 0:
        typer.echo("Error: provide SQL as a positional argument, --sql option, or --stdin flag.", err=True)
        raise typer.Exit(1)
    if sources_provided > 1:
        typer.echo("Error: only one of positional SQL, --sql, or --stdin may be used at a time.", err=True)
        raise typer.Exit(1)

    if stdin:
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw)
            resolved_sql = payload["sql"]
            # Extract register_bq from stdin JSON
            stdin_bq = payload.get("register_bq", {})
            if stdin_bq and isinstance(stdin_bq, dict):
                register_bq = [f"{k}={v}" for k, v in stdin_bq.items()]
        except (json.JSONDecodeError, KeyError) as exc:
            typer.echo(f"Error: failed to parse stdin JSON: {exc}", err=True)
            raise typer.Exit(1)
    elif sql_opt is not None:
        resolved_sql = sql_opt
    else:
        resolved_sql = sql

    if register_bq:
        _query_hybrid(resolved_sql, fmt, limit, register_bq)
    elif remote:
        _query_remote(resolved_sql, fmt, limit)
    else:
        _query_local(resolved_sql, fmt, limit)


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


def _query_hybrid(sql: str, fmt: str, limit: int, register_bq_specs: List[str]):
    """Run a hybrid query: register BigQuery results as DuckDB views, then execute locally."""
    import duckdb
    from src.remote_query import RemoteQueryEngine, RemoteQueryError, load_config

    local_dir = Path(os.environ.get("DA_LOCAL_DIR", "."))
    db_path = local_dir / "user" / "duckdb" / "analytics.duckdb"
    if not db_path.exists():
        typer.echo("Local DuckDB not found. Run: da sync", err=True)
        raise typer.Exit(1)

    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        config = load_config()
        engine_kwargs = {k: v for k, v in config.items() if k in (
            "max_bq_registration_rows", "max_memory_mb", "max_result_rows", "timeout_seconds"
        )}
        # CLI --limit flag overrides config max_result_rows
        engine_kwargs["max_result_rows"] = limit
        engine = RemoteQueryEngine(conn, **engine_kwargs)

        for spec in register_bq_specs:
            if "=" not in spec:
                typer.echo(
                    f"Error: --register-bq spec must be 'alias=BQ_SQL', got: {spec!r}",
                    err=True,
                )
                raise typer.Exit(1)
            alias, bq_sql = spec.split("=", 1)
            alias = alias.strip()
            bq_sql = bq_sql.strip()
            try:
                info = engine.register_bq(alias, bq_sql)
                typer.echo(
                    f"Registered BQ alias '{alias}': {info['rows']:,} rows, "
                    f"{info['memory_mb']:.1f} MiB",
                    err=True,
                )
            except RemoteQueryError as exc:
                typer.echo(f"BQ registration failed for '{alias}': {exc}", err=True)
                raise typer.Exit(1)

        try:
            result = engine.execute(sql)
        except RemoteQueryError as exc:
            typer.echo(f"Query error: {exc}", err=True)
            raise typer.Exit(1)

        _output(result["columns"], result["rows"], fmt)
        if result.get("truncated"):
            typer.echo(f"(truncated at {result['row_count']} rows)", err=True)
    finally:
        conn.close()


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
