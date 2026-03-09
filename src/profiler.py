"""
Data Profiler - YData-inspired profiling using DuckDB.

Reads Parquet files from the server's data directory, computes comprehensive
statistics for each table and column, generates alerts, extracts sample rows,
merges with metadata from existing sources, and outputs a single profiles.json file.

Usage:
    python -m src.profiler
"""

import json
import logging
import math
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import duckdb
import yaml

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

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

# Paths - configurable via environment or defaults for server
DATA_DIR = Path(os.environ.get("PROFILER_DATA_DIR", "/data/src_data"))
DOCS_DIR = Path(os.environ.get("PROFILER_DOCS_DIR", str(Path(__file__).parent.parent / "docs")))
PARQUET_DIR = DATA_DIR / "parquet"
METADATA_DIR = DATA_DIR / "metadata"
SYNC_STATE_PATH = METADATA_DIR / "sync_state.json"
PROFILES_OUTPUT_PATH = METADATA_DIR / "profiles.json"
METRICS_YML_PATH = DOCS_DIR / "metrics.yml"
METRICS_DIR = DOCS_DIR / "metrics"
DATA_DESCRIPTION_PATH = DOCS_DIR / "data_description.md"

# Jira tables - loaded dynamically if Jira connector is enabled
# The Jira connector stores partitioned parquet files in PARQUET_DIR/jira/
def _load_jira_tables() -> tuple:
    """Load Jira table definitions if the connector directory exists."""
    jira_dir = PARQUET_DIR / "jira"
    if not jira_dir.exists():
        return jira_dir, []
    return jira_dir, [
        {
            "name": "jira_issues",
            "subdir": "issues",
            "description": "Jira issues. Key fields: issue_key, summary, description, status, priority, assignee, created_at, resolved_at.",
            "primary_key": "issue_key",
            "foreign_keys": [],
        },
        {
            "name": "jira_comments",
            "subdir": "comments",
            "description": "Comments on Jira issues. Key fields: comment_id, issue_key, author_email, body, created_at.",
            "primary_key": "comment_id",
            "foreign_keys": [{"column": "issue_key", "references": "jira_issues.issue_key", "description": "Parent issue"}],
        },
        {
            "name": "jira_attachments",
            "subdir": "attachments",
            "description": "Attachment metadata with local file paths. Key fields: attachment_id, issue_key, filename, local_path, size_bytes, mime_type.",
            "primary_key": "attachment_id",
            "foreign_keys": [{"column": "issue_key", "references": "jira_issues.issue_key", "description": "Parent issue"}],
        },
        {
            "name": "jira_changelog",
            "subdir": "changelog",
            "description": "History of all field changes on issues. Key fields: change_id, issue_key, field_name, from_value, to_value, changed_at.",
            "primary_key": "change_id",
            "foreign_keys": [{"column": "issue_key", "references": "jira_issues.issue_key", "description": "Parent issue"}],
        },
        {
            "name": "jira_issuelinks",
            "subdir": "issuelinks",
            "description": "Links between Jira issues (blocks, duplicates, relates to). Key fields: issue_key, link_id, link_type, direction, linked_issue_key.",
            "primary_key": "link_id",
            "foreign_keys": [
                {"column": "issue_key", "references": "jira_issues.issue_key", "description": "Source issue"},
                {"column": "linked_issue_key", "references": "jira_issues.issue_key", "description": "Target linked issue"},
            ],
        },
        {
            "name": "jira_remote_links",
            "subdir": "remote_links",
            "description": "External links attached to issues (Confluence pages, Slack threads, etc.). Key fields: issue_key, remote_link_id, url, title.",
            "primary_key": "remote_link_id",
            "foreign_keys": [{"column": "issue_key", "references": "jira_issues.issue_key", "description": "Parent issue"}],
        },
    ]


JIRA_PARQUET_DIR, JIRA_TABLES = _load_jira_tables()


# ---------------------------------------------------------------------------
# Dataclasses for parsed metadata
# ---------------------------------------------------------------------------
class ForeignKeyInfo:
    """Foreign key definition from data_description.md."""

    def __init__(self, column: str, references: str, description: Optional[str] = None):
        self.column = column
        self.references = references
        self.description = description


class TableInfo:
    """Table definition parsed from data_description.md YAML."""

    def __init__(
        self,
        table_id: str,
        name: str,
        description: str,
        primary_key: str,
        sync_strategy: str,
        foreign_keys: Optional[List[ForeignKeyInfo]] = None,
        partition_by: Optional[str] = None,
        partition_granularity: Optional[str] = None,
    ):
        self.id = table_id
        self.name = name
        self.description = description
        self.primary_key = primary_key
        self.sync_strategy = sync_strategy
        self.foreign_keys = foreign_keys or []
        self.partition_by = partition_by
        self.partition_granularity = partition_granularity

    def get_primary_key_columns(self) -> List[str]:
        return [col.strip() for col in self.primary_key.split(",")]

    def is_partitioned(self) -> bool:
        if self.sync_strategy == "partitioned":
            return True
        if self.sync_strategy == "incremental" and self.partition_by:
            return True
        return False


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
    # Strip parameters for parameterized types (e.g. DECIMAL(18,3) -> DECIMAL)
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
    """Write JSON to *path* atomically via tempfile + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.chmod(tmp_path, 0o644)  # readable by webapp (www-data)
        os.replace(tmp_path, str(path))
        logger.info("Wrote %s", path)
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Metadata loading
# ---------------------------------------------------------------------------
def parse_data_description(path: Path) -> Tuple[List[TableInfo], Dict[str, str]]:
    """Parse data_description.md and return (tables, folder_mapping)."""
    if not path.exists():
        logger.warning("data_description.md not found at %s", path)
        return [], {}

    content = path.read_text()
    yaml_pattern = r"```yaml\n(.*?)```"
    yaml_matches = re.findall(yaml_pattern, content, re.DOTALL)

    all_tables: List[TableInfo] = []
    folder_mapping: Dict[str, str] = {}

    for block in yaml_matches:
        try:
            data = yaml.safe_load(block)
        except yaml.YAMLError as exc:
            logger.error("YAML parse error: %s", exc)
            continue
        if not data:
            continue
        if "folder_mapping" in data:
            folder_mapping.update(data["folder_mapping"])
        if "tables" in data:
            for td in data["tables"]:
                fk_list = []
                for fk in td.get("foreign_keys", []) or []:
                    fk_list.append(
                        ForeignKeyInfo(
                            column=fk["column"],
                            references=fk["references"],
                            description=fk.get("description"),
                        )
                    )
                all_tables.append(
                    TableInfo(
                        table_id=td["id"],
                        name=td["name"],
                        description=td["description"],
                        primary_key=td["primary_key"],
                        sync_strategy=td["sync_strategy"],
                        foreign_keys=fk_list,
                        partition_by=td.get("partition_by"),
                        partition_granularity=td.get("partition_granularity"),
                    )
                )

    return all_tables, folder_mapping


def load_sync_state(path: Path) -> Dict[str, Any]:
    """Load sync_state.json and return dict keyed by table_id."""
    if not path.exists():
        logger.warning("sync_state.json not found at %s", path)
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Error reading sync_state.json: %s", exc)
        return {}


def load_metrics(path: Path) -> Dict[str, List[str]]:
    """Scan individual metric YAML files and return {table_name: [metric_name, ...]}.

    Scans individual category YAML files in docs/metrics/*/*.yml for table references.
    The path argument can be either docs/metrics.yml (legacy) or docs/metrics/ directory.
    """
    table_metrics: Dict[str, List[str]] = {}

    # Resolve to the metrics directory (docs/metrics/)
    if path.is_file() or path.name == "metrics.yml":
        metrics_dir = path.parent / "metrics"
    else:
        metrics_dir = path
    if metrics_dir.is_dir():
        for yml_file in sorted(metrics_dir.glob("*/*.yml")):
            try:
                with open(yml_file) as f:
                    data = yaml.safe_load(f)
            except (yaml.YAMLError, OSError) as exc:
                logger.warning("Error reading metric file %s: %s", yml_file, exc)
                continue

            if not data:
                continue

            # Individual metric files contain a list with one metric definition
            metric_list = data if isinstance(data, list) else [data]
            for metric in metric_list:
                if not isinstance(metric, dict):
                    continue
                metric_name = metric.get("name", "")
                if not metric_name:
                    continue
                # Single-table metrics
                table = metric.get("table")
                if table:
                    table_metrics.setdefault(table, []).append(metric_name)
                # Multi-table metrics
                for t in metric.get("tables", []):
                    table_metrics.setdefault(t, []).append(metric_name)

    if not table_metrics:
        logger.warning("No metric-table mappings found in %s", metrics_dir)

    return table_metrics


def load_metric_file_map(path: Path) -> Dict[str, str]:
    """Return {metric_name: 'category/file.yml'} for linking metrics in the UI."""
    metric_files: Dict[str, str] = {}
    if path.is_file() or path.name == "metrics.yml":
        metrics_dir = path.parent / "metrics"
    else:
        metrics_dir = path
    if not metrics_dir.is_dir():
        return metric_files

    for yml_file in sorted(metrics_dir.glob("*/*.yml")):
        try:
            with open(yml_file) as f:
                data = yaml.safe_load(f)
        except (yaml.YAMLError, OSError):
            continue
        if not data:
            continue
        metric_list = data if isinstance(data, list) else [data]
        for metric in metric_list:
            if isinstance(metric, dict) and metric.get("name"):
                # Relative path: "sales_revenue/mrr.yml"
                rel_path = f"{yml_file.parent.name}/{yml_file.name}"
                metric_files[metric["name"]] = rel_path
    return metric_files


def get_parquet_path(table: TableInfo, folder_mapping: Dict[str, str]) -> Path:
    """Compute the Parquet file/directory path for a table."""
    bucket_name = ".".join(table.id.split(".")[:-1])
    folder_name = folder_mapping.get(bucket_name, bucket_name)
    base = PARQUET_DIR / folder_name
    if table.is_partitioned():
        return base / table.name  # directory
    return base / f"{table.name}.parquet"


# ---------------------------------------------------------------------------
# Related tables enrichment
# ---------------------------------------------------------------------------
def compute_related_tables(
    table: TableInfo, all_tables: List[TableInfo]
) -> List[Dict[str, str]]:
    """Build related_tables list from foreign key metadata (both directions)."""
    related: List[Dict[str, str]] = []

    # Outgoing: this table's foreign keys
    for fk in table.foreign_keys:
        parts = fk.references.split(".")
        ref_table = parts[0]
        ref_col = parts[1] if len(parts) > 1 else parts[0]
        related.append(
            {
                "table": ref_table,
                "join_column": fk.column,
                "foreign_column": ref_col,
                "direction": "belongs_to",
                "description": fk.description or f"References {ref_table}",
            }
        )

    # Incoming: other tables that reference this table
    for other in all_tables:
        if other.name == table.name:
            continue
        for fk in other.foreign_keys:
            parts = fk.references.split(".")
            ref_table = parts[0]
            ref_col = parts[1] if len(parts) > 1 else parts[0]
            if ref_table == table.name:
                related.append(
                    {
                        "table": other.name,
                        "join_column": ref_col,
                        "foreign_column": fk.column,
                        "direction": "has_many",
                        "description": fk.description or f"Referenced by {other.name}",
                    }
                )

    return related


# ---------------------------------------------------------------------------
# Metrics referencing a table
# ---------------------------------------------------------------------------
def get_metrics_for_table(
    table_name: str, metrics_map: Dict[str, List[str]]
) -> List[str]:
    """Return list of metric names that reference a given table."""
    return sorted(set(metrics_map.get(table_name, [])))


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
    """Get aggregate statistics for all numeric columns in a single query.

    DuckDB aggregations ignore NULLs, so no WHERE filter needed.
    """
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
    """Get string length statistics for all string columns in a single query.

    LENGTH(NULL) returns NULL which aggregations skip automatically.
    """
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
# Table profiling
# ---------------------------------------------------------------------------
def profile_table(
    table: TableInfo,
    parquet_path: Path,
    all_tables: List[TableInfo],
    sync_state: Dict[str, Any],
    metrics_map: Dict[str, List[str]],
    metric_file_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Profile a single table and return its complete profile dict.

    Optimized flow:
      1. Materialize parquet into TEMP TABLE (read files once)
      2. Batch base stats (COUNT, COUNT DISTINCT) for all columns
      3. Batch type-specific aggregates (numeric, string, date, boolean)
      4. Per-column: sample values, histograms, top values (can't batch)
      5. Assemble profiles
    """
    con = duckdb.connect()

    # Determine read expression
    if parquet_path.is_dir():
        read_expr = f"read_parquet('{parquet_path}/*.parquet')"
    else:
        read_expr = f"read_parquet('{parquet_path}')"

    # Get row count to decide on sampling
    total_rows = con.execute(f"SELECT COUNT(*) FROM {read_expr}").fetchone()[0]

    # Materialize into temp table — reads parquet files once instead of per-query
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
    pk_columns = table.get_primary_key_columns()

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

        # Sample values (per-column, fast on materialized table)
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

        # Type-specific stats (batch results + per-column histograms/top_values)
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

                # Histogram (per-column — each has different width_bucket ranges)
                histogram: Dict[str, Any] = {"bins": [], "counts": []}
                if min_val is not None and max_val is not None and min_val != max_val:
                    try:
                        bucket_rows = con.execute(
                            f"""
                            SELECT
                                width_bucket(
                                    CAST({safe_col} AS DOUBLE),
                                    CAST({min_val} AS DOUBLE),
                                    CAST({max_val} AS DOUBLE) + 0.001,
                                    {HISTOGRAM_BINS}
                                ) AS bucket,
                                COUNT(*) AS cnt
                            FROM {view_name}
                            WHERE {safe_col} IS NOT NULL
                            GROUP BY bucket
                            ORDER BY bucket
                            """
                        ).fetchall()

                        bin_width = (float(max_val) - float(min_val)) / HISTOGRAM_BINS
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

                # Date histogram (per-column — YEAR/QUARTER grouping)
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
            logger.debug("Duplicate check failed for %s: %s", table.name, exc)

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
        logger.debug("Sample rows failed for %s: %s", table.name, exc)

    # Aggregate column alerts to table level (detailed objects for UI)
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

    # Sync state enrichment
    table_sync = sync_state.get(table.id, {})
    file_size_mb = table_sync.get("file_size_mb")
    last_sync = table_sync.get("last_sync")
    sync_strategy_state = table_sync.get("strategy", table.sync_strategy)

    # Date range from first date column
    date_range = None
    if first_date_col:
        date_range = {
            "earliest": first_date_col.get("earliest"),
            "latest": first_date_col.get("latest"),
            "span_days": first_date_col.get("span_days"),
        }

    # Related tables
    related_tables = compute_related_tables(table, all_tables)

    # Metrics - include file path for UI linking
    metric_names = get_metrics_for_table(table.name, metrics_map)
    _file_map = metric_file_map or {}
    used_by_metrics = [
        {"name": m, "file": _file_map.get(m, "")} for m in metric_names
    ]

    con.close()

    return {
        "table_id": table.id,
        "description": table.description,
        "primary_key": table.primary_key,
        "sync_strategy": sync_strategy_state,
        "row_count": total_rows,
        "column_count": len(col_info),
        "file_size_mb": _round(file_size_mb) if file_size_mb is not None else None,
        "avg_completeness": avg_completeness,
        "missing_cells": total_null_count,
        "missing_cells_pct": missing_cells_pct,
        "duplicate_rows": duplicate_rows,
        "variable_types": variable_types,
        "date_range": date_range,
        "alerts": table_alerts,
        "sampled": sampled,
        "last_sync": last_sync,
        "related_tables": related_tables,
        "used_by_metrics": used_by_metrics,
        "columns": columns,
        "sample_rows": sample_rows,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    """Run profiler on all tables."""
    logger.info("Starting profiler")
    logger.info("  PARQUET_DIR:  %s", PARQUET_DIR)
    logger.info("  METADATA_DIR: %s", METADATA_DIR)
    logger.info("  DOCS_DIR:     %s", DOCS_DIR)

    # Parse data_description.md
    tables, folder_mapping = parse_data_description(DATA_DESCRIPTION_PATH)
    if not tables:
        logger.error("No tables found in data_description.md - aborting")
        return
    logger.info("Parsed %d tables from data_description.md", len(tables))

    # Load sync state
    sync_state = load_sync_state(SYNC_STATE_PATH)

    # Load metrics
    metrics_map = load_metrics(METRICS_YML_PATH)
    metric_file_map = load_metric_file_map(METRICS_YML_PATH)

    # Load existing profiles for fallback (preserve data for tables that fail)
    existing_profiles: Dict[str, Any] = {}
    try:
        if PROFILES_OUTPUT_PATH.exists():
            with open(PROFILES_OUTPUT_PATH) as f:
                existing_data = json.load(f)
            existing_profiles = existing_data.get("tables", {})
            logger.info("Loaded %d existing profiles for fallback", len(existing_profiles))
    except Exception as exc:
        logger.warning("Could not load existing profiles: %s", exc)

    # Build Jira TableInfo objects for relationship computation
    jira_table_infos: List[TableInfo] = []
    if JIRA_PARQUET_DIR.is_dir():
        for jt in JIRA_TABLES:
            fk_list = []
            for fk in jt.get("foreign_keys", []):
                fk_list.append(
                    ForeignKeyInfo(
                        column=fk["column"],
                        references=fk["references"],
                        description=fk.get("description"),
                    )
                )
            jira_table_infos.append(
                TableInfo(
                    table_id=f"jira.{jt['name']}",
                    name=jt["name"],
                    description=jt["description"],
                    primary_key=jt["primary_key"],
                    sync_strategy="partitioned",
                    foreign_keys=fk_list,
                    partition_by="created_at",
                    partition_granularity="month",
                )
            )

    # Combined table list for relationship computation (data_description + Jira)
    all_tables_combined = list(tables) + jira_table_infos

    # Profile each table
    profiles: Dict[str, Any] = {}
    success_count = 0
    skip_count = 0
    error_count = 0

    for table in tables:
        parquet_path = get_parquet_path(table, folder_mapping)

        # For partitioned tables (directories), check if any .parquet files exist
        if parquet_path.is_dir():
            parquet_files = list(parquet_path.glob("*.parquet"))
            if not parquet_files:
                logger.warning("Skipping %s: no parquet files in %s", table.name, parquet_path)
                skip_count += 1
                continue
        elif not parquet_path.exists():
            logger.warning("Skipping %s: parquet not found at %s", table.name, parquet_path)
            skip_count += 1
            continue

        try:
            logger.info("Profiling %s ...", table.name)
            profile = profile_table(table, parquet_path, all_tables_combined, sync_state, metrics_map, metric_file_map)
            profiles[table.name] = profile
            success_count += 1
            logger.info(
                "  %s: %d rows, %d cols, %d alerts",
                table.name,
                profile["row_count"],
                profile["column_count"],
                len(profile["alerts"]),
            )
        except Exception as exc:
            logger.error("Failed to profile %s: %s", table.name, exc)
            error_count += 1
            # Preserve old profile if available
            if table.name in existing_profiles:
                profiles[table.name] = existing_profiles[table.name]
                profiles[table.name]["_stale"] = True
                logger.info("  Using cached profile for %s", table.name)

    # Profile Jira / Support tables (partitioned parquet, not in data_description.md)
    if JIRA_PARQUET_DIR.is_dir():
        logger.info("Profiling Jira/Support tables from %s", JIRA_PARQUET_DIR)
        for jira_table in jira_table_infos:
            # Find the matching JIRA_TABLES config for subdir
            jt_config = next((jt for jt in JIRA_TABLES if jt["name"] == jira_table.name), None)
            if not jt_config:
                continue
            jira_path = JIRA_PARQUET_DIR / jt_config["subdir"]
            if not jira_path.is_dir():
                logger.warning("Skipping %s: directory %s not found", jira_table.name, jira_path)
                skip_count += 1
                continue
            parquet_files = list(jira_path.glob("*.parquet"))
            if not parquet_files:
                logger.warning("Skipping %s: no parquet files in %s", jira_table.name, jira_path)
                skip_count += 1
                continue

            try:
                logger.info("Profiling %s ...", jira_table.name)
                profile = profile_table(
                    jira_table, jira_path, all_tables_combined, sync_state, metrics_map, metric_file_map
                )
                profiles[jira_table.name] = profile
                success_count += 1
                logger.info(
                    "  %s: %d rows, %d cols, %d alerts",
                    jira_table.name,
                    profile["row_count"],
                    profile["column_count"],
                    len(profile["alerts"]),
                )
            except Exception as exc:
                logger.error("Failed to profile %s: %s", jira_table.name, exc)
                error_count += 1
                # Preserve old profile if available
                if jira_table.name in existing_profiles:
                    profiles[jira_table.name] = existing_profiles[jira_table.name]
                    profiles[jira_table.name]["_stale"] = True
                    logger.info("  Using cached profile for %s", jira_table.name)
    else:
        logger.info("Jira parquet dir %s not found - skipping Jira tables", JIRA_PARQUET_DIR)

    # Build output
    output = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "version": "1.0",
        "tables": profiles,
    }

    # Write output
    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    write_json_atomic(PROFILES_OUTPUT_PATH, output)

    logger.info(
        "Profiling complete: %d profiled, %d skipped, %d errors",
        success_count,
        skip_count,
        error_count,
    )


if __name__ == "__main__":
    main()
