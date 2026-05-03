"""Query endpoint — execute SQL against server DuckDB."""

import logging
import os
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import duckdb

from app.auth.access import is_user_admin
from app.auth.dependencies import get_current_user, _get_db
from app.instance_config import get_value
from src.db import get_analytics_db_readonly
from src.rbac import get_accessible_tables
from src.repositories.table_registry import TableRegistryRepository

# Imported at module level so tests can monkeypatch via
# `app.api.query._bq_dry_run_bytes` without resolving lazy imports inside
# the handler (reaches the patched attribute on each call). Same for
# get_bq_access — sibling module, dep direction doesn't matter (both are
# leaves under app.api).
from app.api.v2_quota import _build_quota_tracker, QuotaExceededError
from app.api.v2_scan import _bq_dry_run_bytes
from connectors.bigquery.access import get_bq_access, BqAccessError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/query", tags=["query"])

# Issue #160 §4.3.1 — direct `bq.<dataset>.<source_table>` references in user
# SQL. Matches all 16 cases verified empirically (fully-quoted, unquoted,
# mixed quoting, case-insensitive, inside CTE bodies, multiple in one stmt).
# Lookahead `(?=\W|$)` works where `\b` doesn't (after a closing quote).
# Negative lookbehind `(?<![\w.])` rejects `other_bq.x.y` and `x.bq.y.z`.
BQ_PATH = re.compile(
    r'(?<![\w.])bq\s*\.\s*("[^"]+"|\w+)\s*\.\s*("[^"]+"|\w+)(?=\W|$)',
    re.IGNORECASE,
)


def _default_remote_query_cap_bytes() -> int:
    """5 GiB default cap on /api/query BQ-touching scans. Configurable via
    `api.query.bq_max_scan_bytes` in /admin/server-config.
    """
    raw = get_value("api", "query", "bq_max_scan_bytes", default=5_368_709_120)
    try:
        return int(raw) if raw is not None else 5_368_709_120
    except (TypeError, ValueError):
        return 5_368_709_120


class QueryRequest(BaseModel):
    sql: str
    limit: int = 1000


class QueryResponse(BaseModel):
    columns: list
    rows: list
    row_count: int
    truncated: bool = False


@router.post("", response_model=QueryResponse)
async def execute_query(
    request: QueryRequest,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Execute SQL against the server analytics DuckDB."""
    sql_lower = request.sql.strip().lower()

    # Block everything except SELECT
    blocked = [
        "drop ", "delete ", "insert ", "update ", "alter ", "create ",
        "copy ", "attach ", "detach ", "load ", "install ",
        "export ", "import ", "pragma ", "call ",
        # File access functions
        "read_csv", "read_json", "read_parquet", "read_text",
        "write_csv", "write_parquet", "read_blob", "read_ndjson",
        "parquet_scan", "parquet_metadata", "parquet_schema",
        "json_scan", "csv_scan",
        "query_table", "iceberg_scan", "delta_scan",
        # #160: bigquery_query() bypasses the registry / RBAC entirely
        # (it runs an arbitrary BQ jobs API call against any reachable
        # dataset). Wrap views created by the BQ extractor use it inside
        # CREATE VIEW bodies, but those run via DuckDB's view resolution at
        # query time — user-submitted SQL never contains the function name.
        "bigquery_query",
        "glob(", "list_files",
        "'/", '"/','http://', 'https://', 's3://', 'gcs://',
        # DuckDB metadata (leaks schema info regardless of RBAC)
        "information_schema", "duckdb_tables", "duckdb_columns",
        "duckdb_databases", "duckdb_settings", "duckdb_functions",
        "duckdb_views", "duckdb_indexes", "duckdb_schemas",
        "pragma_table_info", "pragma_storage_info",
        # Relative path traversal
        "'../", '"../',
        # Multiple statements
        ";",
    ]
    if any(keyword in sql_lower for keyword in blocked):
        raise HTTPException(status_code=400, detail="Only single SELECT queries are allowed")

    # Accept any whitespace (newline, tab, space) after the keyword so
    # multi-line SQL doesn't 400 on `SELECT\n  col, ...`.
    import re as _re
    if not _re.match(r"^(select|with)\s", sql_lower):
        raise HTTPException(status_code=400, detail="Query must start with SELECT or WITH")

    # Get allowed tables for this user
    allowed = get_accessible_tables(user, conn)

    analytics = get_analytics_db_readonly()
    try:
        if allowed is not None:  # None = admin, sees all
            # Get all views in analytics DB
            all_views = {row[0] for row in analytics.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_type='VIEW'"
            ).fetchall()}

            # Check if query references any forbidden tables (word-boundary match)
            forbidden = all_views - set(allowed)
            for table in forbidden:
                pattern = r'\b' + re.escape(table.lower()) + r'\b'
                if re.search(pattern, sql_lower):
                    raise HTTPException(status_code=403, detail=f"Access denied to table '{table}'")

        # ---- #160 BQ remote-row guardrail + RBAC patch -------------------
        dry_run_set, blocked_bq_path = _bq_guardrail_inputs(
            request.sql, sql_lower, conn, user, allowed,
        )
        if blocked_bq_path is not None:
            raise HTTPException(status_code=403, detail=blocked_bq_path)

        if dry_run_set:
            _enforce_remote_bq_quota_and_cap(
                user_id=user.get("id") or user.get("email") or "anon",
                dry_run_set=dry_run_set,
                sql=request.sql,
            )

        # Open in read-only mode for extra safety
        result = analytics.execute(request.sql).fetchmany(request.limit + 1)
        columns = [desc[0] for desc in analytics.description] if analytics.description else []
        truncated = len(result) > request.limit
        rows = result[:request.limit]

        # Post-flight: bill the dry-run estimate against the user's daily
        # quota. Do this AFTER execute so a downstream failure (e.g. BQ
        # outage) doesn't strand the user with charged-but-unrun bytes.
        if dry_run_set:
            user_id = user.get("id") or user.get("email") or "anon"
            try:
                _build_quota_tracker().record_bytes(
                    user_id, sum(b for _, _, b in dry_run_set),
                )
            except Exception:
                # record_bytes is documented as never-raising; defensive guard.
                logger.warning("quota record_bytes failed for user=%s", user_id)

        # Convert to serializable types
        serializable_rows = []
        for row in rows:
            serializable_rows.append([
                str(v) if v is not None and not isinstance(v, (int, float, bool, str)) else v
                for v in row
            ])
        return QueryResponse(
            columns=columns,
            rows=serializable_rows,
            row_count=len(serializable_rows),
            truncated=truncated,
        )
    except HTTPException:
        raise
    except Exception as e:
        # If DuckDB raised "Table … does not exist" for a referenced name,
        # check whether that name belongs to a registry row in
        # `query_mode='materialized'` that hasn't yet been materialized in
        # this instance's analytics.duckdb. Materialized rows produce a
        # parquet at `${DATA_DIR}/extracts/<source>/data/<id>.parquet` but
        # the orchestrator is `_meta`-driven and only creates master views
        # for connectors that emit `_meta` rows — so on a fresh instance
        # (or before the first scheduler tick) the master view doesn't
        # exist yet and the operator gets a confusing "table does not
        # exist" with no path forward. Surface a materialize-aware hint
        # instead of DuckDB's bare error.
        msg = str(e)
        helpful = _materialized_hint_for_query_error(conn, request.sql, msg)
        if helpful:
            raise HTTPException(status_code=400, detail=helpful)
        raise HTTPException(status_code=400, detail=f"Query error: {msg}")
    finally:
        analytics.close()


def _materialized_hint_for_query_error(
    conn: duckdb.DuckDBPyConnection, sql: str, error_msg: str,
) -> Optional[str]:
    """Return a materialize-aware error message if the failed query
    references a registry row whose `query_mode='materialized'` and which
    has no master view in analytics.duckdb yet, OR ``None`` to fall back
    to DuckDB's raw error.

    The detection scans each materialized row's id/name against the SQL
    text; a hit means the operator picked a name that exists in the
    registry but isn't queryable in this instance. The hint is the same
    in both arms of the OR — it tells them what the table needs and what
    they can do today (`da sync` or query `bq."dataset"."table"`
    directly using the bucket/source_table from the registry row).
    """
    # Cheap fast-path — only inspect the registry when DuckDB's error
    # actually mentions a missing table. Avoids registry round-trip on
    # every parse/cast/permission failure.
    el = error_msg.lower()
    if "does not exist" not in el and "table with name" not in el:
        return None
    try:
        repo = TableRegistryRepository(conn)
        rows = repo.list_all()
    except Exception:
        # Registry read failed for whatever reason — don't compound the
        # error response by hiding the original DuckDB message.
        return None
    sql_l = sql.lower()
    for r in rows:
        if (r.get("query_mode") or "") != "materialized":
            continue
        # Match by id or by name; either could appear in the SQL.
        candidates = {r.get("id"), r.get("name")}
        for cand in candidates:
            if not cand:
                continue
            cand_l = str(cand).lower()
            # Word-boundary-ish check — `\b` doesn't match `.` so
            # `bq.dataset.cand` would still hit, which is fine for the
            # hint path (the operator is referring to the same table).
            if re.search(r"\b" + re.escape(cand_l) + r"\b", sql_l):
                return _build_materialized_hint(r)
    return None


def _build_materialized_hint(row: dict) -> str:
    """Format the user-facing hint for a materialized row that's not yet
    queryable. Includes the table id, the bucket/source_table when the
    row carries them, and concrete operator next steps."""
    tid = row.get("id") or row.get("name") or "<unknown>"
    bucket = row.get("bucket")
    source_table = row.get("source_table")
    direct_hint = ""
    if bucket and source_table:
        # BigQuery: `bq."dataset"."table"`; Keboola: `kbc."bucket"."table"`.
        # Pick the alias by source_type so the hint is copy-pasteable.
        alias = "bq" if (row.get("source_type") or "") == "bigquery" else "kbc"
        direct_hint = (
            f' or query the source directly via {alias}."{bucket}".'
            f'"{source_table}"'
        )
    return (
        f"Table {tid!r} is registered as query_mode='materialized' but is "
        f"not yet materialized in this instance's analytics views. Run "
        f"`da sync` (or wait for the scheduler tick / hit POST "
        f"/api/sync/trigger) to materialize the parquet"
        f"{direct_hint}."
    )


def _bq_guardrail_inputs(
    sql: str,
    sql_lower: str,
    sys_conn: duckdb.DuckDBPyConnection,
    user: dict,
    allowed: Optional[list],
):
    """Two-pass scan over user SQL for the upcoming BQ guardrail + RBAC patch.

    Returns a tuple `(dry_run_set, blocked_bq_path)`:

    - `dry_run_set` is a list of `(bucket, source_table, est_bytes)` triples
      identifying every BigQuery row the request will scan. The caller dry-runs
      each and bills the sum against the user's daily quota.

    - `blocked_bq_path` is a structured-detail dict for the caller to raise
      HTTPException(403) with, when user SQL contains a direct
      `bq."<ds>"."<tbl>"` reference that either points at an unregistered
      path (`bq_path_not_registered`) or registered but the caller has no
      grant on the registered name (`bq_path_access_denied`). None when the
      RBAC check passes.
    """
    repo = TableRegistryRepository(sys_conn)

    # 1. Bare-name pass: look up registered remote-BQ names that appear in
    # the user SQL as word-boundary tokens. Reuses the same regex shape as
    # the existing forbidden-table loop above.
    dry_run: list = []
    seen_paths: set = set()
    accessible_set = set(allowed) if allowed is not None else None
    for r in repo.list_by_source("bigquery"):
        if (r.get("query_mode") or "") != "remote":
            continue
        bucket = r.get("bucket")
        source_table = r.get("source_table")
        name = r.get("name")
        if not (bucket and source_table and name):
            continue
        if accessible_set is not None and name not in accessible_set:
            # Forbidden-table loop above will have rejected the request
            # before we get here. Defensive skip.
            continue
        pattern = r'\b' + re.escape(str(name).lower()) + r'\b'
        if re.search(pattern, sql_lower):
            key = (bucket.lower(), source_table.lower())
            if key not in seen_paths:
                seen_paths.add(key)
                dry_run.append((bucket, source_table, 0))  # bytes filled at dry-run

    # 2. Direct bq.<ds>.<tbl> pass: every match must point at a registered
    # row. Run BEFORE adding to dry_run so unregistered paths fail-fast.
    is_admin = is_user_admin(user.get("id") or user.get("email") or "", sys_conn)
    for m in BQ_PATH.finditer(sql):
        bucket_raw = m.group(1).strip('"')
        source_table_raw = m.group(2).strip('"')
        row = repo.find_by_bq_path(bucket_raw, source_table_raw)
        if row is None:
            return [], {
                "reason": "bq_path_not_registered",
                "path": f'bq."{bucket_raw}"."{source_table_raw}"',
                "hint": (
                    "Direct bq.* references must point to a registered table. "
                    "Register via `da admin register-table` or use the "
                    "registered name from `da catalog`."
                ),
            }
        # Row exists. Per-name grant check (non-admin only).
        if not is_admin:
            if accessible_set is None or row["name"] not in accessible_set:
                return [], {
                    "reason": "bq_path_access_denied",
                    "path": f'bq."{bucket_raw}"."{source_table_raw}"',
                    "registered_as": row["name"],
                }
        # Add to dry-run set if not already covered by bare-name pass.
        bucket = row["bucket"]
        source_table = row["source_table"]
        if bucket and source_table:
            key = (bucket.lower(), source_table.lower())
            if key not in seen_paths:
                seen_paths.add(key)
                dry_run.append((bucket, source_table, 0))

    return dry_run, None


def _enforce_remote_bq_quota_and_cap(*, user_id: str, dry_run_set: list, sql: str) -> None:
    """Pre-flight check + dry-run + cap enforcement for /api/query BQ paths.

    1. `check_daily_budget` — over-cap users get 429 BEFORE any BQ work.
    2. `with quota.acquire(user_id)` — concurrent slot guard.
    3. Dry-run each `(bucket, source_table)` via the existing
       `_bq_dry_run_bytes` helper. Sum bytes.
    4. If sum > cap → 400 `remote_scan_too_large` with structured detail.

    Mutates `dry_run_set` in place: the third tuple element (bytes) is
    populated with the per-path dry-run result so the caller can sum and
    record the bytes against the user's quota post-flight.
    """
    quota = _build_quota_tracker()
    try:
        quota.check_daily_budget(user_id)
    except QuotaExceededError as exc:
        raise HTTPException(status_code=429, detail={
            "reason": "daily_byte_cap_exceeded",
            "kind": exc.kind,
            "current": exc.current,
            "limit": exc.limit,
            "retry_after_seconds": exc.retry_after_seconds,
        })

    try:
        bq = get_bq_access()
    except BqAccessError as exc:
        raise HTTPException(status_code=502, detail={
            "kind": exc.kind,
            "message": exc.message,
            **(exc.details or {}),
        })

    cap_bytes = _default_remote_query_cap_bytes()

    with quota.acquire(user_id):
        total_bytes = 0
        for i, (bucket, source_table, _) in enumerate(dry_run_set):
            bq_sql = f"SELECT * FROM `{bq.projects.data}.{bucket}.{source_table}`"
            try:
                est = _bq_dry_run_bytes(bq, bq_sql)
            except BqAccessError as exc:
                raise HTTPException(status_code=502, detail={
                    "kind": exc.kind,
                    "message": exc.message,
                    **(exc.details or {}),
                })
            dry_run_set[i] = (bucket, source_table, est)
            total_bytes += est

        if cap_bytes > 0 and total_bytes > cap_bytes:
            tables = [f"{b}.{t}" for b, t, _ in dry_run_set]
            raise HTTPException(status_code=400, detail={
                "reason": "remote_scan_too_large",
                "scan_bytes": total_bytes,
                "limit_bytes": cap_bytes,
                "tables": tables,
                "suggestion": (
                    "Use `da fetch <id> --select <cols> --where <predicate> "
                    "--estimate` to materialize a filtered subset, then query "
                    "the snapshot locally."
                ),
            })
