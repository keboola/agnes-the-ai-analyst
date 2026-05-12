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

TABLE usage_plugin_daily
    day DATE NOT NULL
    source VARCHAR NOT NULL
    ref_id VARCHAR NOT NULL
    invocations INTEGER
    distinct_users INTEGER
    distinct_sessions INTEGER
    PRIMARY KEY (day, source, ref_id)
"""

SYSTEM_PROMPT = """You translate natural-language questions into DuckDB SELECT statements over a telemetry schema.

Rules:
1. Output a single SQL statement only.
2. SELECT-only — never INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, ATTACH, COPY, PRAGMA, or any side-effect statement.
3. No semicolons except optionally one at the end.
4. No CTE that contains a write.
5. Prefer rollup tables (`usage_tool_daily`, `usage_plugin_daily`) for date-range aggregations; use `usage_events` for forensic detail.
6. Use DuckDB-flavor SQL: `CURRENT_DATE`, `INTERVAL 7 DAY`, `DATE_TRUNC('week', ...)`, `EPOCH`, etc.
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
        "sql": {"type": "string", "description": "DuckDB SELECT statement"},
        "rationale": {"type": "string", "description": "1-2 sentence explanation"},
    },
    "required": ["sql", "rationale"],
}


# Mutating keywords that disqualify a query
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|CREATE|ALTER|ATTACH|DETACH|COPY|PRAGMA|VACUUM|REINDEX|EXPORT|IMPORT|LOAD|INSTALL)\b",
    re.IGNORECASE,
)

# DuckDB table-valued / scalar functions that can read arbitrary files,
# make network calls, or expose internal secrets.  We match only when
# the name is immediately followed by optional whitespace + "(" so that
# benign column names like "read_count" or "shell_name" are not rejected.
_FORBIDDEN_FUNCS = re.compile(
    r"\b("
    r"read_csv|read_json|read_json_auto|read_parquet|read_text|read_file|read_blob|"
    r"parquet_scan|json_scan|"
    r"glob|"
    r"http_get|http_post|http_head|"
    r"aws_secret|azure|gcs|iceberg_scan|delta_scan|hudi_scan|"
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
