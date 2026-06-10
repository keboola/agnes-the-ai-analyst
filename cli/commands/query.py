"""Query commands — agnes query."""

import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

import typer


def query_command(
    sql: Optional[str] = typer.Argument(None, help="SQL query to execute (positional)"),
    sql_opt: Optional[str] = typer.Option(None, "--sql", help="SQL query to execute (named option)"),
    remote: bool = typer.Option(False, "--remote", help="Execute on server instead of locally"),
    fmt: str = typer.Option("table", "--format", "-f", help="Output format: table, json, csv"),
    json_flag: bool = typer.Option(False, "--json", help="Shortcut for --format json"),
    limit: int = typer.Option(1000, "--limit", help="Max rows to return"),
    stdin: bool = typer.Option(False, "--stdin", help="Read SQL from stdin as JSON {\"sql\": \"...\"}"),
):
    """Execute SQL query against DuckDB."""
    # `--json` is an alias for `--format json` (issue #345 D). Paste-prompts
    # and LLM-assisted analysis routinely reach for `--json`; the typer
    # "Did you mean --stdin?" suggestion that the absence of this flag
    # produced was actively misleading.
    if json_flag:
        if fmt != "table" and fmt != "json":
            typer.echo(
                f"Error: --json and --format={fmt} are mutually exclusive.",
                err=True,
            )
            raise typer.Exit(1)
        fmt = "json"

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
        except (json.JSONDecodeError, KeyError) as exc:
            typer.echo(f"Error: failed to parse stdin JSON: {exc}", err=True)
            raise typer.Exit(1)
    elif sql_opt is not None:
        resolved_sql = sql_opt
    else:
        resolved_sql = sql

    if remote:
        _query_remote(resolved_sql, fmt, limit)
    else:
        _query_local(resolved_sql, fmt, limit)


def _query_local(sql: str, fmt: str, limit: int):
    """Run query against local DuckDB."""
    from src.duckdb_conn import _open_duckdb

    local_dir = Path(os.environ.get("AGNES_LOCAL_DIR", "."))
    db_path = local_dir / "user" / "duckdb" / "analytics.duckdb"
    if not db_path.exists():
        # No local data yet. Lead with `--remote` (runs server-side, no
        # download) — the right path in a constrained sandbox where a full
        # `agnes pull` would drag down every granted table. `agnes pull`
        # stays the offline-friendly option for laptop analysts.
        typer.echo(
            "No local DuckDB yet (nothing pulled). Two ways to run this query:\n"
            '  - Server-side, no download (recommended):  agnes query --remote "<SQL>"\n'
            "  - Or sync data locally first:               agnes pull   "
            "(downloads every table you can access — may be large)",
            err=True,
        )
        raise typer.Exit(1)

    conn = _open_duckdb(str(db_path), read_only=True)
    try:
        result = conn.execute(sql).fetchmany(limit)
        columns = [desc[0] for desc in conn.description] if conn.description else []
        _output(columns, result, fmt)
    except Exception as e:
        typer.echo(f"Query error: {e}", err=True)
        # DuckDB's "Did you mean <similar materialized view>" suggestion is
        # misleading when the unresolvable identifier is actually a
        # `query_mode='remote'` table — those have no local view by design.
        # Append a friendly hint pointing the user at `agnes catalog`,
        # `agnes schema`, and `agnes query --remote`. We don't verify against
        # the remote registry here (this command is offline-friendly), so the
        # hint is conditional ("might be") — safe even when the name was just
        # a typo.
        m = re.search(r"Table with name ([A-Za-z_][A-Za-z0-9_]*) does not exist", str(e))
        if m:
            typer.echo("", err=True)
            typer.echo(
                f"Note: `{m.group(1)}` might be a `query_mode='remote'` table. Local "
                "DuckDB only holds views for `local` and `materialized` tables — "
                "`remote` ones live on BigQuery and are not synced.\n"
                "  - List all registered tables:    agnes catalog\n"
                "  - Inspect column schema:         agnes schema <name>\n"
                "  - Run a query against BigQuery:  agnes query --remote \"<SQL>\"",
                err=True,
            )
        raise typer.Exit(1)
    finally:
        conn.close()


def _query_remote(sql: str, fmt: str, limit: int):
    """Run query against server DuckDB via API."""
    from cli.client import QUERY_TIMEOUT_S, api_post
    from cli.error_render import render_error

    resp = api_post(
        "/api/query",
        json={"sql": sql, "limit": limit},
        timeout=QUERY_TIMEOUT_S,
    )
    if resp.status_code != 200:
        # Parse JSON body if possible, fall back to text. The shared
        # renderer pretty-prints typed BQ errors (cross_project_forbidden,
        # remote_scan_too_large, bq_path_not_registered) instead of
        # flattening the structured detail to a single truncated line.
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        typer.echo(render_error(resp.status_code, body), err=True)
        raise typer.Exit(1)

    data = resp.json()
    _output(data["columns"], data["rows"], fmt)
    if data.get("truncated"):
        typer.echo(f"(truncated at {limit} rows)", err=True)
    # BigQuery dry-run scan estimate — present only for query_mode='remote'
    # rows; local queries return None and emit no line. Written to STDERR so
    # json/csv stdout stays pure (#393).
    if data.get("bytes_scanned") is not None:
        from cli.commands.snapshot import _format_size

        typer.echo(
            f"BigQuery scanned ~{_format_size(data['bytes_scanned'])} "
            "(dry-run estimate)",
            err=True,
        )


def _output(columns: list, rows: list, fmt: str):
    if fmt == "json":
        output = [dict(zip(columns, row)) for row in rows]
        typer.echo(json.dumps(output, indent=2, default=str))
    elif fmt == "csv":
        typer.echo(",".join(columns))
        for row in rows:
            typer.echo(",".join(str(v) if v is not None else "" for v in row))
    else:
        # Table format using rich, with a vertical-record fallback when the
        # column count would collapse every cell to zero width.
        #
        # Issue #255: `SELECT * FROM order_economics LIMIT 3` against a
        # 53-column table on an 80-col TTY produced an empty grid with
        # only header pipes visible — rich shrinks each column to fit and
        # gives up at 53 × 1-char minimum. Fallback to a psql-`\x`-style
        # record view ("─── row 1 ───\n  col: val\n…") when the column
        # count exceeds what the terminal can sensibly render.
        import shutil
        from rich.console import Console
        from rich.table import Table

        term_cols = shutil.get_terminal_size((120, 24)).columns
        # Conservative threshold: rich's column overhead (separators +
        # padding) is ~3 chars; below ~6 chars per column the result is
        # unreadable. Switch to vertical when columns × 6 > terminal.
        too_wide = len(columns) * 6 > term_cols
        console = Console()
        if too_wide:
            for i, row in enumerate(rows, 1):
                console.print(f"─── row {i} ───", style="dim")
                pad = max(len(c) for c in columns)
                for col, val in zip(columns, row):
                    rendered = "" if val is None else str(val)
                    console.print(f"  {col:<{pad}} : {rendered}")
            return
        table = Table()
        for col in columns:
            table.add_column(col)
        for row in rows:
            table.add_row(*(str(v) if v is not None else "" for v in row))
        console.print(table)
