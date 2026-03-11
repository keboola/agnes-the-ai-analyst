#!/usr/bin/env python3
"""
Standalone Data Profiler — DuckDB-based table profiling for Parquet/CSV files.

Zero external dependencies beyond DuckDB. Produces a comprehensive JSON profile
with column statistics, histograms, alerts, and sample data.

Usage:
    # Profile a single Parquet file
    python standalone_profiler.py data/orders.parquet

    # Profile a directory of Parquet files (treated as one table)
    python standalone_profiler.py data/partitioned_orders/

    # Profile a CSV file
    python standalone_profiler.py data/customers.csv

    # Custom output path
    python standalone_profiler.py data/orders.parquet -o profiles/orders_profile.json

    # Specify primary key for duplicate detection
    python standalone_profiler.py data/orders.parquet --primary-key order_id

    # Composite primary key
    python standalone_profiler.py data/orders.parquet --primary-key "order_id,line_id"

    # Profile multiple files at once
    python standalone_profiler.py data/orders.parquet data/customers.parquet data/products.csv

    # Generate HTML report alongside JSON
    python standalone_profiler.py data/orders.parquet --html

    # Generate HTML from existing profile JSON
    python standalone_profiler.py --from-json profile.json

Output:
    JSON file with table-level and column-level statistics, alerts, histograms,
    top values for categorical columns, and sample rows.
    With --html: self-contained HTML file viewable in any browser.

Requirements:
    pip install duckdb
"""

import argparse
import html as html_mod
import json
import logging
import math
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import duckdb

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("profiler")

# ---------------------------------------------------------------------------
# Profiler configuration
# ---------------------------------------------------------------------------
SAMPLE_THRESHOLD = 500_000  # Sample tables larger than this
SAMPLE_SIZE = 500_000
MAX_CATEGORICAL_DISTINCT = 50  # Treat as categorical if unique <= this
TOP_VALUES_LIMIT = 10  # Number of top values for categorical columns
HISTOGRAM_BINS = 15  # Number of bins for numeric histograms
SAMPLE_ROWS_LIMIT = 5  # Number of sample rows to include
SAMPLE_VALUES_LIMIT = 5  # Number of sample distinct values per column

# Alert thresholds
ALERT_HIGH_MISSING_PCT = 30.0
ALERT_MISSING_PCT = 5.0
ALERT_IMBALANCE_PCT = 60.0
ALERT_ZEROS_PCT = 50.0
ALERT_HIGH_CARDINALITY = 50


# ---------------------------------------------------------------------------
# DuckDB type classification
# ---------------------------------------------------------------------------
def classify_type(duckdb_type: str) -> str:
    """Map a DuckDB type string to a simplified category."""
    t = duckdb_type.upper()
    if t in ("BOOLEAN", "BOOL"):
        return "BOOLEAN"
    if t in ("DATE",):
        return "DATE"
    if "TIMESTAMP" in t:
        return "TIMESTAMP"
    base_type = t.split("(")[0].strip()
    if base_type in (
        "FLOAT", "DOUBLE", "DECIMAL", "REAL", "FLOAT4", "FLOAT8",
        "NUMERIC", "HUGEINT", "INTEGER", "INT", "BIGINT", "SMALLINT",
        "TINYINT", "INT8", "INT4", "INT2", "INT1", "UBIGINT",
        "UINTEGER", "USMALLINT", "UTINYINT",
    ):
        return "NUMERIC"
    return "STRING"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _round(value: Any, digits: int = 2) -> Any:
    """Round a value if it is a float, otherwise return as-is."""
    if value is None:
        return None
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return round(value, digits)
    return value


def _format_number(n: float) -> str:
    """Format large numbers with human-readable suffixes for histogram bin labels."""
    if n is None:
        return "?"
    abs_n = abs(n)
    if abs_n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if abs_n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if abs_n >= 1_000:
        return f"{n / 1_000:.1f}K"
    if isinstance(n, float) and n != int(n):
        return f"{n:.2f}"
    return str(int(n))


def write_json_atomic(path: Path, data: Any) -> None:
    """Write JSON to path atomically via tempfile + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, str(path))
        logger.info("Wrote %s", path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Batch statistics functions
# ---------------------------------------------------------------------------
def _batch_base_stats(
    con: duckdb.DuckDBPyConnection,
    view_name: str,
    columns: List[str],
) -> Dict[str, Tuple[int, int]]:
    """Get non_null and unique counts for all columns in a single query.

    Returns: {col_name: (non_null_count, unique_count)}
    """
    if not columns:
        return {}

    parts = []
    for col_name in columns:
        safe = f'"{col_name}"'
        parts.append(f"COUNT({safe})")
        parts.append(f"COUNT(DISTINCT {safe})")

    sql = f"SELECT {', '.join(parts)} FROM {view_name}"
    row = con.execute(sql).fetchone()

    result: Dict[str, Tuple[int, int]] = {}
    idx = 0
    for col_name in columns:
        result[col_name] = (row[idx], row[idx + 1])
        idx += 2
    return result


def _batch_numeric_stats(
    con: duckdb.DuckDBPyConnection,
    view_name: str,
    numeric_cols: List[str],
) -> Dict[str, Dict[str, Any]]:
    """Get aggregate statistics for all numeric columns in a single query."""
    if not numeric_cols:
        return {}

    parts = []
    for col_name in numeric_cols:
        safe = f'"{col_name}"'
        parts.extend([
            f"MIN({safe})",
            f"MAX({safe})",
            f"AVG({safe})",
            f"MEDIAN({safe})",
            f"STDDEV({safe})",
            f"PERCENTILE_CONT(0.05) WITHIN GROUP (ORDER BY {safe})",
            f"PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY {safe})",
            f"PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY {safe})",
            f"PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY {safe})",
            f"SUM(CASE WHEN {safe} = 0 THEN 1 ELSE 0 END)",
            f"SUM(CASE WHEN {safe} < 0 THEN 1 ELSE 0 END)",
        ])

    sql = f"SELECT {', '.join(parts)} FROM {view_name}"
    row = con.execute(sql).fetchone()

    result: Dict[str, Dict[str, Any]] = {}
    idx = 0
    for col_name in numeric_cols:
        result[col_name] = {
            "min": row[idx], "max": row[idx + 1], "mean": row[idx + 2],
            "median": row[idx + 3], "stddev": row[idx + 4],
            "p5": row[idx + 5], "p25": row[idx + 6],
            "p75": row[idx + 7], "p95": row[idx + 8],
            "zeros": row[idx + 9], "negative": row[idx + 10],
        }
        idx += 11
    return result


def _batch_string_stats(
    con: duckdb.DuckDBPyConnection,
    view_name: str,
    string_cols: List[str],
) -> Dict[str, Dict[str, Any]]:
    """Get string length statistics for all string columns in a single query."""
    if not string_cols:
        return {}

    parts = []
    for col_name in string_cols:
        safe = f'"{col_name}"'
        parts.extend([
            f"MIN(LENGTH({safe}))",
            f"MAX(LENGTH({safe}))",
            f"AVG(LENGTH({safe}))",
        ])

    sql = f"SELECT {', '.join(parts)} FROM {view_name}"
    row = con.execute(sql).fetchone()

    result: Dict[str, Dict[str, Any]] = {}
    idx = 0
    for col_name in string_cols:
        result[col_name] = {
            "min_length": row[idx] if row[idx] is not None else 0,
            "max_length": row[idx + 1] if row[idx + 1] is not None else 0,
            "avg_length": _round(row[idx + 2]) if row[idx + 2] is not None else 0.0,
        }
        idx += 3
    return result


def _batch_date_stats(
    con: duckdb.DuckDBPyConnection,
    view_name: str,
    date_cols: List[str],
    category_map: Dict[str, str],
) -> Dict[str, Dict[str, Any]]:
    """Get date range statistics for all date/timestamp columns in a single query."""
    if not date_cols:
        return {}

    parts = []
    for col_name in date_cols:
        safe = f'"{col_name}"'
        cast_expr = f"CAST({safe} AS DATE)" if category_map[col_name] == "TIMESTAMP" else safe
        parts.extend([
            f"MIN({cast_expr})",
            f"MAX({cast_expr})",
        ])

    sql = f"SELECT {', '.join(parts)} FROM {view_name}"
    row = con.execute(sql).fetchone()

    result: Dict[str, Dict[str, Any]] = {}
    idx = 0
    for col_name in date_cols:
        earliest = row[idx]
        latest = row[idx + 1]
        span_days = None
        if earliest is not None and latest is not None:
            try:
                delta = latest - earliest
                span_days = delta.days if hasattr(delta, "days") else int(delta)
            except (TypeError, ValueError):
                span_days = None
        result[col_name] = {
            "earliest": str(earliest) if earliest is not None else None,
            "latest": str(latest) if latest is not None else None,
            "span_days": span_days,
        }
        idx += 2
    return result


def _batch_boolean_stats(
    con: duckdb.DuckDBPyConnection,
    view_name: str,
    bool_cols: List[str],
) -> Dict[str, Dict[str, Any]]:
    """Get boolean true/false counts for all boolean columns in a single query."""
    if not bool_cols:
        return {}

    parts = []
    for col_name in bool_cols:
        safe = f'"{col_name}"'
        parts.extend([
            f"SUM(CASE WHEN {safe} = TRUE THEN 1 ELSE 0 END)",
            f"SUM(CASE WHEN {safe} = FALSE THEN 1 ELSE 0 END)",
        ])

    sql = f"SELECT {', '.join(parts)} FROM {view_name}"
    row = con.execute(sql).fetchone()

    result: Dict[str, Dict[str, Any]] = {}
    idx = 0
    for col_name in bool_cols:
        true_count = int(row[idx]) if row[idx] is not None else 0
        false_count = int(row[idx + 1]) if row[idx + 1] is not None else 0
        total = true_count + false_count
        result[col_name] = {
            "true_count": true_count,
            "false_count": false_count,
            "true_pct": _round(100.0 * true_count / total) if total > 0 else 0.0,
        }
        idx += 2
    return result


# ---------------------------------------------------------------------------
# Core: profile a single file/table
# ---------------------------------------------------------------------------
def profile_table(
    source_path: Path,
    table_name: Optional[str] = None,
    primary_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Profile a single Parquet file, Parquet directory, or CSV file.

    Args:
        source_path: Path to .parquet file, directory of .parquet files, or .csv file.
        table_name: Display name for the table (defaults to filename stem).
        primary_key: Comma-separated primary key column(s) for duplicate detection.

    Returns:
        Dict with complete profile (table-level + column-level statistics).
    """
    source_path = Path(source_path)
    if table_name is None:
        table_name = source_path.stem

    pk_columns: List[str] = []
    if primary_key:
        pk_columns = [c.strip() for c in primary_key.split(",")]

    con = duckdb.connect()

    # Determine read expression based on file type
    if source_path.is_dir():
        read_expr = f"read_parquet('{source_path}/*.parquet')"
    elif source_path.suffix.lower() == ".csv":
        read_expr = f"read_csv_auto('{source_path}')"
    else:
        read_expr = f"read_parquet('{source_path}')"

    # Get row count to decide on sampling
    total_rows = con.execute(f"SELECT COUNT(*) FROM {read_expr}").fetchone()[0]

    # Materialize into temp table (reads source files once instead of per-query)
    view_name = "tbl"
    sampled = total_rows > SAMPLE_THRESHOLD
    if sampled:
        con.execute(
            f"CREATE TEMP TABLE {view_name} AS SELECT * FROM {read_expr} USING SAMPLE {SAMPLE_SIZE} ROWS"
        )
        working_rows = con.execute(f"SELECT COUNT(*) FROM {view_name}").fetchone()[0]
    else:
        con.execute(f"CREATE TEMP TABLE {view_name} AS SELECT * FROM {read_expr}")
        working_rows = total_rows

    # Column metadata
    col_info = con.execute(f"DESCRIBE {view_name}").fetchall()

    # Classify columns by type
    all_col_names: List[str] = []
    type_map: Dict[str, str] = {}
    category_map: Dict[str, str] = {}
    numeric_cols: List[str] = []
    string_cols: List[str] = []
    date_cols: List[str] = []
    bool_cols: List[str] = []

    for col_row in col_info:
        col_name = col_row[0]
        col_type = col_row[1]
        all_col_names.append(col_name)
        type_map[col_name] = col_type
        category = classify_type(col_type)
        category_map[col_name] = category
        if category == "NUMERIC":
            numeric_cols.append(col_name)
        elif category == "STRING":
            string_cols.append(col_name)
        elif category in ("DATE", "TIMESTAMP"):
            date_cols.append(col_name)
        elif category == "BOOLEAN":
            bool_cols.append(col_name)

    # ---- Batch queries (one scan per type category) ----
    base_stats = _batch_base_stats(con, view_name, all_col_names)

    numeric_batch: Dict[str, Dict[str, Any]] = {}
    try:
        numeric_batch = _batch_numeric_stats(con, view_name, numeric_cols)
    except Exception as exc:
        logger.warning("Batch numeric stats failed: %s", exc)

    string_batch: Dict[str, Dict[str, Any]] = {}
    try:
        string_batch = _batch_string_stats(con, view_name, string_cols)
    except Exception as exc:
        logger.warning("Batch string stats failed: %s", exc)

    date_batch: Dict[str, Dict[str, Any]] = {}
    try:
        date_batch = _batch_date_stats(con, view_name, date_cols, category_map)
    except Exception as exc:
        logger.warning("Batch date stats failed: %s", exc)

    boolean_batch: Dict[str, Dict[str, Any]] = {}
    try:
        boolean_batch = _batch_boolean_stats(con, view_name, bool_cols)
    except Exception as exc:
        logger.warning("Batch boolean stats failed: %s", exc)

    # ---- Build column profiles ----
    columns: List[Dict[str, Any]] = []
    variable_types: Dict[str, int] = {}
    total_null_count = 0
    total_cells = working_rows * len(col_info) if col_info else 0
    first_date_col: Optional[Dict[str, Any]] = None

    for col_name in all_col_names:
        col_type = type_map[col_name]
        category = category_map[col_name]
        safe_col = f'"{col_name}"'
        variable_types[category] = variable_types.get(category, 0) + 1

        non_null, unique_count = base_stats.get(col_name, (0, 0))
        null_count = working_rows - non_null

        completeness_pct = _round(100.0 * non_null / working_rows) if working_rows > 0 else 0.0
        unique_pct = _round(100.0 * unique_count / non_null) if non_null > 0 else 0.0
        missing_pct = _round(100.0 * null_count / working_rows) if working_rows > 0 else 0.0
        is_pk = col_name in pk_columns

        # Sample values
        sample_values: List[str] = []
        try:
            rows = con.execute(
                f"""
                SELECT DISTINCT CAST({safe_col} AS VARCHAR) AS v
                FROM {view_name}
                WHERE {safe_col} IS NOT NULL
                LIMIT {SAMPLE_VALUES_LIMIT}
                """
            ).fetchall()
            sample_values = [r[0] for r in rows if r[0] is not None]
        except Exception:
            pass

        # Alerts
        alerts: List[str] = []
        if unique_count == 1 and null_count == 0:
            alerts.append("constant")
        if unique_pct == 100.0 and null_count == 0 and non_null > 0:
            alerts.append("unique")
        if missing_pct > ALERT_HIGH_MISSING_PCT:
            alerts.append("high_missing")
        elif missing_pct > ALERT_MISSING_PCT:
            alerts.append("missing")

        col_profile: Dict[str, Any] = {
            "name": col_name,
            "type": col_type,
            "type_category": category,
            "completeness_pct": completeness_pct,
            "null_count": null_count,
            "unique_count": unique_count,
            "unique_pct": unique_pct,
            "sample_values": sample_values,
            "is_primary_key": is_pk,
            "alerts": alerts,
        }

        # Type-specific stats
        try:
            if category == "NUMERIC" and col_name in numeric_batch:
                raw = numeric_batch[col_name]
                min_val = _round(raw["min"])
                max_val = _round(raw["max"])
                zeros = int(raw["zeros"]) if raw["zeros"] is not None else 0
                negative = int(raw["negative"]) if raw["negative"] is not None else 0
                zeros_pct = _round(100.0 * zeros / non_null) if non_null > 0 else 0.0
                negative_pct = _round(100.0 * negative / non_null) if non_null > 0 else 0.0

                if zeros_pct > ALERT_ZEROS_PCT and "zeros" not in alerts:
                    alerts.append("zeros")

                # Histogram (FLOOR-based bucketing, works in all DuckDB versions)
                histogram: Dict[str, Any] = {"bins": [], "counts": []}
                if min_val is not None and max_val is not None and min_val != max_val:
                    try:
                        bin_width = (float(max_val) - float(min_val)) / HISTOGRAM_BINS
                        bucket_rows = con.execute(
                            f"""
                            SELECT
                                LEAST(FLOOR((CAST({safe_col} AS DOUBLE) - {float(min_val)}) / {bin_width}), {HISTOGRAM_BINS - 1}) + 1 AS bucket,
                                COUNT(*) AS cnt
                            FROM {view_name}
                            WHERE {safe_col} IS NOT NULL
                            GROUP BY bucket
                            ORDER BY bucket
                            """
                        ).fetchall()

                        bin_labels: List[str] = []
                        bin_counts: List[int] = []
                        bucket_dict = {int(r[0]): int(r[1]) for r in bucket_rows if r[0] is not None}
                        for i in range(1, HISTOGRAM_BINS + 1):
                            lo = float(min_val) + (i - 1) * bin_width
                            hi = float(min_val) + i * bin_width
                            bin_labels.append(f"{_format_number(lo)}-{_format_number(hi)}")
                            bin_counts.append(bucket_dict.get(i, 0))
                        histogram = {"bins": bin_labels, "counts": bin_counts}
                    except Exception as exc:
                        logger.debug("Histogram failed for column %s: %s", col_name, exc)

                col_profile["numeric_stats"] = {
                    "min": min_val,
                    "max": max_val,
                    "mean": _round(raw["mean"]),
                    "median": _round(raw["median"]),
                    "stddev": _round(raw["stddev"]),
                    "p5": _round(raw["p5"]),
                    "p25": _round(raw["p25"]),
                    "p75": _round(raw["p75"]),
                    "p95": _round(raw["p95"]),
                    "zeros": zeros,
                    "zeros_pct": zeros_pct,
                    "negative": negative,
                    "negative_pct": negative_pct,
                    "histogram": histogram,
                }

            elif category == "STRING" and col_name in string_batch:
                sl = string_batch[col_name]
                is_categorical = unique_count <= MAX_CATEGORICAL_DISTINCT

                top_values: List[Dict[str, Any]] = []
                if is_categorical and non_null > 0:
                    rows = con.execute(
                        f"""
                        SELECT {safe_col} AS val, COUNT(*) AS cnt
                        FROM {view_name}
                        WHERE {safe_col} IS NOT NULL
                        GROUP BY {safe_col}
                        ORDER BY cnt DESC
                        LIMIT {TOP_VALUES_LIMIT}
                        """
                    ).fetchall()
                    for row in rows:
                        pct = _round(100.0 * row[1] / non_null) if non_null > 0 else 0.0
                        top_values.append({"value": str(row[0]), "count": row[1], "pct": pct})

                    if top_values and top_values[0]["pct"] > ALERT_IMBALANCE_PCT:
                        if "imbalance" not in alerts:
                            alerts.append("imbalance")
                else:
                    if unique_count > ALERT_HIGH_CARDINALITY and "high_cardinality" not in alerts:
                        alerts.append("high_cardinality")

                col_profile["string_stats"] = {
                    "min_length": sl["min_length"],
                    "max_length": sl["max_length"],
                    "avg_length": sl["avg_length"],
                    "top_values": top_values,
                }

            elif category in ("DATE", "TIMESTAMP") and col_name in date_batch:
                dr = date_batch[col_name]
                cast_expr = f"CAST({safe_col} AS DATE)" if category == "TIMESTAMP" else safe_col

                # Date histogram (YEAR/QUARTER grouping)
                histogram = {"bins": [], "counts": []}
                try:
                    rows = con.execute(
                        f"""
                        SELECT
                            YEAR({cast_expr}) AS yr,
                            QUARTER({cast_expr}) AS qtr,
                            COUNT(*) AS cnt
                        FROM {view_name}
                        WHERE {safe_col} IS NOT NULL
                        GROUP BY yr, qtr
                        ORDER BY yr, qtr
                        """
                    ).fetchall()
                    histogram["bins"] = [f"{int(r[0])}-Q{int(r[1])}" for r in rows]
                    histogram["counts"] = [int(r[2]) for r in rows]
                except Exception as exc:
                    logger.debug("Date histogram failed for %s: %s", col_name, exc)

                col_profile["date_stats"] = {
                    "earliest": dr["earliest"],
                    "latest": dr["latest"],
                    "span_days": dr["span_days"],
                    "histogram": histogram,
                }

                if first_date_col is None and dr["earliest"]:
                    first_date_col = col_profile["date_stats"]

            elif category == "BOOLEAN" and col_name in boolean_batch:
                col_profile["boolean_stats"] = boolean_batch[col_name]

        except Exception as exc:
            logger.warning("Type-specific stats failed for %s: %s", col_name, exc)

        columns.append(col_profile)
        total_null_count += null_count

    # Table-level completeness
    avg_completeness = 0.0
    if columns:
        avg_completeness = _round(
            sum(c["completeness_pct"] for c in columns) / len(columns)
        )
    missing_cells_pct = _round(100.0 * total_null_count / total_cells) if total_cells > 0 else 0.0

    # Duplicate rows (by primary key)
    duplicate_rows = 0
    if pk_columns and working_rows > 0:
        try:
            pk_expr = ", ".join(f'"{c}"' for c in pk_columns)
            distinct_pk = con.execute(
                f"SELECT COUNT(DISTINCT ({pk_expr})) FROM {view_name}"
            ).fetchone()[0]
            duplicate_rows = working_rows - distinct_pk
        except Exception as exc:
            logger.debug("Duplicate check failed: %s", exc)

    # Sample rows
    sample_rows: List[Dict[str, Any]] = []
    try:
        sample_result = con.execute(f"SELECT * FROM {view_name} LIMIT {SAMPLE_ROWS_LIMIT}")
        sample_col_names = [desc[0] for desc in sample_result.description]
        for row in sample_result.fetchall():
            sample_rows.append(
                {sample_col_names[i]: str(v) if v is not None else None for i, v in enumerate(row)}
            )
    except Exception as exc:
        logger.debug("Sample rows failed: %s", exc)

    # Aggregate column alerts to table level
    table_alerts: List[Dict[str, str]] = []
    alert_messages = {
        "constant": "{col} is constant (single value)",
        "unique": "{col} has all unique values",
        "high_missing": "{col} has {pct}% missing values",
        "missing": "{col} has {pct}% missing values",
        "imbalance": "{col} is highly imbalanced (top value {pct}%)",
        "zeros": "{col} has {pct}% zero values",
        "high_cardinality": "{col} has high cardinality ({n} distinct)",
    }
    for col in columns:
        col_alert_name = col.get("name", "")
        missing_pct_val = _round(100.0 - col.get("completeness_pct", 100.0))
        for a in col.get("alerts", []):
            if a in ("high_missing", "missing"):
                msg = alert_messages[a].format(col=col_alert_name, pct=missing_pct_val)
            elif a == "imbalance":
                top_pct = 0.0
                ss = col.get("string_stats", {})
                tv = ss.get("top_values", [])
                if tv:
                    top_pct = tv[0].get("pct", 0.0)
                msg = alert_messages[a].format(col=col_alert_name, pct=top_pct)
            elif a == "zeros":
                ns = col.get("numeric_stats", {})
                msg = alert_messages[a].format(col=col_alert_name, pct=ns.get("zeros_pct", 0.0))
            elif a == "high_cardinality":
                msg = alert_messages[a].format(col=col_alert_name, n=col.get("unique_count", 0))
            else:
                msg = alert_messages.get(a, f"{col_alert_name}: {a}").format(col=col_alert_name)
            table_alerts.append({"column": col_alert_name, "type": a, "message": msg})

    # File size
    file_size_mb = None
    try:
        if source_path.is_dir():
            total_bytes = sum(f.stat().st_size for f in source_path.glob("*.parquet"))
        elif source_path.exists():
            total_bytes = source_path.stat().st_size
        else:
            total_bytes = 0
        file_size_mb = _round(total_bytes / (1024 * 1024))
    except OSError:
        pass

    # Date range from first date column
    date_range = None
    if first_date_col:
        date_range = {
            "earliest": first_date_col.get("earliest"),
            "latest": first_date_col.get("latest"),
            "span_days": first_date_col.get("span_days"),
        }

    con.close()

    return {
        "table_name": table_name,
        "source_path": str(source_path),
        "row_count": total_rows,
        "column_count": len(col_info),
        "file_size_mb": file_size_mb,
        "primary_key": primary_key,
        "avg_completeness": avg_completeness,
        "missing_cells": total_null_count,
        "missing_cells_pct": missing_cells_pct,
        "duplicate_rows": duplicate_rows,
        "variable_types": variable_types,
        "date_range": date_range,
        "alerts": table_alerts,
        "sampled": sampled,
        "columns": columns,
        "sample_rows": sample_rows,
    }


# ---------------------------------------------------------------------------
# HTML report generation
# ---------------------------------------------------------------------------

_TYPE_COLORS = {
    "NUMERIC": "#8b5cf6",
    "STRING": "#3b82f6",
    "DATE": "#f59e0b",
    "TIMESTAMP": "#f59e0b",
    "BOOLEAN": "#10b981",
}

_ALERT_SEVERITY = {
    "high_missing": "e",
    "missing": "w",
    "constant": "i",
    "unique": "i",
    "imbalance": "w",
    "zeros": "w",
    "high_cardinality": "i",
}

_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
  background:#f8fafc;color:#0f172a;line-height:1.5;font-size:14px}
.wrap{max-width:1200px;margin:0 auto;padding:20px 24px 60px}
header{padding:20px 0 16px;border-bottom:1px solid #e2e8f0;margin-bottom:24px}
h1{font-size:22px;font-weight:700}
.meta{color:#64748b;font-size:12px;margin-top:2px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:16px 0}
.card{background:#fff;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,.08);padding:14px 16px;text-align:center}
.card-v{font-size:26px;font-weight:700}.card-l{font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:.05em;margin-top:2px}
.tabs{display:flex;gap:4px;margin-bottom:20px;flex-wrap:wrap}
.tab{padding:7px 14px;border-radius:6px;cursor:pointer;font-size:13px;border:1px solid #e2e8f0;background:#fff;transition:all .15s}
.tab:hover{border-color:#93c5fd}.tab.active{background:#3b82f6;color:#fff;border-color:#3b82f6}
.tsec{display:none}.tsec.active{display:block}
.alerts{margin:12px 0}
.alert{padding:7px 12px;border-radius:6px;margin:3px 0;font-size:12px}
.alert-w{background:#fef3c7;color:#92400e}.alert-e{background:#fee2e2;color:#991b1b}.alert-i{background:#dbeafe;color:#1e40af}
.types{display:flex;gap:6px;margin:10px 0;flex-wrap:wrap}
.tbadge{padding:2px 10px;border-radius:12px;font-size:11px;font-weight:600;color:#fff}
.stitle{font-size:15px;font-weight:600;margin:20px 0 8px}
.col-list{background:#fff;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,.08);overflow:hidden}
.col-hdr{display:grid;grid-template-columns:minmax(140px,1.5fr) 56px minmax(100px,1fr) 90px 50px;
  align-items:center;padding:8px 14px;cursor:pointer;border-bottom:1px solid #f1f5f9;gap:8px;transition:background .1s}
.col-hdr:hover{background:#f8fafc}
.col-hdr-label{cursor:default;font-weight:600;font-size:11px;color:#64748b;border-bottom-width:2px}
.cn{font-weight:600;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pk{color:#f59e0b;font-size:10px;font-weight:700;margin-left:3px}
.ct{font-size:10px;padding:2px 6px;border-radius:4px;text-align:center;font-weight:600;color:#fff;white-space:nowrap}
.cbar-bg{height:5px;background:#e2e8f0;border-radius:3px;overflow:hidden;flex:1}
.cbar{height:100%;border-radius:3px}
.compl{display:flex;align-items:center;gap:6px}
.cpct{font-size:11px;color:#64748b;min-width:32px;text-align:right}
.cuniq{font-size:11px;color:#64748b;text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.calerts span{padding:1px 5px;border-radius:8px;background:#fee2e2;color:#991b1b;font-size:10px}
.col-det{display:none;padding:14px 16px;border-bottom:1px solid #e2e8f0;background:#fafbfc}
.col-det.open{display:block}
.dgrid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:768px){.dgrid{grid-template-columns:1fr}.col-hdr{grid-template-columns:1fr 50px 1fr 70px 40px;font-size:12px}}
.stbl{font-size:12px;width:100%;border-collapse:collapse}
.stbl td{padding:2px 0}.stbl td:first-child{color:#64748b;padding-right:10px;white-space:nowrap}
.stbl td:last-child{font-weight:500;text-align:right}
.histogram{display:flex;align-items:flex-end;gap:1px;height:72px;margin:10px 0}
.h-bar{flex:1;background:#3b82f6;border-radius:2px 2px 0 0;min-width:3px;transition:background .15s;cursor:default;min-height:1px}
.h-bar:hover{background:#2563eb}
.h-labels{display:flex;justify-content:space-between;font-size:9px;color:#94a3b8;margin-top:2px}
.tvr{display:grid;grid-template-columns:110px 1fr 42px 52px;align-items:center;gap:6px;padding:2px 0;font-size:12px}
.tvl{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tvb-bg{height:7px;background:#e2e8f0;border-radius:4px;overflow:hidden}
.tvb{height:100%;background:#3b82f6;border-radius:4px}
.tvp{text-align:right;color:#64748b;font-size:11px}
.tvc{text-align:right;color:#94a3b8;font-size:10px}
.bbar{display:flex;height:18px;border-radius:4px;overflow:hidden;font-size:10px}
.bt{background:#22c55e;color:#fff;display:flex;align-items:center;justify-content:center}
.bf{background:#e2e8f0;color:#64748b;display:flex;align-items:center;justify-content:center}
.svs{display:flex;gap:4px;flex-wrap:wrap;margin-top:6px}
.sv{background:#f1f5f9;padding:1px 7px;border-radius:4px;font-size:11px;color:#475569}
.swrap{margin-top:20px}
.stog{cursor:pointer;color:#3b82f6;font-size:13px;font-weight:500;user-select:none}
.sdata{display:none;margin-top:8px;overflow-x:auto}
.sdata.open{display:block}
table.dt{border-collapse:collapse;font-size:11px;width:100%}
table.dt th{background:#f1f5f9;padding:5px 8px;text-align:left;font-weight:600;border:1px solid #e2e8f0;white-space:nowrap}
table.dt td{padding:5px 8px;border:1px solid #e2e8f0;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.foot{text-align:center;color:#94a3b8;font-size:11px;margin-top:40px;padding-top:16px;border-top:1px solid #e2e8f0}
@media print{.tabs,.stog{display:none}.tsec,.col-det,.sdata{display:block!important}body{background:#fff}.card{box-shadow:none;border:1px solid #e2e8f0}}
"""

_JS = """
function switchTab(n){
  document.querySelectorAll('.tab').forEach(function(t){t.classList.toggle('active',t.dataset.t===n)});
  document.querySelectorAll('.tsec').forEach(function(s){s.classList.toggle('active',s.id==='t-'+n)});
}
function toggleCol(el){el.nextElementSibling.classList.toggle('open')}
function toggleSample(el){el.nextElementSibling.classList.toggle('open')}
"""


def _esc(s: Any) -> str:
    return html_mod.escape(str(s)) if s is not None else ""


def _slug(name: str) -> str:
    return name.replace(" ", "-").replace(".", "-").replace("/", "-")


def _fnum(n: Any) -> str:
    if n is None:
        return "-"
    if isinstance(n, float):
        if n == int(n) and abs(n) < 1e15:
            return f"{int(n):,}"
        return f"{n:,.2f}"
    if isinstance(n, int):
        return f"{n:,}"
    return str(n)


def _compl_color(pct: float) -> str:
    if pct >= 95:
        return "#22c55e"
    if pct >= 70:
        return "#eab308"
    return "#ef4444"


def _render_hist(bins: list, counts: list) -> str:
    if not bins or not counts:
        return ""
    max_c = max(counts) or 1
    bars = []
    for b, c in zip(bins, counts):
        pct = c / max_c * 100
        bars.append(f'<div class="h-bar" style="height:{pct:.0f}%" title="{_esc(b)}: {c:,}"></div>')
    return (
        f'<div class="histogram">{"".join(bars)}</div>'
        f'<div class="h-labels"><span>{_esc(bins[0])}</span><span>{_esc(bins[-1])}</span></div>'
    )


def _render_top_vals(top_values: list) -> str:
    if not top_values:
        return ""
    max_pct = max((tv.get("pct", 0) for tv in top_values), default=1) or 1
    rows = []
    for tv in top_values:
        bar_w = tv.get("pct", 0) / max_pct * 100
        rows.append(
            f'<div class="tvr">'
            f'<span class="tvl" title="{_esc(tv["value"])}">{_esc(str(tv["value"])[:30])}</span>'
            f'<div class="tvb-bg"><div class="tvb" style="width:{bar_w:.0f}%"></div></div>'
            f'<span class="tvp">{tv.get("pct", 0)}%</span>'
            f'<span class="tvc">({_fnum(tv.get("count", 0))})</span>'
            f'</div>'
        )
    return "".join(rows)


def _render_col_detail(col: dict) -> str:
    parts: List[str] = []
    ns = col.get("numeric_stats")
    if ns:
        parts.append('<div class="dgrid"><div><table class="stbl">')
        for label, key in [
            ("Min", "min"), ("Max", "max"), ("Mean", "mean"),
            ("Median", "median"), ("Std Dev", "stddev"),
            ("P5", "p5"), ("P25", "p25"), ("P75", "p75"), ("P95", "p95"),
            ("Zeros", "zeros"), ("Zeros %", "zeros_pct"),
            ("Negative", "negative"), ("Negative %", "negative_pct"),
        ]:
            parts.append(f'<tr><td>{label}</td><td>{_fnum(ns.get(key))}</td></tr>')
        parts.append('</table></div><div>')
        h = ns.get("histogram", {})
        parts.append(_render_hist(h.get("bins", []), h.get("counts", [])))
        parts.append('</div></div>')

    ss = col.get("string_stats")
    if ss:
        parts.append('<table class="stbl">')
        parts.append(f'<tr><td>Min length</td><td>{_fnum(ss.get("min_length"))}</td></tr>')
        parts.append(f'<tr><td>Max length</td><td>{_fnum(ss.get("max_length"))}</td></tr>')
        parts.append(f'<tr><td>Avg length</td><td>{_fnum(ss.get("avg_length"))}</td></tr>')
        parts.append('</table>')
        tv = ss.get("top_values", [])
        if tv:
            parts.append('<div style="font-size:12px;font-weight:600;color:#64748b;margin-top:10px">Top Values</div>')
            parts.append(_render_top_vals(tv))

    ds = col.get("date_stats")
    if ds:
        parts.append('<div class="dgrid"><div><table class="stbl">')
        parts.append(f'<tr><td>Earliest</td><td>{_esc(ds.get("earliest", "-"))}</td></tr>')
        parts.append(f'<tr><td>Latest</td><td>{_esc(ds.get("latest", "-"))}</td></tr>')
        parts.append(f'<tr><td>Span</td><td>{_fnum(ds.get("span_days"))} days</td></tr>')
        parts.append('</table></div><div>')
        h = ds.get("histogram", {})
        parts.append(_render_hist(h.get("bins", []), h.get("counts", [])))
        parts.append('</div></div>')

    bs = col.get("boolean_stats")
    if bs:
        tc, fc = bs.get("true_count", 0), bs.get("false_count", 0)
        tp = bs.get("true_pct", 0)
        fp = round(100 - tp, 1) if tp else 0
        parts.append(
            f'<div class="bbar">'
            f'<div class="bt" style="width:{tp}%">True {tp}% ({tc:,})</div>'
            f'<div class="bf" style="width:{fp}%">False {fp}% ({fc:,})</div>'
            f'</div>'
        )

    sv = col.get("sample_values", [])
    if sv:
        parts.append('<div style="margin-top:8px;font-size:11px;color:#64748b">Sample values:</div>')
        parts.append('<div class="svs">')
        for v in sv:
            parts.append(f'<span class="sv">{_esc(str(v)[:50])}</span>')
        parts.append('</div>')

    return "".join(parts)


def generate_html_report(profile_data: Dict[str, Any], output_path: Path) -> None:
    """Generate a standalone HTML report from profile data.

    Args:
        profile_data: Full profile dict with "tables" key.
        output_path: Path to write the HTML file.
    """
    tables = profile_data.get("tables", {})
    generated_at = profile_data.get("generated_at", "")
    if not tables:
        logger.warning("No tables in profile data")
        return

    total_tables = len(tables)
    total_rows = sum(t.get("row_count", 0) for t in tables.values())
    total_cols = sum(t.get("column_count", 0) for t in tables.values())
    compl_vals = [t.get("avg_completeness", 0) for t in tables.values()]
    avg_compl = round(sum(compl_vals) / len(compl_vals), 1) if compl_vals else 0
    total_alerts = sum(len(t.get("alerts", [])) for t in tables.values())
    table_names = list(tables.keys())

    h: List[str] = []
    h.append('<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">')
    h.append('<meta name="viewport" content="width=device-width,initial-scale=1">')
    h.append('<title>Data Profile Report</title>')
    h.append(f'<style>{_CSS}</style></head><body><div class="wrap">')

    # Header
    h.append('<header>')
    h.append('<h1>Data Profile Report</h1>')
    h.append(f'<div class="meta">Generated: {_esc(generated_at)}</div>')
    h.append('</header>')

    # Summary cards
    h.append('<div class="cards">')
    for val, label in [
        (_fnum(total_tables), "Tables"),
        (_fnum(total_rows), "Total Rows"),
        (_fnum(total_cols), "Total Columns"),
        (f"{avg_compl}%", "Avg Completeness"),
        (_fnum(total_alerts), "Alerts"),
    ]:
        h.append(f'<div class="card"><div class="card-v">{val}</div><div class="card-l">{label}</div></div>')
    h.append('</div>')

    # Table tabs
    if total_tables > 1:
        h.append('<div class="tabs">')
        for i, name in enumerate(table_names):
            act = " active" if i == 0 else ""
            sl = _slug(name)
            h.append(f'<div class="tab{act}" data-t="{sl}" onclick="switchTab(\'{sl}\')">{_esc(name)}</div>')
        h.append('</div>')

    # Table sections
    for i, (name, tbl) in enumerate(tables.items()):
        act = " active" if i == 0 or total_tables == 1 else ""
        sl = _slug(name)
        h.append(f'<section class="tsec{act}" id="t-{sl}">')
        h.append(f'<h2 class="stitle" style="font-size:18px;margin-bottom:12px">{_esc(name)}</h2>')

        # Stat cards
        h.append('<div class="cards">')
        rc = tbl.get("row_count", 0)
        cc = tbl.get("column_count", 0)
        tc = tbl.get("avg_completeness", 0)
        sz = tbl.get("file_size_mb")
        dupes = tbl.get("duplicate_rows", 0)
        sampled = tbl.get("sampled", False)
        for val, label in [
            (_fnum(rc), "Rows"),
            (_fnum(cc), "Columns"),
            (f"{tc}%", "Completeness"),
            (f"{sz} MB" if sz is not None else "-", "File Size"),
        ]:
            h.append(f'<div class="card"><div class="card-v">{val}</div><div class="card-l">{label}</div></div>')
        dr = tbl.get("date_range")
        if dr and dr.get("earliest"):
            h.append(
                f'<div class="card"><div class="card-v" style="font-size:14px">'
                f'{_esc(dr["earliest"])} &mdash; {_esc(dr["latest"])}</div>'
                f'<div class="card-l">Date Range ({_fnum(dr.get("span_days"))} days)</div></div>'
            )
        if dupes:
            h.append(f'<div class="card"><div class="card-v" style="color:#ef4444">{_fnum(dupes)}</div><div class="card-l">Duplicate Rows</div></div>')
        if sampled:
            h.append(f'<div class="card"><div class="card-v" style="font-size:14px;color:#f59e0b">Sampled</div><div class="card-l">500K rows</div></div>')
        h.append('</div>')

        # Variable types
        vt = tbl.get("variable_types", {})
        if vt:
            h.append('<div class="types">')
            for cat, cnt in sorted(vt.items()):
                color = _TYPE_COLORS.get(cat, "#6b7280")
                h.append(f'<span class="tbadge" style="background:{color}">{cat} {cnt}</span>')
            h.append('</div>')

        # Alerts
        alerts = tbl.get("alerts", [])
        if alerts:
            h.append('<div class="alerts">')
            for a in alerts:
                sev = _ALERT_SEVERITY.get(a.get("type", ""), "i")
                h.append(f'<div class="alert alert-{sev}">{_esc(a.get("message", ""))}</div>')
            h.append('</div>')

        # Column list
        columns = tbl.get("columns", [])
        if columns:
            h.append('<div class="stitle">Columns</div>')
            h.append('<div class="col-list">')
            # Header row
            h.append('<div class="col-hdr col-hdr-label">')
            h.append('<div>Name</div><div style="text-align:center">Type</div>')
            h.append('<div style="padding-left:4px">Completeness</div>')
            h.append('<div style="text-align:right">Unique</div><div></div>')
            h.append('</div>')

            for col in columns:
                cname = col.get("name", "")
                cat = col.get("type_category", "STRING")
                ctype = col.get("type", "")
                cpct = col.get("completeness_pct", 0)
                uniq = col.get("unique_count", 0)
                upct = col.get("unique_pct", 0)
                ca = col.get("alerts", [])
                is_pk = col.get("is_primary_key", False)
                color = _TYPE_COLORS.get(cat, "#6b7280")
                cc_col = _compl_color(cpct)
                pk_html = '<span class="pk">PK</span>' if is_pk else ""
                alert_html = f'<span>{len(ca)}</span>' if ca else ""

                h.append('<div class="col-hdr" onclick="toggleCol(this)">')
                h.append(f'<div class="cn" title="{_esc(cname)}">{_esc(cname)}{pk_html}</div>')
                h.append(f'<div><span class="ct" style="background:{color}" title="{_esc(ctype)}">{_esc(cat[:4])}</span></div>')
                h.append(f'<div class="compl"><div class="cbar-bg"><div class="cbar" style="width:{cpct}%;background:{cc_col}"></div></div><span class="cpct">{cpct}%</span></div>')
                h.append(f'<div class="cuniq">{_fnum(uniq)} ({upct}%)</div>')
                h.append(f'<div class="calerts">{alert_html}</div>')
                h.append('</div>')
                h.append(f'<div class="col-det">{_render_col_detail(col)}</div>')

            h.append('</div>')

        # Sample data
        sample_rows = tbl.get("sample_rows", [])
        if sample_rows:
            h.append('<div class="swrap">')
            h.append(f'<div class="stog" onclick="toggleSample(this)">&#9654; Sample Data ({len(sample_rows)} rows)</div>')
            h.append('<div class="sdata"><table class="dt">')
            headers = list(sample_rows[0].keys())
            h.append('<tr>' + ''.join(f'<th>{_esc(hd)}</th>' for hd in headers) + '</tr>')
            for row in sample_rows:
                h.append('<tr>' + ''.join(
                    f'<td title="{_esc(row.get(hd, ""))}">{_esc(str(row.get(hd, ""))[:60])}</td>'
                    for hd in headers
                ) + '</tr>')
            h.append('</table></div></div>')

        h.append('</section>')

    # Footer + JS
    h.append('<div class="foot">Generated by Standalone Data Profiler</div>')
    h.append(f'<script>{_JS}</script>')
    h.append('</div></body></html>')

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(h), encoding="utf-8")
    logger.info("Wrote HTML report: %s", output_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Profile Parquet/CSV files and output JSON statistics + optional HTML report.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s data/orders.parquet
  %(prog)s data/orders.parquet --primary-key order_id --html
  %(prog)s data/orders.parquet data/customers.csv -o profiles.json --html
  %(prog)s --from-json profile.json
        """,
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="Parquet file(s), directory of Parquet files, or CSV file(s) to profile",
    )
    parser.add_argument(
        "-o", "--output",
        default="profile.json",
        help="Output JSON file path (default: profile.json)",
    )
    parser.add_argument(
        "--primary-key",
        default=None,
        help="Comma-separated primary key column(s) for duplicate detection",
    )
    parser.add_argument(
        "--html",
        action="store_true",
        help="Also generate a standalone HTML report",
    )
    parser.add_argument(
        "--from-json",
        metavar="PATH",
        default=None,
        help="Generate HTML report from existing profile JSON (no profiling)",
    )
    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Suppress info logging",
    )
    args = parser.parse_args()

    if args.quiet:
        logging.getLogger("profiler").setLevel(logging.WARNING)

    # Mode 1: Generate HTML from existing JSON
    if args.from_json:
        json_path = Path(args.from_json)
        if not json_path.exists():
            logger.error("File not found: %s", json_path)
            sys.exit(1)
        with open(json_path) as f:
            profile_data = json.load(f)
        html_path = json_path.with_suffix(".html")
        generate_html_report(profile_data, html_path)
        logger.info("Done: HTML report at %s", html_path)
        return

    # Mode 2: Profile files
    if not args.files:
        parser.error("Provide files to profile, or use --from-json")

    profiles: Dict[str, Any] = {}
    success = 0
    errors = 0

    for file_path_str in args.files:
        file_path = Path(file_path_str)
        if not file_path.exists():
            logger.error("File not found: %s", file_path)
            errors += 1
            continue

        try:
            logger.info("Profiling %s ...", file_path)
            profile = profile_table(
                source_path=file_path,
                primary_key=args.primary_key,
            )
            profiles[profile["table_name"]] = profile
            success += 1
            logger.info(
                "  %s: %d rows, %d cols, %d alerts",
                profile["table_name"],
                profile["row_count"],
                profile["column_count"],
                len(profile["alerts"]),
            )
        except Exception as exc:
            logger.error("Failed to profile %s: %s", file_path, exc)
            errors += 1

    if not profiles:
        logger.error("No tables profiled successfully")
        sys.exit(1)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "version": "1.0",
        "tables": profiles,
    }

    output_path = Path(args.output)
    write_json_atomic(output_path, output)

    # Generate HTML if requested
    if args.html:
        html_path = output_path.with_suffix(".html")
        generate_html_report(output, html_path)

    logger.info("Done: %d profiled, %d errors. Output: %s", success, errors, output_path)


if __name__ == "__main__":
    main()
