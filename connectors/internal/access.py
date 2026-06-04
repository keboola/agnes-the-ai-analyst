"""Per-request scoped query execution for the ``internal`` data source.

Architecture: open ``system.duckdb`` read-only, build a temporary view per
referenced internal table with RBAC applied as a WHERE clause, execute the
user's SQL inside that scope, return rows.

RBAC model:
- Admin (per ``is_user_admin``) sees the table unscoped — no WHERE clause.
- Everyone else gets a single-row-filter projection. The filter column is
  hard-coded per table (``INTERNAL_TABLES``) and the filter value comes
  from the auth-resolved user object — never from user-supplied SQL.

SQL-injection considerations:
- The temp-view DDL interpolates a literal string for the filter value.
- ``username`` for ``usage_*`` is the local-part of an email; we enforce
  the same regex used by the session-file path (alnums + ``._-``) before
  interpolation.
- ``user_id`` for ``audit_log`` is a UUID; we validate the regex before
  interpolation.
- The user's SELECT itself is gated by the same SELECT-only validator the
  ``/api/query`` endpoint uses (denylist of write/DDL keywords + file
  functions). That validator runs in the API layer.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal-table registry — single source of truth
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InternalTable:
    """One internal table mapping.

    Fields:
        registry_id:    the value used in ``table_registry.id`` and what
                        analysts type in SQL (``SELECT * FROM <registry_id>``)
        source_table:   the underlying physical table in ``system.duckdb``
        filter_column:  the column on ``source_table`` carrying the per-row
                        owner. Used for the non-admin scoping clause.
        filter_kind:    how to resolve the filter value from the auth user
                        dict — ``'username'`` (email local-part) or
                        ``'user_id'`` (UUID).
        display_name:   human-readable name (also goes into ``table_registry.name``)
        description:    short blurb (catalog UI + ``agnes catalog`` output)
    """

    registry_id: str
    source_table: str
    filter_column: str
    filter_kind: str  # 'username' | 'user_id'
    display_name: str
    description: str
    legacy_username_column: str | None = None  # backward-compat OR fallback


INTERNAL_TABLES: tuple[InternalTable, ...] = (
    InternalTable(
        registry_id="agnes_sessions",
        source_table="usage_session_summary",
        filter_column="user_id",
        filter_kind="user_id",
        display_name="Agnes sessions",
        description="Claude Code sessions. Also available locally for analysis.",
        legacy_username_column="username",
    ),
    InternalTable(
        registry_id="agnes_telemetry",
        source_table="usage_events",
        filter_column="user_id",
        filter_kind="user_id",
        display_name="Agnes telemetry events",
        description="Tool and skill invocations from Claude Code. Also available locally for analysis.",
        legacy_username_column="username",
    ),
    InternalTable(
        registry_id="agnes_audit",
        source_table="audit_log",
        filter_column="user_id",
        filter_kind="user_id",
        display_name="Agnes audit log",
        description="Server-side actions performed against Agnes. Also available locally for analysis.",
    ),
)

INTERNAL_TABLES_BY_ID: dict[str, InternalTable] = {t.registry_id: t for t in INTERNAL_TABLES}


def is_internal_table(table_id: str) -> bool:
    return table_id in INTERNAL_TABLES_BY_ID


# ---------------------------------------------------------------------------
# RBAC filter resolution
# ---------------------------------------------------------------------------

# `+` allowed in both regexes — RFC 5321 local-parts (e.g. alice+test@x)
# resolve to filesystem usernames with a `+`, and the session-data-dir
# layout already supports the same character class.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._+-]{1,200}$")
_USER_ID_RE = re.compile(r"^[A-Za-z0-9._@:+-]{1,200}$")


class InternalAccessError(Exception):
    """Raised when the caller cannot be safely resolved to a filter value
    or when an internal table is misconfigured."""


def _filter_value(user: dict[str, Any], kind: str) -> str:
    """Derive the per-row filter value from the authenticated user.

    For ``username`` we mirror the session-data-dir convention: the
    local-part of the email. This is the same value the UsageProcessor
    writes into ``usage_events.username`` / ``usage_session_summary``.

    For ``user_id`` we use the ``users.id`` UUID directly — same value
    audit_log writes carry.
    """
    if kind == "username":
        email = (user or {}).get("email", "") or ""
        username = email.split("@")[0] if "@" in email else email
        if not _USERNAME_RE.match(username):
            raise InternalAccessError(f"user email {email!r} does not yield a safe username for scoping")
        return username
    if kind == "user_id":
        uid = (user or {}).get("id", "") or ""
        if not _USER_ID_RE.match(uid):
            raise InternalAccessError(f"user_id {uid!r} fails the safe-identifier check")
        return uid
    raise InternalAccessError(f"unknown filter_kind: {kind!r}")


def build_filter_clause(table: InternalTable, user: dict[str, Any], is_admin: bool) -> str:
    """Return the WHERE clause for one internal table.

    Admins get an empty string (unscoped view). Everyone else get
    ``WHERE <col> = '<value>'`` where value has been regex-validated.

    ``agnes_sessions`` and ``agnes_telemetry`` filter primarily on
    ``user_id`` (stable UUID) but include an OR fallback on
    ``username`` (email local-part) for rows that pre-date the v45
    backfill.  Once all rows carry a non-NULL ``user_id`` the
    ``legacy_username_column`` field can be removed.
    """
    if is_admin:
        return ""
    value = _filter_value(user, table.filter_kind)
    safe = value.replace("'", "''")

    if table.legacy_username_column:
        legacy = _filter_value(user, "username").replace("'", "''")
        return f"WHERE ({table.filter_column} = '{safe}' OR {table.legacy_username_column} = '{legacy}')"

    return f"WHERE {table.filter_column} = '{safe}'"


def sample_internal_rows(
    table: InternalTable, where_clause: str, n: int
) -> list[dict[str, Any]]:
    """Read up to ``n`` rows from an internal table's physical source on the
    ACTIVE state backend (DuckDB or Postgres), applying the RBAC ``where_clause``.

    Internal-table rows live in the state backend; reading them off a raw
    DuckDB connection returns nothing on a Postgres instance (the catalog
    ``/sample`` preview then shows an empty table). Dispatching on ``use_pg()``
    keeps the preview correct on either backend.

    ``where_clause`` comes from :func:`build_filter_clause` — an ANSI
    ``WHERE col = '<escaped>'`` (single quotes doubled, value regex-validated),
    so the same statement runs unchanged on DuckDB and Postgres. The Postgres
    path uses ``exec_driver_sql`` (raw DBAPI) so SQLAlchemy never reinterprets a
    ``:token`` in the literal as a bind parameter.
    """
    n = max(1, int(n))
    sql = f"SELECT * FROM {table.source_table} {where_clause} LIMIT {n}"

    from src.repositories import use_pg

    if use_pg():
        from src.db_pg import get_engine

        with get_engine().connect() as conn:
            return [dict(r) for r in conn.exec_driver_sql(sql).mappings().all()]

    from src.db import get_system_db

    cur = get_system_db().cursor()
    try:
        return cur.execute(sql).fetchdf().to_dict(orient="records")
    finally:
        cur.close()


# ---------------------------------------------------------------------------
# Query execution
# ---------------------------------------------------------------------------

_TABLE_REF_RE = re.compile(
    r"\b(" + "|".join(re.escape(t.registry_id) for t in INTERNAL_TABLES) + r")\b",
    re.IGNORECASE,
)

# Single-quoted SQL string literals (with `''` escape handling). Stripped
# before reference detection so a non-admin can't trigger the internal
# privileged code path by smuggling the alias inside a literal.
_SQL_STRING_LITERAL_RE = re.compile(r"'(?:''|[^'])*'")

# SQL comments — block `/* … */` and `--` line forms. Stripped so a
# comment-wrapped table name (`/**/users/**/`) can't slip past the
# identifier scan downstream.
_SQL_BLOCK_COMMENT_RE = re.compile(r"/\*[\s\S]*?\*/")
_SQL_LINE_COMMENT_RE = re.compile(r"--[^\n]*")


def _strip_sql_noise(sql: str) -> str:
    """Strip string literals + block + line comments so the identifier
    scanners that follow see only structural SQL. Order matters: strip
    literals first (they can contain comment-looking text), then
    comments (which can contain literal-looking text). String content
    is replaced with empty `''` to keep token spacing intact."""
    s = _SQL_STRING_LITERAL_RE.sub("''", sql)
    s = _SQL_BLOCK_COMMENT_RE.sub(" ", s)
    s = _SQL_LINE_COMMENT_RE.sub(" ", s)
    return s


_INTERNAL_ALIAS_NAMES: frozenset[str] = frozenset(t.registry_id.lower() for t in INTERNAL_TABLES)


def _sensitive_table_reference(stripped_sql: str, conn) -> str | None:
    """Return the first non-allowlisted system.duckdb table name that
    appears in ``stripped_sql``, or None if clean.

    Allowlist = the registered ``agnes_*`` internal-table IDs. The
    denylist is derived dynamically from ``information_schema.tables``
    in the system.duckdb main schema, so adding a new sensitive table
    in a future migration is automatically covered without re-editing
    this module.

    ``stripped_sql`` MUST already have string literals and comments
    stripped (see ``_strip_sql_noise``). Identifier scan is
    case-insensitive word-boundary; schema-prefixed (`main.users`) and
    double-quoted (`"users"`) forms both match because the bare name
    still sits between word boundaries.
    """
    rows = conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'").fetchall()
    for (name,) in rows:
        if name is None:
            continue
        if name.lower() in _INTERNAL_ALIAS_NAMES:
            continue
        if re.search(rf"\b{re.escape(name)}\b", stripped_sql, re.IGNORECASE):
            return name
    return None


def find_internal_refs(sql: str) -> list[str]:
    """Word-boundary scan of `sql` for the registered internal table IDs.

    Returns the matched IDs (lowercase, deduped) in declaration order.
    String literals AND comments are stripped first so a literal /
    commented mention of `agnes_sessions` doesn't route the request
    into the privileged internal-query path (review #278 R2 / R3).
    """
    stripped = _strip_sql_noise(sql)
    found = {m.group(1).lower() for m in _TABLE_REF_RE.finditer(stripped)}
    # Preserve declaration order so reasoning about the resulting set is stable.
    return [t.registry_id for t in INTERNAL_TABLES if t.registry_id.lower() in found]


def execute_internal_query(
    system_db_path: str,
    user: dict[str, Any],
    is_admin: bool,
    sql: str,
    limit: int = 1000,
) -> tuple[list[str], list[tuple], bool]:
    """Run a SELECT against per-request-scoped internal views.

    Approach: wrap the user SQL in a CTE prefix that defines one
    ``agnes_*`` alias per referenced internal table, each scoped to the
    caller's rows (admin → unscoped). We then ``SELECT * FROM (<user_sql>)``
    inside the wrapper. DuckDB resolves the CTE aliases when it parses
    the user_sql; the caller never sees the real ``usage_*`` tables.

    Why a CTE wrapper instead of TEMP VIEW or ATTACH:

      - ``duckdb.connect(path, read_only=True)`` from a fresh handle is
        rejected when the app's main connection already holds
        ``system.duckdb`` open RW: "Can't open a connection to same
        database file with a different configuration than existing
        connections".
      - ``ATTACH '<path>' AS sys (READ_ONLY)`` from a :memory: handle is
        rejected with "Binder Error: Unique file handle conflict — the
        database file is already attached by …": DuckDB serialises file
        handles process-wide, so two connections can't reach the same
        ``.duckdb`` file even at different attach points.
      - ``TEMP VIEW`` on the shared singleton connection bleeds across
        concurrent requests (TEMP VIEWS are connection-scoped, and the
        request-handler pool reuses the same handle).

      CTE wrap leaves no residual state on the connection and isolates
      naturally per request. The SQL stays in SELECT space; existing
      keyword-denylist + sanitised-username defenses still apply.
    """
    refs = find_internal_refs(sql)
    if not refs:
        raise InternalAccessError("no internal-table references in SQL")

    # Lazy import to avoid a hard cycle (src.db imports go via repositories
    # which then end up importing access in some test paths).
    from src.db import get_system_db

    # Non-admins are NOT allowed to reference any system.duckdb table
    # outside the registered agnes_* aliases. The CTE wrapper only
    # scopes those aliases; a direct FROM on the base table
    # (`usage_session_summary`, `audit_log`, `users`,
    # `personal_access_tokens`, etc.) would bypass row-level RBAC and
    # leak other users' data. Denylist is derived dynamically from
    # `information_schema.tables` — every table in system.duckdb that
    # is NOT one of the agnes_* aliases is sensitive. This is
    # future-proof: new tables added by later migrations are
    # automatically covered without re-editing this module.
    #
    # Admin path is unaffected — admins have legitimate need to read
    # raw rows, and the filter clause is empty for them anyway.
    if not is_admin:
        stripped = _strip_sql_noise(sql)
        sensitive = _sensitive_table_reference(stripped, get_system_db())
        if sensitive is not None:
            raise InternalAccessError(
                f"non-admin SQL cannot reference table {sensitive!r}; query one of the agnes_* aliases instead"
            )
    cte_parts = []
    for table_id in refs:
        table = INTERNAL_TABLES_BY_ID[table_id]
        where_clause = build_filter_clause(table, user, is_admin)
        cte_parts.append(f"{table.registry_id} AS (SELECT * FROM {table.source_table} {where_clause})")
    cte_prefix = "WITH " + ", ".join(cte_parts)
    wrapped = f"{cte_prefix} SELECT * FROM ({sql}) AS _agnes_user_query"

    conn = get_system_db()
    cursor = conn.cursor()
    try:
        rows = cursor.execute(wrapped).fetchmany(limit + 1)
        cols = [d[0] for d in cursor.description] if cursor.description else []
        truncated = len(rows) > limit
        return cols, rows[:limit], truncated
    finally:
        try:
            cursor.close()
        except Exception:
            logger.exception("close() failed on internal-query cursor")


# ---------------------------------------------------------------------------
# Schema introspection — feeds /api/v2/schema/{id} for internal tables
# ---------------------------------------------------------------------------


def get_schema(system_db_path: str, table_id: str) -> list[dict]:
    """Return the underlying physical schema for an internal table.

    Used by ``/api/v2/schema/<id>`` so ``agnes schema <table>`` works
    against internal sources. Reuses the shared ``system.duckdb``
    connection — same rationale as ``execute_internal_query``: opening
    a parallel handle to the same file is process-wide blocked. The
    information_schema query is read-only and small.

    ``system_db_path`` is kept in the signature for API symmetry with the
    earlier draft, but is unused — the singleton handle already knows the
    path.
    """
    if table_id not in INTERNAL_TABLES_BY_ID:
        return []
    table = INTERNAL_TABLES_BY_ID[table_id]
    from src.db import get_system_db

    cursor = get_system_db().cursor()
    try:
        rows = cursor.execute(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_name = ? ORDER BY ordinal_position",
            [table.source_table],
        ).fetchall()
        return [{"name": r[0], "type": r[1], "nullable": r[2] == "YES"} for r in rows]
    finally:
        try:
            cursor.close()
        except Exception:
            pass
