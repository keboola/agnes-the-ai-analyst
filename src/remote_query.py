"""
Remote Query - Execute DuckDB queries spanning local Parquet + remote BigQuery tables.

Provides a server-side CLI for the AI agent to run SQL queries that JOIN local
(Parquet-backed) tables with on-demand BigQuery results. Designed for tables too
large to sync locally (e.g., daily_deal_traffic: ~3M rows/day).

Two-phase query protocol:
1. BQ sub-queries (--register-bq "alias=SQL") run on BigQuery, results registered
   as DuckDB views via PyArrow (reuses register_bq_table from duckdb_manager).
2. DuckDB SQL (--sql) runs against local Parquet views + registered BQ results.

Usage:
    python -m src.remote_query \\
        --sql "SELECT ... FROM order_economics o JOIN traffic t ON ..." \\
        --register-bq "traffic=SELECT ... FROM \\`project.dataset.table\\` WHERE ..." \\
        --format table

Safety features:
- COUNT(*) pre-check before fetching BQ data
- Memory estimation (refuses queries > 2 GB estimated)
- Configurable row limits (per BQ sub-query and final result)
- Query timeout support
"""

import argparse
import csv
import io
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

import duckdb

from config.loader import get_instance_value
from scripts.duckdb_manager import (
    create_local_views,
    register_bq_table,
    _create_bq_client,
)

logger = logging.getLogger(__name__)


class RemoteQueryError(Exception):
    """Error during remote query execution."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _load_remote_query_config() -> dict:
    """Load remote_query settings from instance.yaml with defaults.

    Uses raw YAML loading instead of load_instance_config() to avoid
    requiring webapp secrets (WEBAPP_SECRET_KEY etc.) that analysts
    don't have access to.
    """
    import yaml as _yaml
    from pathlib import Path as _Path

    instance_config: dict = {}
    config_dir = _Path(os.environ.get("CONFIG_DIR", "./config"))
    yaml_path = config_dir / "instance.yaml"
    if yaml_path.exists():
        try:
            with open(yaml_path) as f:
                instance_config = _yaml.safe_load(f) or {}
        except Exception as e:
            logger.warning("Could not load instance.yaml: %s. Using defaults.", e)

    return {
        "timeout_seconds": get_instance_value(
            instance_config, "remote_query", "timeout_seconds", default=300,
        ),
        "max_result_rows": get_instance_value(
            instance_config, "remote_query", "max_result_rows", default=100_000,
        ),
        "max_bq_registration_rows": get_instance_value(
            instance_config, "remote_query", "max_bq_registration_rows", default=500_000,
        ),
        "default_format": get_instance_value(
            instance_config, "remote_query", "default_format", default="table",
        ),
        "output_dir": get_instance_value(
            instance_config, "remote_query", "output_dir", default="/tmp/remote_query",
        ),
    }


# ---------------------------------------------------------------------------
# BQ registration with safety checks
# ---------------------------------------------------------------------------

def _validate_bq_result_size(
    bq_client, sql: str, alias: str, max_rows: int,
) -> int:
    """Execute COUNT(*) on the BQ sub-query before fetching all rows.

    Args:
        bq_client: BigQuery client instance
        sql: The BQ SQL query to count
        alias: Alias name (for error messages)
        max_rows: Maximum allowed rows

    Returns:
        Row count

    Raises:
        RemoteQueryError: If count exceeds max_rows
    """
    count_sql = f"SELECT COUNT(*) AS cnt FROM ({sql})"
    _log_progress(f"  Counting rows for '{alias}'...")

    job = bq_client.query(count_sql)
    result = job.result()
    row_count = next(iter(result))[0]

    if row_count > max_rows:
        raise RemoteQueryError(
            f"BQ sub-query '{alias}' would return {row_count:,} rows "
            f"(limit: {max_rows:,}). Add more WHERE filters or GROUP BY "
            f"to reduce the result set."
        )

    return row_count


def _estimate_memory_mb(row_count: int, column_count: int) -> float:
    """Estimate memory usage in MB for a PyArrow table.

    Uses ~50 bytes per cell as a rough average across data types.
    """
    return (row_count * column_count * 50) / (1024 * 1024)


def _register_bq_views(
    conn: duckdb.DuckDBPyConnection,
    registrations: list[tuple[str, str]],
    max_bq_rows: int,
    timeout_seconds: int,
    quiet: bool = False,
) -> dict[str, int]:
    """Register BQ query results as DuckDB views with safety checks.

    Args:
        conn: DuckDB connection
        registrations: List of (alias, bq_sql) tuples
        max_bq_rows: Max rows per sub-query
        timeout_seconds: BQ job timeout
        quiet: Suppress progress messages

    Returns:
        Dict of {alias: row_count}
    """
    if not registrations:
        return {}

    bq_project = os.environ.get("BIGQUERY_PROJECT")
    if not bq_project:
        raise RemoteQueryError(
            "BIGQUERY_PROJECT environment variable not set. "
            "Required for BigQuery sub-queries."
        )

    bq_client = _create_bq_client(bq_project)
    results = {}

    for alias, bq_sql in registrations:
        start_time = time.time()

        # Phase 1: COUNT(*) safety check
        row_count = _validate_bq_result_size(bq_client, bq_sql, alias, max_bq_rows)
        _log_progress(f"  '{alias}': {row_count:,} rows (within limit)")

        # Phase 2: Memory estimation
        # Estimate column count from a LIMIT 0 query (cheap)
        sample_job = bq_client.query(f"SELECT * FROM ({bq_sql}) LIMIT 0")
        schema = sample_job.result().schema
        col_count = len(schema)
        estimated_mb = _estimate_memory_mb(row_count, col_count)

        if estimated_mb > 2048:  # 2 GB = 25% of 8 GB server RAM
            raise RemoteQueryError(
                f"BQ sub-query '{alias}' estimated memory: {estimated_mb:.0f} MB "
                f"({row_count:,} rows x {col_count} cols). "
                f"Limit is 2048 MB. Add more aggregation or filters."
            )

        # Phase 3: Execute and register
        _log_progress(f"  Fetching '{alias}' ({row_count:,} rows, ~{estimated_mb:.0f} MB)...")
        actual_rows = register_bq_table(
            conn=conn,
            table_id=f"bq_registration.{alias}",
            view_name=alias,
            sql=bq_sql,
            bq_project=bq_project,
        )

        elapsed = time.time() - start_time
        _log_progress(f"  '{alias}' registered: {actual_rows:,} rows in {elapsed:.1f}s")
        results[alias] = actual_rows

    return results


# ---------------------------------------------------------------------------
# Local view setup
# ---------------------------------------------------------------------------

def _setup_local_views(
    conn: duckdb.DuckDBPyConnection,
    data_dir: str,
    quiet: bool = False,
) -> list[str]:
    """Create DuckDB views for all local/hybrid tables from Parquet.

    Args:
        conn: DuckDB connection
        data_dir: Path to data directory (e.g., "/data/src_data")
        quiet: Suppress progress messages

    Returns:
        List of created view names
    """
    created, skipped = create_local_views(
        conn=conn,
        data_dir=data_dir,
        verbose=not quiet,
    )
    return created


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def _print_table(columns: list[str], rows: list[tuple]) -> None:
    """Print an aligned ASCII table to stdout."""
    if not rows:
        print("(empty result)")
        return

    # Calculate column widths
    str_rows = [[str(v) if v is not None else "NULL" for v in row] for row in rows]
    widths = [len(col) for col in columns]
    for row in str_rows:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], len(val))

    # Header
    header = " | ".join(col.ljust(widths[i]) for i, col in enumerate(columns))
    separator = "-+-".join("-" * widths[i] for i in range(len(columns)))
    print(header)
    print(separator)

    # Rows
    for row in str_rows:
        line = " | ".join(val.ljust(widths[i]) for i, val in enumerate(row))
        print(line)

    print(f"\n({len(rows)} rows)")


def _format_output(
    conn: duckdb.DuckDBPyConnection,
    sql: str,
    fmt: str,
    output_path: Optional[str],
    max_rows: int,
) -> None:
    """Execute final SQL and output results in the requested format.

    Args:
        conn: DuckDB connection with all views registered
        sql: The final DuckDB SQL query
        fmt: Output format (table, csv, json, parquet)
        output_path: File path for file-based outputs
        max_rows: Maximum rows to return
    """
    # Add LIMIT to prevent runaway results
    limited_sql = f"SELECT * FROM ({sql}) AS _rq LIMIT {max_rows + 1}"
    result = conn.execute(limited_sql)
    columns = [desc[0] for desc in result.description]
    rows = result.fetchall()

    # Check if result exceeded limit
    if len(rows) > max_rows:
        rows = rows[:max_rows]
        _log_progress(
            f"  WARNING: Result truncated to {max_rows:,} rows. "
            f"Add more filters or increase --max-rows."
        )

    if fmt == "table":
        _print_table(columns, rows)

    elif fmt == "csv":
        if output_path:
            with open(output_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(columns)
                writer.writerows(rows)
            _log_progress(f"  CSV written: {output_path} ({len(rows)} rows)")
        else:
            writer = csv.writer(sys.stdout)
            writer.writerow(columns)
            writer.writerows(rows)

    elif fmt == "json":
        data = [dict(zip(columns, row)) for row in rows]
        json_str = json.dumps(data, default=str, indent=2)
        if output_path:
            with open(output_path, "w") as f:
                f.write(json_str)
            _log_progress(f"  JSON written: {output_path} ({len(rows)} rows)")
        else:
            print(json_str)

    elif fmt == "parquet":
        import pyarrow as pa
        import pyarrow.parquet as pq

        # Re-execute without limit wrapper for clean Arrow export
        arrow_result = conn.execute(
            f"SELECT * FROM ({sql}) AS _rq LIMIT {max_rows}"
        ).arrow().read_all()

        if not output_path:
            output_path = str(Path(_load_remote_query_config()["output_dir"]) / "result.parquet")

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(arrow_result, output_path)
        _log_progress(
            f"  Parquet written: {output_path} "
            f"({arrow_result.num_rows} rows, {arrow_result.num_columns} cols)"
        )

    else:
        raise RemoteQueryError(f"Unknown format: {fmt}")


# ---------------------------------------------------------------------------
# Progress logging (stderr so stdout stays clean for data)
# ---------------------------------------------------------------------------

_quiet_mode = False


def _log_progress(msg: str) -> None:
    """Print progress message to stderr (keeps stdout clean for data output)."""
    if not _quiet_mode:
        print(msg, file=sys.stderr)


# ---------------------------------------------------------------------------
# Main execution
# ---------------------------------------------------------------------------

def execute_remote_query(
    sql: str,
    bq_registrations: list[tuple[str, str]],
    fmt: str = "table",
    output: Optional[str] = None,
    max_rows: Optional[int] = None,
    max_bq_rows: Optional[int] = None,
    timeout: Optional[int] = None,
    data_dir: str = "/data/src_data",
    quiet: bool = False,
) -> None:
    """Main execution function for remote queries.

    Args:
        sql: DuckDB SQL query to execute
        bq_registrations: List of (alias, bq_sql) tuples
        fmt: Output format (table, csv, json, parquet)
        output: Output file path (for parquet/csv/json)
        max_rows: Max rows in final result
        max_bq_rows: Max rows per BQ sub-query
        timeout: Query timeout in seconds
        data_dir: Path to data directory
        quiet: Suppress progress messages
    """
    global _quiet_mode
    _quiet_mode = quiet

    config = _load_remote_query_config()
    max_rows = max_rows or config["max_result_rows"]
    max_bq_rows = max_bq_rows or config["max_bq_registration_rows"]
    timeout = timeout or config["timeout_seconds"]
    fmt = fmt or config["default_format"]

    start_time = time.time()

    # Create in-memory DuckDB connection
    conn = duckdb.connect(":memory:")

    try:
        # Step 1: Register local Parquet views
        _log_progress("Setting up local views...")
        local_views = _setup_local_views(conn, data_dir, quiet=quiet)
        _log_progress(f"  {len(local_views)} local views ready")

        # Step 2: Register BQ sub-query results
        if bq_registrations:
            _log_progress(f"Registering {len(bq_registrations)} BQ sub-queries...")
            bq_results = _register_bq_views(
                conn, bq_registrations, max_bq_rows, timeout, quiet=quiet,
            )
            for alias, count in bq_results.items():
                _log_progress(f"  {alias}: {count:,} rows")

        # Step 3: Execute the final DuckDB query
        _log_progress("Executing query...")
        _format_output(conn, sql, fmt, output, max_rows)

        elapsed = time.time() - start_time
        _log_progress(f"Done in {elapsed:.1f}s")

    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------

def _parse_register_bq(value: str) -> tuple[str, str]:
    """Parse --register-bq argument in 'alias=SQL' format.

    Args:
        value: String in format "alias=SELECT ..."

    Returns:
        Tuple of (alias, sql)

    Raises:
        argparse.ArgumentTypeError: If format is invalid
    """
    eq_pos = value.find("=")
    if eq_pos <= 0:
        raise argparse.ArgumentTypeError(
            f"Invalid --register-bq format: '{value}'. "
            f"Expected: 'alias=SELECT ...' (e.g., 'traffic=SELECT report_date, ...')"
        )
    alias = value[:eq_pos].strip()
    sql = value[eq_pos + 1:].strip()
    if not sql:
        raise argparse.ArgumentTypeError(
            f"Empty SQL in --register-bq for alias '{alias}'"
        )
    return alias, sql


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for remote_query CLI."""
    parser = argparse.ArgumentParser(
        description="Execute DuckDB queries spanning local Parquet + remote BigQuery tables",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Local-only query (no BigQuery):
  python -m src.remote_query --sql "SELECT COUNT(*) FROM order_economics"

  # Register BQ result and query it:
  python -m src.remote_query \\
    --register-bq "traffic=SELECT report_date, SUM(visitors) FROM \\`proj.ds.table\\` GROUP BY 1" \\
    --sql "SELECT * FROM traffic ORDER BY report_date"

  # JOIN local + remote:
  python -m src.remote_query \\
    --register-bq "traffic=SELECT ... GROUP BY ..." \\
    --sql "SELECT o.*, t.visitors FROM order_economics o JOIN traffic t ON ..." \\
    --format parquet --output /tmp/result.parquet
        """,
    )
    parser.add_argument(
        "--sql",
        required=False,  # not required when --stdin is used
        default=None,
        help="DuckDB SQL query (executed after all views are registered)",
    )
    parser.add_argument(
        "--register-bq",
        action="append",
        type=_parse_register_bq,
        default=[],
        metavar="ALIAS=SQL",
        dest="bq_registrations",
        help='Register BQ query result as DuckDB view. Format: "alias=BQ_SQL". Repeatable.',
    )
    parser.add_argument(
        "--format",
        choices=["table", "csv", "json", "parquet"],
        default=None,
        dest="fmt",
        help="Output format (default: from config or 'table')",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output file path for parquet/csv/json (default: auto for parquet)",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=None,
        help="Max rows in final result (default: from config)",
    )
    parser.add_argument(
        "--max-bq-rows",
        type=int,
        default=None,
        help="Max rows per BQ sub-query (default: from config)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="Query timeout in seconds (default: from config)",
    )
    parser.add_argument(
        "--data-dir",
        default="/data/src_data",
        help="Parquet data directory (default: /data/src_data)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress messages (stderr)",
    )
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read query spec from stdin as JSON. Avoids shell escaping issues.",
    )
    return parser


def _parse_stdin_query() -> dict:
    """Parse query specification from stdin JSON.

    Expected format:
    {
        "sql": "SELECT ... FROM ...",
        "register_bq": {"alias": "BQ SQL", ...},
        "format": "table",
        "output": "/path/to/file",
        "max_rows": 100000,
        "max_bq_rows": 500000
    }

    Returns:
        Dict with parsed query spec
    """
    raw = sys.stdin.read().strip()
    if not raw:
        raise RemoteQueryError("Empty stdin. Provide JSON query spec.")

    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RemoteQueryError(f"Invalid JSON on stdin: {e}")

    if "sql" not in spec:
        raise RemoteQueryError("JSON must contain 'sql' field.")

    return spec


def main(argv: Optional[list[str]] = None) -> None:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # Setup logging
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stderr,
    )

    try:
        # --stdin mode: read query spec from JSON on stdin (no shell escaping needed)
        if args.stdin:
            spec = _parse_stdin_query()
            bq_regs = [
                (alias, sql) for alias, sql in spec.get("register_bq", {}).items()
            ]
            execute_remote_query(
                sql=spec["sql"],
                bq_registrations=bq_regs,
                fmt=spec.get("format", args.fmt),
                output=spec.get("output", args.output),
                max_rows=spec.get("max_rows", args.max_rows),
                max_bq_rows=spec.get("max_bq_rows", args.max_bq_rows),
                timeout=args.timeout,
                data_dir=args.data_dir,
                quiet=args.quiet,
            )
            return

        # Validate --sql is provided when not using --stdin
        if not args.sql:
            parser.error("--sql is required (or use --stdin for JSON input)")

        execute_remote_query(
            sql=args.sql,
            bq_registrations=args.bq_registrations,
            fmt=args.fmt,
            output=args.output,
            max_rows=args.max_rows,
            max_bq_rows=args.max_bq_rows,
            timeout=args.timeout,
            data_dir=args.data_dir,
            quiet=args.quiet,
        )
    except RemoteQueryError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f"UNEXPECTED ERROR: {e}", file=sys.stderr)
        logger.exception("Unexpected error in remote_query")
        sys.exit(2)


if __name__ == "__main__":
    main()
