"""Text-to-SQL for telemetry — schema-aware prompt + SELECT-only validator.

The LLM is asked to translate a natural-language question into a single
DuckDB SELECT statement against the v41 usage_* tables. The server then
validates the result is SELECT-only and executes it.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


# Schema digest — embedded in the system prompt so the LLM knows the columns.
# Keep aligned with src/db.py v41 DDL.
SCHEMA_DIGEST = """
TABLE usage_events
    id VARCHAR PRIMARY KEY
    session_id VARCHAR NOT NULL
    session_file VARCHAR NOT NULL
    username VARCHAR NOT NULL
    event_uuid VARCHAR
    parent_uuid VARCHAR
    event_type VARCHAR NOT NULL    -- 'tool_use' | 'slash_command' | 'subagent' | 'mcp_call'
    tool_name VARCHAR              -- 'Bash', 'Read', 'Skill', 'Task', 'mcp__github__create_issue', etc.
    skill_name VARCHAR             -- canonical skill name when tool_name='Skill'
    subagent_type VARCHAR
    command_name VARCHAR           -- when event_type='slash_command'
    is_error BOOLEAN
    source VARCHAR NOT NULL        -- 'curated' | 'flea' | 'builtin'
    ref_id VARCHAR                 -- '<marketplace_id>/<plugin_name>' | store_entities.id | NULL
    model VARCHAR
    cwd VARCHAR
    occurred_at TIMESTAMP NOT NULL
    processor_version INTEGER NOT NULL

TABLE usage_session_summary
    session_file VARCHAR PRIMARY KEY
    session_id VARCHAR NOT NULL
    username VARCHAR NOT NULL
    started_at TIMESTAMP
    ended_at TIMESTAMP
    active_seconds INTEGER
    wall_seconds INTEGER
    user_messages INTEGER
    assistant_messages INTEGER
    tool_calls INTEGER
    tool_errors INTEGER
    skill_invocations INTEGER
    subagent_dispatches INTEGER
    mcp_calls INTEGER
    slash_commands INTEGER
    distinct_tools INTEGER
    distinct_skills INTEGER
    primary_model VARCHAR

TABLE usage_tool_daily
    day DATE NOT NULL
    tool_name VARCHAR NOT NULL
    source VARCHAR NOT NULL
    invocations INTEGER
    error_count INTEGER
    distinct_users INTEGER
    distinct_sessions INTEGER
    PRIMARY KEY (day, tool_name, source)

TABLE usage_marketplace_item_daily
    day DATE NOT NULL
    source VARCHAR NOT NULL              -- 'curated' | 'flea' | 'builtin'
    type VARCHAR NOT NULL                -- 'plugin' | 'skill' | 'agent' | 'command'
    parent_plugin VARCHAR NOT NULL       -- '' for top-level plugins; '<plugin>' for inner items
    name VARCHAR NOT NULL
    count INTEGER
    distinct_users INTEGER
    error_count INTEGER
    PRIMARY KEY (day, source, type, parent_plugin, name)

TABLE usage_marketplace_item_window
    period_label VARCHAR NOT NULL        -- 'last_7d' | 'last_30d'
    source VARCHAR NOT NULL
    type VARCHAR NOT NULL
    parent_plugin VARCHAR NOT NULL
    name VARCHAR NOT NULL
    invocations INTEGER
    distinct_users INTEGER               -- TRUE distinct across the window (not summed from daily)
    refreshed_at TIMESTAMP
    PRIMARY KEY (period_label, source, type, parent_plugin, name)
"""

SYSTEM_PROMPT = """You translate natural-language questions into PostgreSQL SELECT statements over a telemetry schema.

Rules:
1. Output a single SQL statement only.
2. SELECT-only — never INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, ATTACH, COPY, PRAGMA, or any side-effect statement.
3. No semicolons except optionally one at the end.
4. No CTE that contains a write.
5. Prefer rollup tables for date-range aggregations: `usage_tool_daily` (per-tool), `usage_marketplace_item_daily` (per marketplace item — plugin / skill / agent / command keyed by source + type + parent_plugin + name), and `usage_marketplace_item_window` (true-distinct counts across `last_7d` / `last_30d` snapshots). Use `usage_events` for forensic detail. The pre-v48 `usage_plugin_daily` and `usage_attribution_*` tables are gone — do not reference them.
6. Use PostgreSQL-flavor SQL:
   - Date literal: `DATE '2026-01-01'`
   - Now: `CURRENT_DATE`, `CURRENT_TIMESTAMP`, `NOW()`
   - Interval: `INTERVAL '7 days'` (quoted string, plural unit). NEVER `INTERVAL 7 DAY` — that is DuckDB / MySQL syntax and is a syntax error in Postgres.
   - Date arithmetic: `CURRENT_DATE - INTERVAL '30 days'`, `occurred_at - INTERVAL '1 hour'`
   - Truncation: `DATE_TRUNC('week', occurred_at)`
   - Epoch: `EXTRACT(EPOCH FROM occurred_at)` (NOT `EPOCH(occurred_at)`)
   - Quantile: `PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY col)` (NOT `approx_quantile`)
   - Prefix match: `col LIKE 'prefix%'` (NOT `starts_with(col, 'prefix')`)
   - Cast: `CAST(x AS INTEGER)` or `x::INTEGER`
   - String coalesce on JSONB: `params->>'key'` for text, `(params->>'key')::INTEGER` for cast.
7. Limit large result sets — default `LIMIT 100` unless the question asks for ALL rows.

Schema:
""" + SCHEMA_DIGEST + """

Return JSON with:
- sql: the SELECT statement
- rationale: 1-2 sentences explaining the query
"""


RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "sql": {"type": "string", "description": "PostgreSQL SELECT statement"},
        "rationale": {"type": "string", "description": "1-2 sentence explanation"},
    },
    "required": ["sql", "rationale"],
}


# Mutating keywords that disqualify a query
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|CREATE|ALTER|ATTACH|DETACH|COPY|PRAGMA|VACUUM|REINDEX|EXPORT|IMPORT|LOAD|INSTALL)\b",
    re.IGNORECASE,
)

# Postgres-side functions that can read arbitrary files, expose internal
# secrets, or escape the SELECT-only sandbox. We match only when the name
# is immediately followed by optional whitespace + "(" so that benign
# column names ("read_count", "shell_name") are not rejected. The
# DuckDB-side scanners (``read_csv`` / ``read_parquet`` / ``glob`` /
# ``http_get`` / ``duckdb_*`` reflection) are kept as defence in depth in
# case a future operator points usage_ask at a DuckDB analytics engine —
# they're a no-op on Postgres.
_FORBIDDEN_FUNCS = re.compile(
    r"\b("
    # Postgres file / RPC / inspection surface
    r"pg_read_file|pg_read_binary_file|pg_ls_dir|pg_ls_logdir|pg_stat_file|"
    r"pg_read_server_files|lo_import|lo_export|"
    r"dblink|dblink_connect|dblink_exec|dblink_open|"
    r"pg_sleep|pg_sleep_for|pg_sleep_until|pg_terminate_backend|pg_cancel_backend|"
    r"current_setting|set_config|"
    # DuckDB-side scanners (defence in depth)
    r"read_csv|read_json|read_json_auto|read_parquet|read_text|read_file|read_blob|"
    r"parquet_scan|json_scan|"
    r"glob|"
    r"http_get|http_post|http_head|"
    r"aws_secret|azure|gcs|iceberg_scan|delta_scan|hudi_scan|"
    r"pragma_table_info|pragma_storage_info|pragma_database_size|pragma_database_list|"
    r"duckdb_tables|duckdb_columns|duckdb_views|duckdb_indexes|duckdb_schemas|"
    r"duckdb_extensions|duckdb_functions|duckdb_settings|duckdb_databases|duckdb_secrets|"
    r"shell|system"
    r")\s*\(",
    re.IGNORECASE,
)


def validate_select_only(sql: str) -> str:
    """Validate the SQL is a single SELECT statement; raise ValueError otherwise.

    Returns the trimmed SQL (one trailing semicolon ok).
    """
    if not sql or not sql.strip():
        raise ValueError("SQL is empty")
    s = sql.strip()
    # Strip trailing semicolon for inspection
    if s.endswith(";"):
        s = s[:-1].strip()
    # Reject if there's still a semicolon inside (multiple statements)
    if ";" in s:
        raise ValueError("multiple statements are not allowed")
    # Reject mutating keywords anywhere (checked before the SELECT/WITH test so
    # that e.g. "INSERT INTO …" raises "forbidden keyword: INSERT" rather than
    # the less informative "only SELECT/WITH queries are allowed").
    m = _FORBIDDEN.search(s)
    if m:
        raise ValueError(f"forbidden keyword: {m.group(1)}")
    # Reject DuckDB file-read / network / system functions (function-call form only).
    m2 = _FORBIDDEN_FUNCS.search(s)
    if m2:
        raise ValueError(f"forbidden function: {m2.group(1).lower()}")
    # Must start with SELECT or WITH
    head = s.lstrip().split(None, 1)
    if not head or head[0].upper() not in ("SELECT", "WITH"):
        raise ValueError(f"only SELECT/WITH queries are allowed; got: {head[0] if head else '?'}")
    return s


def build_prompt(question: str) -> str:
    """Build the user-content prompt sent to the LLM."""
    return f"Question: {question.strip()}\n\nReturn the SQL + rationale as JSON."
