"""BigQuery extractor — produces extract.duckdb with remote views via DuckDB BigQuery extension.

No data is downloaded. All queries go directly to BigQuery via DuckDB extension ATTACH.
"""

import logging
import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional

import duckdb

# Serializes the body of `init_extract` across threads so two concurrent
# materialize calls (e.g. the synchronous timeout-fallback BackgroundTask
# kicking in while the original daemon thread is still running) can't both
# reach the `shutil.move(tmp, db_path)` swap and corrupt the extract file.
# `SyncOrchestrator._rebuild_lock` only protects the master-view rebuild,
# not the per-source extract-file write, so we need a dedicated lock here.
_INIT_EXTRACT_LOCK = threading.Lock()

from connectors.bigquery.auth import get_metadata_token, BQMetadataAuthError
from app.instance_config import get_value
from src.sql_safe import (
    validate_identifier as _validate_identifier,
    validate_project_id as _validate_project_id,
)
from src.identifier_validation import validate_identifier, validate_quoted_identifier

logger = logging.getLogger(__name__)


def _detect_table_type(
    conn: duckdb.DuckDBPyConnection,
    project: str,
    dataset: str,
    table: str,
) -> str | None:
    """Return BQ entity type for `project.dataset.table`.

    Uses `bigquery_query()` table function which routes through the BQ jobs
    API — works on tables, views, and materialized views alike. Returns the
    value of INFORMATION_SCHEMA.TABLES.table_type ('BASE TABLE', 'VIEW',
    'MATERIALIZED_VIEW') or None if not found.
    """
    bq_sql = (
        f"SELECT table_type FROM `{project}.{dataset}.INFORMATION_SCHEMA.TABLES` "
        f"WHERE table_name = ? LIMIT 1"
    )
    # Parameter-bind project (1st arg of bigquery_query), the inner BQ SQL
    # (2nd arg), and the table-name predicate. This avoids the nested-quote
    # bug where inline `'{table}'` would close the outer `bigquery_query('...')`
    # string. Note: bigquery_query forwards extra positional args as BQ query
    # parameters, bound positionally to the `?` placeholders inside `bq_sql`.
    duck_sql = "SELECT * FROM bigquery_query(?, ?, ?)"
    row = conn.execute(duck_sql, [project, bq_sql, table]).fetchone()
    return row[0] if row else None


def _create_meta_table(conn: duckdb.DuckDBPyConnection) -> None:
    """Create the _meta table required by the extract.duckdb contract."""
    conn.execute("DROP TABLE IF EXISTS _meta")
    conn.execute("""CREATE TABLE _meta (
        table_name VARCHAR NOT NULL,
        description VARCHAR,
        rows BIGINT,
        size_bytes BIGINT,
        extracted_at TIMESTAMP,
        query_mode VARCHAR DEFAULT 'remote'
    )""")


def _create_remote_attach_table(
    conn: duckdb.DuckDBPyConnection, project_id: str
) -> None:
    """Write _remote_attach. token_env is empty for BQ — orchestrator
    detects extension='bigquery' and refreshes the token from GCE metadata
    on its own."""
    conn.execute("DROP TABLE IF EXISTS _remote_attach")
    conn.execute("""CREATE TABLE _remote_attach (
        alias VARCHAR,
        extension VARCHAR,
        url VARCHAR,
        token_env VARCHAR
    )""")
    conn.execute(
        "INSERT INTO _remote_attach VALUES (?, ?, ?, ?)",
        ["bq", "bigquery", f"project={project_id}", ""],
    )


def init_extract(
    output_dir: str,
    project_id: str,
    table_configs: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Create extract.duckdb with remote views into BigQuery.

    Authenticates via the GCE metadata server. For each registered table,
    detects whether the BQ entity is a BASE TABLE or VIEW, and emits a
    DuckDB view that uses the appropriate path:
      - BASE TABLE → direct ATTACH ref (Storage Read API, fast for full scans)
      - VIEW       → bigquery_query() table function (jobs API, supports views)

    Args:
        output_dir: Path to write extract.duckdb
        project_id: GCP project ID for billing/job execution
        table_configs: List of table config dicts from table_registry

    Returns:
        Dict with stats: {tables_registered: int, errors: list}
    """
    # Serialize concurrent calls to avoid a torn `shutil.move` swap when the
    # admin route's timeout-fallback BackgroundTask runs alongside the still-
    # alive daemon thread that exceeded the 5s budget.
    with _INIT_EXTRACT_LOCK:
        return _init_extract_locked(output_dir, project_id, table_configs)


def _init_extract_locked(
    output_dir: str,
    project_id: str,
    table_configs: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Inner body of init_extract executed under _INIT_EXTRACT_LOCK."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    db_path = output_path / "extract.duckdb"
    tmp_db_path = output_path / "extract.duckdb.tmp"
    if tmp_db_path.exists():
        tmp_db_path.unlink()

    stats: Dict[str, Any] = {"tables_registered": 0, "errors": []}
    now = datetime.now(timezone.utc)

    # Validate project_id before any work — no point opening DB or fetching a
    # token if the value is structurally bogus and would only break SQL later.
    if not _validate_project_id(project_id):
        msg = f"unsafe BQ project_id: {project_id!r}"
        logger.error(msg)
        stats["errors"].append({"table": "<config>", "error": msg})
        return stats

    # Fetch token before opening DB so failure aborts cleanly without partial file
    try:
        token = get_metadata_token()
    except BQMetadataAuthError as e:
        logger.error("BQ metadata auth failed: %s", e)
        stats["errors"].append({"table": "<auth>", "error": str(e)})
        return stats

    conn = duckdb.connect(str(tmp_db_path))
    try:
        # Install and load BigQuery extension
        try:
            conn.execute("INSTALL bigquery FROM community; LOAD bigquery;")
            # session-scoped DuckDB secret with the metadata token
            escaped_token = token.replace("'", "''")
            conn.execute(
                f"CREATE SECRET bq_session (TYPE bigquery, ACCESS_TOKEN '{escaped_token}')"
            )
            conn.execute(
                f"ATTACH 'project={project_id}' AS bq (TYPE bigquery, READ_ONLY)"
            )
            logger.info("Attached BigQuery project: %s", project_id)
        except Exception as attach_err:
            logger.error("Failed to attach BigQuery project %s: %s", project_id, attach_err)
            stats["errors"].append(
                {"table": "*", "error": f"BigQuery ATTACH failed: {attach_err}"}
            )
            # No tables can be registered without a working connection
            for tc in table_configs:
                stats["errors"].append(
                    {"table": tc["name"], "error": "skipped: BigQuery ATTACH failed"}
                )
            return stats

        _create_meta_table(conn)
        _create_remote_attach_table(conn, project_id)

        for tc in table_configs:
            # Materialized rows are written by the sync trigger pass via
            # `materialize_query()` — they live as parquets in
            # /data/extracts/bigquery/data/, picked up by the orchestrator's
            # standard local-parquet discovery. Don't create a remote view
            # here (would shadow the parquet via name collision).
            if tc.get("query_mode") == "materialized":
                continue

            table_name = tc["name"]
            dataset = tc.get("bucket", "")
            source_table = tc.get("source_table", table_name)

            # #81 Group D — refuse rows with unsafe identifiers. Same
            # rationale as the keboola extractor: registry is admin-controlled
            # but anyone with write access can otherwise inject SQL via the
            # CREATE VIEW interpolation below. Skip-and-continue.
            # `table_name` is the DuckDB view name in the master
            # analytics DB and the orchestrator uses the STRICT
            # validator there — accept the same constraint upstream
            # so a name with `-` or `.` fails fast in extraction
            # rather than getting silently dropped at rebuild time.
            # `dataset` and `source_table` are upstream-typed (BQ
            # naming) so use the relaxed validator for those.
            if not validate_identifier(table_name, "BigQuery table_name"):
                stats["errors"].append({"table": table_name, "error": f"unsafe table_name: {table_name!r}"})
                continue
            if not validate_quoted_identifier(dataset, "BigQuery dataset"):
                stats["errors"].append({"table": table_name, "error": f"unsafe dataset: {dataset!r}"})
                continue
            if not validate_quoted_identifier(source_table, "BigQuery source_table"):
                stats["errors"].append({"table": table_name, "error": f"unsafe source_table: {source_table!r}"})
                continue

            try:
                entity_type = _detect_table_type(conn, project_id, dataset, source_table)
                if entity_type is None:
                    raise RuntimeError(
                        f"BQ entity {project_id}.{dataset}.{source_table} not found"
                    )

                legacy_wrap_views = bool(
                    get_value("data_source", "bigquery", "legacy_wrap_views", default=False)
                )

                if entity_type == "BASE TABLE":
                    # Storage Read API — fast for full scans
                    view_sql = (
                        f'CREATE OR REPLACE VIEW "{table_name}" AS '
                        f'SELECT * FROM bq."{dataset}"."{source_table}"'
                    )
                    conn.execute(view_sql)
                elif legacy_wrap_views:
                    # Backwards compatibility — for one release cycle only.
                    if entity_type not in ("VIEW", "MATERIALIZED_VIEW"):
                        logger.warning(
                            "Unknown BQ entity type %r for %s.%s.%s — using bigquery_query() path",
                            entity_type, project_id, dataset, source_table,
                        )
                    # VIEW or MATERIALIZED_VIEW — use jobs API
                    bq_inner = f"SELECT * FROM `{project_id}.{dataset}.{source_table}`"
                    bq_inner_escaped = bq_inner.replace("'", "''")
                    view_sql = (
                        f'CREATE OR REPLACE VIEW "{table_name}" AS '
                        f"SELECT * FROM bigquery_query('{project_id}', '{bq_inner_escaped}')"
                    )
                    conn.execute(view_sql)
                else:
                    # Default: VIEW / MATERIALIZED_VIEW are recorded in _meta but no master
                    # view created. Analyst must use `da fetch` (v2 primitives) to materialise
                    # a snapshot locally.
                    logger.info(
                        "Skipping wrap view for %s entity %s.%s.%s — use `da fetch`",
                        entity_type, project_id, dataset, source_table,
                    )

                conn.execute(
                    "INSERT INTO _meta VALUES (?, ?, 0, 0, ?, 'remote')",
                    [table_name, tc.get("description", ""), now],
                )
                stats["tables_registered"] += 1
                logger.info(
                    "Registered remote view: %s -> %s.%s.%s (%s)",
                    table_name, project_id, dataset, source_table, entity_type,
                )
            except Exception as e:
                logger.error("Failed to register %s: %s", table_name, e)
                stats["errors"].append({"table": table_name, "error": str(e)})

        conn.execute("DETACH bq")
    finally:
        conn.close()

    # Atomic swap (preserve from existing implementation)
    old_wal = Path(str(db_path) + ".wal")
    if old_wal.exists():
        old_wal.unlink()
    if tmp_db_path.exists():
        shutil.move(str(tmp_db_path), str(db_path))
    tmp_wal = Path(str(tmp_db_path) + ".wal")
    if tmp_wal.exists():
        tmp_wal.unlink()

    return stats


class MaterializeBudgetError(RuntimeError):
    """Raised when a `materialize_query` BQ dry-run estimate exceeds the
    configured `data_source.bigquery.max_bytes_per_materialize` cap.

    The materialize trigger pass logs this and skips the row; the next
    scheduled tick re-tries (in case the underlying table size dropped
    or the operator raised the cap). Shape mirrors `BqAccessError` —
    `current` and `limit` for operator triage.
    """

    def __init__(self, message: str, *, table_id: str, current: int, limit: int):
        self.table_id = table_id
        self.current = current
        self.limit = limit
        super().__init__(message)


def materialize_query(
    table_id: str,
    sql: str,
    *,
    bq,  # connectors.bigquery.access.BqAccess (untyped here to avoid circular import at type-check)
    output_dir: str,
    max_bytes: Optional[int] = None,
) -> Dict[str, Any]:
    """Run `sql` through the DuckDB BigQuery extension and write the result
    to `<output_dir>/data/<table_id>.parquet` atomically.

    Designed for `query_mode='materialized'` table_registry rows. The SQL
    is admin-registered (validated upstream) and may reference DuckDB
    three-part identifiers (`bq."dataset"."table"`) resolved by the
    in-session ATTACH, OR native BQ identifiers via the `bigquery_query()`
    table function — both work because the session has the bigquery
    extension loaded with a SECRET token.

    Cost guardrail: when `max_bytes` is a positive int, run a BQ dry-run
    via `bq.client()` first; raise `MaterializeBudgetError` if the
    estimate exceeds the cap. `max_bytes=None` or `max_bytes <= 0`
    disables the guardrail (config sentinel, see
    `data_source.bigquery.max_bytes_per_materialize`).

    Dry-run is best-effort and fail-open: if the SQL uses DuckDB syntax
    that the native BQ client can't parse (e.g. `bq."ds"."t"`), the
    dry-run raises and we log a warning; the COPY still runs. This
    matches the BqAccess facade's "client is for native BQ SQL only"
    contract — operators who need the cap to engage write the registered
    SQL using native BQ identifiers (`\\`project.ds.t\\``).

    Atomic write: result lands in `<id>.parquet.tmp` first, then
    `os.replace` swaps it in. A failed COPY leaves no partial file behind.

    Args:
        table_id: Logical id from table_registry; becomes the parquet
            filename. Must pass `validate_identifier()` so it can't
            inject path traversal.
        sql: SELECT statement, no trailing semicolon.
        bq: A `BqAccess` instance — provides `duckdb_session()` for the
            COPY and `client()` for the dry-run.
        output_dir: Connector root, e.g. `/data/extracts/bigquery`.
            Parquet lands in `<output_dir>/data/<table_id>.parquet`.
        max_bytes: Optional cap on BQ bytes scanned. None or <= 0 disables.

    Returns:
        {"rows": int, "size_bytes": int, "query_mode": "materialized"}

    Raises:
        ValueError: if `table_id` is unsafe.
        MaterializeBudgetError: if `max_bytes > 0` and dry-run estimate exceeds it.
        BqAccessError: from `bq.duckdb_session()` (auth_failed / bq_lib_missing /
            not_configured) — caller catches and aggregates into the trigger
            pass summary.
        duckdb.Error: if the COPY itself fails (e.g. bad SQL, missing table).
    """
    if not validate_identifier(table_id, "materialize table_id"):
        raise ValueError(f"unsafe table_id: {table_id!r}")

    out_path = Path(output_dir)
    data_dir = out_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    parquet_path = data_dir / f"{table_id}.parquet"
    tmp_path = data_dir / f"{table_id}.parquet.tmp"
    if tmp_path.exists():
        tmp_path.unlink()

    # Cost guardrail (best-effort — fail-open if dry-run can't parse the SQL).
    if max_bytes is not None and max_bytes > 0:
        try:
            from app.api.v2_scan import _bq_dry_run_bytes  # reuse main's impl
            estimated = _bq_dry_run_bytes(bq, sql)
        except Exception as e:
            logger.warning(
                "BQ dry-run failed for materialize cost guardrail (fail-open): %s. "
                "If the SQL uses DuckDB three-part names like bq.\"ds\".\"t\", "
                "rewrite to native BQ identifiers (`project.ds.t`) for the "
                "guardrail to engage. Proceeding with COPY.",
                e,
            )
            estimated = 0
        if estimated > max_bytes:
            raise MaterializeBudgetError(
                f"dry-run estimate {estimated:,} bytes exceeds cap "
                f"{max_bytes:,} for table {table_id!r}",
                table_id=table_id,
                current=estimated,
                limit=max_bytes,
            )

    # COPY through a BqAccess-managed session.
    with bq.duckdb_session() as conn:
        # ATTACH the data project. Test stubs pre-populate `bq` as an
        # in-memory schema; production uses the real BQ extension. The
        # only tolerated failure is "alias already in use" — anything else
        # (auth, permission, malformed project_id) must surface so the
        # caller's per-row try/except can record it. Devil's-advocate
        # review found that swallowing the error blindly hid
        # cross-project permission errors behind a confusing
        # "bq is not attached" downstream message.
        try:
            conn.execute(
                f"ATTACH 'project={bq.projects.data}' AS bq (TYPE bigquery, READ_ONLY)"
            )
        except duckdb.Error as e:
            msg = str(e).lower()
            if "already" not in msg and "in use" not in msg:
                raise

        try:
            safe_path = str(tmp_path).replace("'", "''")
            conn.execute(f"COPY ({sql}) TO '{safe_path}' (FORMAT PARQUET)")
            rows = conn.execute(
                f"SELECT count(*) FROM read_parquet('{safe_path}')"
            ).fetchone()[0]
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink()
            raise

    # Compute the parquet hash inline before the atomic swap. The caller used
    # to re-read the file in `_run_materialized_pass` to hash it via
    # `_file_hash`, but that's a synchronous full-read on the FastAPI worker
    # thread — a 10 GiB parquet means 50+ seconds of disk I/O blocking other
    # requests. Hashing here keeps the open-file handle hot from the COPY
    # round and removes the second read. Devil's-advocate review item.
    import hashlib
    h = hashlib.md5()
    with open(tmp_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    parquet_hash = h.hexdigest()

    size_bytes = tmp_path.stat().st_size
    os.replace(tmp_path, parquet_path)

    rows = int(rows)
    if rows == 0:
        # 0 rows is indistinguishable from "the SQL is wrong and nobody
        # noticed" — surface it loudly so operators see it in the scheduler
        # log line and the per-row error aggregation. Caller decides whether
        # to alert.
        logger.warning(
            "Materialized %s produced 0 rows — verify the SQL filter is "
            "intentional. Parquet written: %s",
            table_id, parquet_path,
        )

    return {
        "rows": rows,
        "size_bytes": size_bytes,
        "query_mode": "materialized",
        "hash": parquet_hash,
    }


def _resolve_bq_project_id() -> str:
    """Resolve ``data_source.bigquery.project`` honoring the overlay.

    Tries ``app.instance_config.get_value`` first (deep-merge of the static
    ``CONFIG_DIR/instance.yaml`` and the writable
    ``DATA_DIR/state/instance.yaml``); the writable overlay is what the
    admin UI / API writes to. Falls back to a direct read of the static
    config so the standalone ``__main__`` entry point still works in
    environments where the FastAPI app isn't importable (e.g. a one-shot
    scheduler container that only ships connector code).
    """
    try:
        from app.instance_config import get_value as _get_value  # noqa: PLC0415
        project_id = _get_value("data_source", "bigquery", "project", default="") or ""
        if project_id:
            return project_id
    except Exception:
        # The fallback below covers this path — keep going.
        pass
    try:
        from config.loader import load_instance_config as _load  # noqa: PLC0415
        cfg = _load() or {}
        return ((cfg.get("data_source") or {}).get("bigquery") or {}).get("project", "") or ""
    except Exception:
        return ""


def rebuild_from_registry(
    conn: duckdb.DuckDBPyConnection | None = None,
    output_dir: str | None = None,
) -> Dict[str, Any]:
    """Re-materialize the BigQuery extract.duckdb from the current registry.

    Reads ``data_source.bigquery.project`` from ``instance.yaml`` and the
    BigQuery rows from ``table_registry``, then calls ``init_extract`` to
    write ``extract.duckdb`` containing one DuckDB view per registered BQ
    table. Used by the admin API immediately after a register / update /
    unregister of a BigQuery row so the master view appears (or disappears)
    in seconds without waiting for the next scheduled sync.

    Args:
        conn: System DuckDB connection (already open). If None, a new one
            is opened and closed inside this call — convenient for the
            standalone __main__ entrypoint, but the API path always passes
            its request-scoped connection so we don't open a second handle
            on the same file.
        output_dir: Override for the extract directory. Defaults to
            ``${DATA_DIR}/extracts/bigquery`` to match the orchestrator's
            scan path.

    Returns:
        Dict with ``project_id``, ``tables_registered``, ``errors``, and
        ``skipped`` (set to True when there are no BQ rows in the registry,
        in which case the extract is left untouched).

    Project resolution: reads ``data_source.bigquery.project`` via
    ``app.instance_config.get_value`` so the writable overlay
    (``DATA_DIR/state/instance.yaml``, populated by ``POST /api/admin/
    configure`` and ``/server-config``) is honored. Pre-2026-04-28 this
    used ``config.loader.load_instance_config`` directly, which only sees
    the static ``CONFIG_DIR/instance.yaml`` — operators who configured BQ
    through the admin UI got a silent rebuild failure ("project missing")
    while validation passed (the validator already used the merged view).
    See review BLOCKER 2 in PR #119.
    """
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository

    project_id = _resolve_bq_project_id()

    if not project_id:
        msg = "data_source.bigquery.project missing from instance.yaml"
        logger.error(msg)
        return {
            "project_id": "",
            "tables_registered": 0,
            "errors": [{"table": "<config>", "error": msg}],
            "skipped": False,
        }

    owns_conn = conn is None
    sys_conn = conn if conn is not None else get_system_db()
    try:
        repo = TableRegistryRepository(sys_conn)
        tables = repo.list_by_source("bigquery")
    finally:
        if owns_conn:
            try:
                sys_conn.close()
            except Exception:
                pass

    if not tables:
        logger.warning("No BigQuery tables registered in table_registry")
        return {
            "project_id": project_id,
            "tables_registered": 0,
            "errors": [],
            "skipped": True,
        }

    if output_dir is None:
        data_dir = Path(os.environ.get("DATA_DIR", "./data"))
        output_dir = str(data_dir / "extracts" / "bigquery")

    # Resolve init_extract via this module so tests that monkey-patch it
    # (e.g. tests/test_admin_bq_register.py) see the patched callable.
    import connectors.bigquery.extractor as _self
    result = _self.init_extract(output_dir, project_id, tables)
    out = dict(result)
    out["project_id"] = project_id
    out["skipped"] = False
    return out


if __name__ == "__main__":
    """Standalone: reads config from instance.yaml + table_registry, creates extract."""
    result = rebuild_from_registry()
    if result.get("skipped"):
        # No BQ rows registered — nothing to do, exit cleanly.
        raise SystemExit(0)
    if not result.get("project_id"):
        # Missing project → already logged inside rebuild_from_registry.
        raise SystemExit(2)
    logger.info("BigQuery extract init complete: %s", result)
