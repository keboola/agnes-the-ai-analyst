"""Keboola extractor — produces extract.duckdb + data/*.parquet using DuckDB Keboola extension."""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional

import duckdb

from src.duckdb_conn import _open_duckdb
from src.identifier_validation import (
    is_safe_quoted_identifier,
    validate_identifier,
    validate_quoted_identifier,
)

logger = logging.getLogger(__name__)


# Cap DuckDB memory_limit + thread count on short-lived consolidation
# connections inside ``materialize_query``. DuckDB's default
# ``memory_limit`` is 80% of system RAM, which on a 4 GiB cgroup
# container resolves to ~3.2 GiB of process-resident buffer pool. With
# Python objects + a few hundred MiB for orchestrator state + caddy /
# scheduler sidecars, that exceeds the cgroup cap and triggers OOM-kill
# during slice consolidation against any non-trivial table (observed
# on a 4 GiB dev container against a multi-GiB Keboola table: anon RSS
# climbed from ~350 MiB to ~3.5 GiB in minutes, then SIGKILL).
#
# 2 GiB strikes the balance: the parquet path's streaming row-group
# COPY rarely needs more than ~100 MiB, but the legacy CSV path's
# ``read_csv(max_line_size=64MB)`` pre-allocates a multi-thread
# sliding window buffer that DuckDB internally treats as a single
# ~1 GiB allocation unit (verified empirically — a 1 GiB cap raised
# `OutOfMemoryException` on a 2-row CSV fixture). Combined with
# ``preserve_insertion_order=false`` the peak stays well under 2 GiB.
# On a 4 GiB cgroup container that leaves ~1.5 GiB for Python objects
# + sidecars; on the 8 GiB target sizing the headroom is generous.
_CONSOLIDATION_MEMORY_LIMIT = "2GB"
_CONSOLIDATION_THREADS = 2


def _open_consolidation_conn(db_path: Optional[str] = None):
    """Return a DuckDB connection with memory/thread caps applied.

    Use for short-lived ``COPY (SELECT … FROM read_parquet/read_csv)``
    consolidation queries inside the materialize path. ``db_path``
    defaults to in-memory; pass a path for on-disk consolidation.

    ``preserve_insertion_order=false`` is set alongside the memory cap
    on DuckDB's recommendation (the OOM error message itself proposes
    it) — for the CSV→parquet path, the default
    insertion-order-preserving execution holds row batches in memory
    until the parent operator can emit them in order, which collides
    badly with a 2 GiB cap when ``read_csv(max_line_size=64MB)``
    pre-allocates large sliding-window buffers. The materialize output
    is a single parquet that downstream consumers re-sort however they
    like — preserving the read-order from the input file isn't
    something any caller depends on.
    """
    conn = _open_duckdb(db_path) if db_path else _open_duckdb(":memory:")
    conn.execute(f"SET memory_limit='{_CONSOLIDATION_MEMORY_LIMIT}'")
    conn.execute(f"SET threads={_CONSOLIDATION_THREADS}")
    conn.execute("SET preserve_insertion_order=false")
    return conn


def materialize_query(
    table_id: str,
    *,
    bucket: str,
    source_table: str,
    source_query: Optional[str] = None,
    storage_client=None,  # KeboolaStorageClient (avoid circular import)
    keboola_url: Optional[str] = None,
    keboola_token: Optional[str] = None,
    output_dir: Path,
) -> dict:
    """Materialize a Keboola Storage table to a local parquet via Storage API.

    Replaces the previous DuckDB-extension path. The extension's QueryService
    scan is unreliable on linked-bucket projects (keboola/duckdb-extension#17;
    fix shipped upstream as v0.1.6 but not yet in the community CDN, and on
    flag-restricted projects the pre-fix workspace role wouldn't have GRANTs
    on the bucket schema anyway). The Storage API export-async path always
    works regardless of project flags.

    Parallel of `connectors/bigquery/extractor.py:materialize_query` in
    surface — same return shape, same atomic write, same MD5 contract — but
    the inputs differ because Keboola's structured filter spec replaces
    BQ's free-form SQL.

    Args:
        table_id: parquet filename + sync_state key (must be a safe ident).
        bucket: Keboola bucket id, e.g. ``in.c-crm``.
        source_table: table id within the bucket, e.g. ``orders``.
        source_query: optional JSON string with a Storage API filter spec
            (see `storage_api.ExportFilter`). Empty / NULL = full table.
        storage_client: pre-built `KeboolaStorageClient` (preferred — lets
            sync.py share one across rows). When omitted, ``keboola_url``
            and ``keboola_token`` are used to construct a one-shot client.
        keboola_url, keboola_token: alternative to ``storage_client`` for
            single-call usage (tests, ad-hoc).
        output_dir: directory to write `<table_id>.parquet`.

    Returns:
        ``{"table_id", "path", "rows", "bytes", "md5"}`` — same shape the
        BQ branch returns, so ``app/api/sync.py:_run_materialized_pass``
        downstream code stays uniform.
    """
    import re
    import hashlib
    import json

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_id):
        raise ValueError(f"unsafe table_id for materialize: {table_id!r}")

    # Lazy import to avoid pulling `requests` at module import time when only
    # the sync trigger imports `extractor` for `run()`.
    from connectors.keboola.storage_api import (
        FILE_TYPE_PARQUET,
        ExportFilter,
        KeboolaStorageClient,
    )

    if storage_client is None:
        if not (keboola_url and keboola_token):
            raise ValueError("materialize_query requires either storage_client or (keboola_url + keboola_token)")
        storage_client = KeboolaStorageClient(url=keboola_url, token=keboola_token)

    # Filter spec is optional. Admin can register a row with no
    # source_query at all (= full-table export), or with a JSON object
    # describing whereFilters / columns / changedSince / file_type.
    payload: dict = {}
    if source_query:
        _sq = source_query.strip()
        if _sq.upper().startswith(("SELECT", "WITH", "INSERT", "UPDATE", "DELETE")):
            raise ValueError(
                f"source_query for {table_id} contains SQL, but Keboola "
                f"materialized tables use a JSON filter spec (or null for "
                f"full-table export). Set query_mode='local' for DuckDB-SQL "
                f"Keboola pulls, or clear source_query for a full-table export."
            )
        try:
            payload = json.loads(_sq)
        except json.JSONDecodeError as e:
            raise ValueError(f"source_query for {table_id} is not valid JSON: {e}") from e
    export_filter = ExportFilter.from_dict(payload)

    # Resolve date placeholders ({{last_6_months}}, {{today}}, …) in the
    # where_filters values, mirroring the local/legacy extract path
    # (`run()` calls `resolve_placeholders(parse_filters(...))`). The Storage
    # API receives literal dates; an unresolved `{{last_6_months}}` would be
    # compared verbatim and silently return 0 rows. Materialized rows
    # previously skipped this step, so placeholders only worked on
    # `query_mode='local'` rows — this aligns the materialized path.
    if export_filter.where_filters:
        from connectors.keboola.where_filters import (
            InvalidFilterError,
            parse_filters,
            resolve_placeholders,
        )

        try:
            export_filter.where_filters = resolve_placeholders(
                parse_filters(export_filter.where_filters),
                datetime.now(timezone.utc),
            )
        except InvalidFilterError as e:
            raise ValueError(f"source_query for {table_id} has an invalid where_filters placeholder: {e}") from e

    # Default the materialized path to parquet — Storage API serves it
    # via native Snowflake UNLOAD, the extractor renames it into place,
    # no CSV intermediate, no DuckDB COPY, no peak-memory load. Admin
    # can pin `{"file_type":"csv"}` in source_query to fall back (legacy
    # debugging, or projects whose backend can't UNLOAD parquet — none
    # known today, but the escape hatch costs nothing). Only override
    # when the admin spec didn't *explicitly* set a file_type.
    if "file_type" not in payload and "fileType" not in payload:
        export_filter.file_type = FILE_TYPE_PARQUET

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = output_dir / f"{table_id}.parquet"
    tmp_parquet = output_dir / f"{table_id}.parquet.tmp"
    if tmp_parquet.exists():
        tmp_parquet.unlink()

    # Per-call temp dir for the intermediate file (CSV or parquet) —
    # separates concurrent exports cleanly without the os.chdir() race
    # the kbcstorage SDK has. ``ignore_cleanup_errors=True`` keeps
    # disk-full / permission errors from masking the original
    # exception, and prevents a half-cleaned dir from sitting around
    # forever (a 12 GiB stale slice tree was seen after a worker died
    # mid-write on a saturated boot disk). ``dir=get_temp_root()``
    # routes to ``AGNES_TEMP_DIR`` when the operator has steered
    # tempfiles off the overlayfs (e.g. onto the data disk) — see
    # storage_api.get_temp_root for the rationale.
    import tempfile
    from connectors.keboola.storage_api import get_temp_root

    with tempfile.TemporaryDirectory(
        prefix=f"kbc-export-{table_id}-",
        dir=get_temp_root(),
        ignore_cleanup_errors=True,
    ) as tmpdir:
        full_table_id = f"{bucket}.{source_table}"

        if export_filter.file_type == FILE_TYPE_PARQUET:
            # Native parquet path. Storage API serves Snowflake UNLOAD
            # output directly. Two shapes to handle:
            #
            # 1. **Single file** (small exports): file_info.url points at
            #    one signed URL; download to tmp_parquet and we're done.
            # 2. **Sliced** (large exports — Snowflake UNLOAD respects
            #    MAX_FILE_SIZE, default 16 MiB, so anything past that
            #    arrives as a manifest of N parquet slices). Each slice
            #    is itself a complete parquet file with its own footer;
            #    naively concatenating them like CSV would be invalid.
            #    We download all slices into the per-call tempdir, then
            #    DuckDB-COPY across `read_parquet([slice1, slice2, ...])`
            #    into one consolidated tmp_parquet. DuckDB streams row
            #    groups during this consolidation — peak memory is one
            #    row group (~1 MiB), not the full table.
            stats = storage_client.prepare_export(
                full_table_id,
                export_filter=export_filter,
            )
            file_info = stats["file_info"]
            if file_info.get("isSliced"):
                slice_dir = Path(tmpdir) / "slices"
                slice_paths = storage_client.download_file_slices(file_info, slice_dir)
                if not slice_paths:
                    raise RuntimeError(f"sliced parquet export for {full_table_id} yielded no slices")
                quoted = ", ".join("'" + str(p).replace("'", "''") + "'" for p in slice_paths)
                safe_tmp = str(tmp_parquet).replace("'", "''")
                conv = _open_consolidation_conn()
                try:
                    conv.execute(f"COPY (SELECT * FROM read_parquet([{quoted}])) TO '{safe_tmp}' (FORMAT PARQUET)")
                finally:
                    conv.close()
            else:
                storage_client.download_file(file_info, tmp_parquet)
                stats["bytes"] = tmp_parquet.stat().st_size if tmp_parquet.exists() else 0

            if not tmp_parquet.exists() or tmp_parquet.stat().st_size == 0:
                logger.warning(
                    "Storage API parquet export for %s returned no data (filter may be too restrictive)",
                    full_table_id,
                )
                # Empty placeholder parquet so the orchestrator doesn't
                # choke on a missing file.
                _open_consolidation_conn().execute(
                    f"COPY (SELECT 1 AS _empty WHERE FALSE) TO '{tmp_parquet}' (FORMAT PARQUET)"
                ).close()
        else:
            # Legacy CSV path. Kept for the explicit `{"file_type":"csv"}`
            # opt-in. Slower (CSV parse + parquet rewrite) and
            # memory-heavier (DuckDB pulls the CSV into a buffer with
            # max_line_size headroom), but doesn't depend on Storage
            # API parquet support if a future project backend lacks it.
            csv_path = Path(tmpdir) / f"{table_id}.csv"
            stats = storage_client.export_table(
                full_table_id,
                csv_path,
                export_filter=export_filter,
            )
            if not csv_path.exists() or csv_path.stat().st_size == 0:
                logger.warning(
                    "Storage API CSV export for %s returned no data (filter may be too restrictive)",
                    full_table_id,
                )
                _open_consolidation_conn().execute(
                    f"COPY (SELECT 1 AS _empty WHERE FALSE) TO '{tmp_parquet}' (FORMAT PARQUET)"
                ).close()
            else:
                # CSV → parquet via DuckDB. `all_varchar=True` matches the
                # legacy client's behavior — preserves the source's exact
                # character data without DuckDB's type inference rewriting
                # numeric-looking strings (e.g. "Non-Manager") as NULL.
                #
                # `max_line_size=64MB` overrides DuckDB's default 2 MB cap
                # on any single CSV line. Keboola tables that store
                # embedded JSON / SQL transformation bodies routinely
                # have multi-MB cells (e.g. `kbc_component_configuration`
                # rows ship full Snowflake transformation SQL inline as
                # a JSON column value); the default 2 MB ceiling rejects
                # them with `Maximum line size of 2000000 bytes
                # exceeded`. 64 MB is generous enough to absorb any
                # reasonable embedded blob; DuckDB allocates a single
                # buffer of this size per worker thread.
                safe_csv = str(csv_path).replace("'", "''")
                safe_tmp = str(tmp_parquet).replace("'", "''")
                try:
                    conv = _open_consolidation_conn()
                    conv.execute(
                        f"COPY (SELECT * FROM read_csv('{safe_csv}', "
                        f"all_varchar=true, max_line_size=67108864)) "
                        f"TO '{safe_tmp}' (FORMAT PARQUET)"
                    )
                    conv.close()
                except Exception:
                    if tmp_parquet.exists():
                        tmp_parquet.unlink()
                    raise

    # Row count from the parquet, not from `stats["rows"]` — Storage API
    # sometimes omits totalRowsCount on small results, and the parquet is
    # the authoritative count we'll be serving downstream anyway.
    safe_tmp = str(tmp_parquet).replace("'", "''")
    cnt_conn = _open_consolidation_conn()
    try:
        row_count = cnt_conn.execute(f"SELECT COUNT(*) FROM read_parquet('{safe_tmp}')").fetchone()[0]
    finally:
        cnt_conn.close()

    # Streaming MD5 — bounded memory regardless of parquet size.
    h = hashlib.md5()
    with open(tmp_parquet, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    md5 = h.hexdigest()
    size = tmp_parquet.stat().st_size

    os.replace(tmp_parquet, parquet_path)

    if row_count == 0:
        logger.warning(
            "Materialized Keboola export for %s wrote 0 rows — verify the filter and that the source bucket has data.",
            table_id,
        )

    return {
        "table_id": table_id,
        "path": str(parquet_path),
        "rows": row_count,
        "bytes": size,
        "md5": md5,
    }


def _read_last_sync_for_tc(tc: Dict[str, Any]):
    """Resolve last_sync for an incremental/partitioned table_config.

    Two paths, in order of preference:
    1. `__last_sync__` injected by the parent server before subprocess
       spawn (see app/api/sync.py:_run_sync) — this is the canonical
       path because the subprocess holds no DuckDB lock.
    2. Direct sync_state read — only safe in same-process callers
       (tests, in-process sync). The subprocess path errors out with
       "Conflicting lock is held" because the web server keeps an
       open write handle on system.duckdb.

    Returns None when no prior sync (treat error state as never-synced
    so the next attempt redownloads from max_history_days, not a stale
    watermark from a half-finished run).

    Tests stub this directly (`monkeypatch.setattr(extractor, "_read_last_sync_for_tc", ...)`).
    Pre-v26 fallback name `_read_last_sync` retained for monkeypatching tests.
    """
    injected = tc.get("__last_sync__")
    if injected is not None:
        if isinstance(injected, str):
            from datetime import datetime

            try:
                return datetime.fromisoformat(injected)
            except ValueError:
                return None
        return injected

    # Direct DB read — same-process fallback only.
    try:
        from src.db import get_system_db
        from src.repositories.sync_state import SyncStateRepository

        conn = get_system_db()
        try:
            repo = SyncStateRepository(conn)
            state = repo.get_table_state(tc.get("id") or tc.get("name"))
            if not state or state.get("status") == "error":
                return None
            return state.get("last_sync")
        finally:
            conn.close()
    except Exception as e:
        logger.warning(
            "_read_last_sync_for_tc fallback failed for %s (%s); treating as first sync",
            tc.get("id") or tc.get("name"),
            e,
        )
        return None


# Back-compat alias for tests written against the pre-v26 name.
def _read_last_sync(table_id: str):
    return _read_last_sync_for_tc({"id": table_id})


def _create_meta_table(conn: duckdb.DuckDBPyConnection) -> None:
    """Create the _meta table required by the extract.duckdb contract."""
    conn.execute("DROP TABLE IF EXISTS _meta")
    conn.execute("""CREATE TABLE _meta (
        table_name VARCHAR NOT NULL,
        description VARCHAR,
        rows BIGINT,
        size_bytes BIGINT,
        extracted_at TIMESTAMP,
        query_mode VARCHAR DEFAULT 'local'
    )""")


def _create_remote_attach_table(conn: duckdb.DuckDBPyConnection, keboola_url: str) -> None:
    """Write _remote_attach so orchestrator can re-ATTACH the Keboola extension."""
    conn.execute("DROP TABLE IF EXISTS _remote_attach")
    conn.execute("""CREATE TABLE _remote_attach (
        alias VARCHAR,
        extension VARCHAR,
        url VARCHAR,
        token_env VARCHAR
    )""")
    conn.execute(
        "INSERT INTO _remote_attach VALUES (?, ?, ?, ?)",
        ["kbc", "keboola", keboola_url, "KEBOOLA_STORAGE_TOKEN"],
    )


def _try_attach_extension(conn: duckdb.DuckDBPyConnection, keboola_url: str, keboola_token: str) -> bool:
    """Try to install and attach the Keboola DuckDB extension. Returns True on success."""
    try:
        conn.execute("INSTALL keboola FROM community; LOAD keboola;")
        escaped_token = keboola_token.replace("'", "''")
        # Strip trailing slash — the Keboola DuckDB extension's ATTACH fails
        # with a network error when the URL ends in `/` (e.g. the canonical
        # `https://connection.us-east4.gcp.keboola.com/` form). Bare host
        # works.
        attach_url = keboola_url.rstrip("/")
        conn.execute(f"ATTACH '{attach_url}' AS kbc (TYPE keboola, TOKEN '{escaped_token}')")
        logger.info("Using DuckDB Keboola extension")
        return True
    except Exception as e:
        logger.warning("Keboola extension unavailable (%s), falling back to legacy client", e)
        return False


def run(output_dir: str, table_configs: List[Dict[str, Any]], keboola_url: str, keboola_token: str) -> Dict[str, Any]:
    """Extract tables from Keboola into output_dir using DuckDB extension.

    Args:
        output_dir: Path to write extract.duckdb + data/
        table_configs: List of table config dicts from table_registry
        keboola_url: Keboola stack URL
        keboola_token: Keboola Storage API token

    Returns:
        Dict with extraction stats: {tables_extracted: int, tables_failed: int, errors: list}
    """
    output_path = Path(output_dir)
    data_dir = output_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # Write to temp file then rename — avoids lock conflict with orchestrator
    # which may hold a read lock on the existing extract.duckdb
    db_path = output_path / "extract.duckdb"
    tmp_db_path = output_path / "extract.duckdb.tmp"
    if tmp_db_path.exists():
        tmp_db_path.unlink()
    conn = _open_duckdb(str(tmp_db_path))

    stats = {"tables_extracted": 0, "tables_failed": 0, "errors": []}
    now = datetime.now(timezone.utc)

    # Per-table workitems whose extension scan failed and need the legacy
    # Storage-API fallback. Drained in a parallel pool below the per-table
    # serial loop. Items are `(tc, pq_path)` tuples.
    legacy_queue: List[tuple] = []

    try:
        # Try DuckDB Keboola extension
        use_extension = _try_attach_extension(conn, keboola_url, keboola_token)

        _create_meta_table(conn)

        has_remote = any(tc.get("query_mode") == "remote" for tc in table_configs)
        if has_remote and use_extension:
            _create_remote_attach_table(conn, keboola_url)

        for tc in table_configs:
            table_name = tc["name"]
            query_mode = tc.get("query_mode", "local")

            # Materialized rows are written by the sync trigger pass via
            # `materialize_query()` — they live as parquets in
            # /data/extracts/keboola/data/, picked up by the orchestrator's
            # standard local-parquet discovery. Don't extract here (would
            # double-write data via the source bucket reference and confuse
            # sync_state bookkeeping). Mirror of the BQ extractor's skip at
            # connectors/bigquery/extractor.py:190.
            if query_mode == "materialized":
                logger.info(
                    "Skipping legacy extract for %s — query_mode='materialized', "
                    "handled by _run_materialized_pass instead",
                    tc.get("id") or tc.get("name"),
                )
                continue

            # #81 Group D — refuse rows whose identifiers don't pass the
            # whitelist. The registry is admin-controlled but anyone with
            # write access can otherwise inject SQL via the CREATE VIEW /
            # COPY / SELECT interpolation below. Skip-and-continue rather
            # than crashing the whole extraction; valid rows still process.
            #
            # `table_name` is the DuckDB view name in the master
            # analytics DB. The orchestrator uses the STRICT validator
            # (`^[a-zA-Z_][a-zA-Z0-9_]{0,63}$`) when re-creating views,
            # so any name with `-` or `.` would pass extraction here
            # but be silently dropped at orchestrator-rebuild time.
            # Use the strict validator here too so the failure is
            # caught early and visible in tables_failed.
            if not validate_identifier(table_name, "Keboola table_name"):
                stats["tables_failed"] += 1
                stats["errors"].append({"table": table_name, "error": "unsafe identifier"})
                continue

            if query_mode == "remote":
                # Create view pointing to kbc extension (requires re-ATTACH at query time)
                bucket = tc.get("bucket", "")
                source_table = tc.get("source_table", table_name)
                if not (
                    validate_quoted_identifier(bucket, "Keboola bucket")
                    and validate_quoted_identifier(source_table, "Keboola source_table")
                ):
                    stats["tables_failed"] += 1
                    stats["errors"].append({"table": table_name, "error": "unsafe bucket/source_table"})
                    continue
                if use_extension and bucket:
                    conn.execute(
                        f'CREATE OR REPLACE VIEW "{table_name}" AS SELECT * FROM kbc."{bucket}"."{source_table}"'
                    )
                conn.execute(
                    "INSERT INTO _meta VALUES (?, ?, 0, 0, ?, 'remote')",
                    [table_name, tc.get("description", ""), now],
                )
                stats["tables_extracted"] += 1
                continue

            # v26 dispatcher: route per-table by sync_strategy.
            # API-layer validators reject conflicting combinations
            # (incremental + where_filters, partitioned + remote) before
            # rows reach this point — here we trust tc and dispatch.
            sync_strategy = tc.get("sync_strategy") or "full_refresh"

            # Resolve where_filters once for any strategy that supports them.
            # Storage API extension does not expose whereFilters, so any
            # filter forces the SDK path. Resolution happens here so a
            # placeholder typo surfaces as a per-table error, not silent.
            resolved_filters = None
            raw_filters = tc.get("where_filters")
            if raw_filters:
                from connectors.keboola.where_filters import (
                    InvalidFilterError,
                    parse_filters,
                    resolve_placeholders,
                )

                try:
                    resolved_filters = resolve_placeholders(
                        parse_filters(raw_filters),
                        datetime.now(timezone.utc),
                    )
                except InvalidFilterError as e:
                    logger.error("where_filters invalid for %s: %s", table_name, e)
                    stats["tables_failed"] += 1
                    stats["errors"].append({"table": table_name, "error": f"where_filters: {e}"})
                    continue

            if sync_strategy == "incremental":
                try:
                    pq_path = data_dir / f"{table_name}.parquet"
                    last_sync = _read_last_sync_for_tc(tc)
                    from connectors.keboola.incremental import extract_incremental

                    incr_result = extract_incremental(
                        table_config=tc,
                        parquet_path=pq_path,
                        last_sync=last_sync,
                        keboola_url=keboola_url,
                        keboola_token=keboola_token,
                    )
                    safe_pq_lit = str(pq_path).replace("'", "''")
                    rows = incr_result["rows"]
                    size = pq_path.stat().st_size if pq_path.exists() else 0
                    conn.execute(
                        f"CREATE OR REPLACE VIEW \"{table_name}\" AS SELECT * FROM read_parquet('{safe_pq_lit}')"
                    )
                    conn.execute(
                        "INSERT INTO _meta VALUES (?, ?, ?, ?, ?, 'local')",
                        [table_name, tc.get("description", ""), rows, size, now],
                    )
                    stats["tables_extracted"] += 1
                    logger.info(
                        "Incremental %s: %d rows (%d delta), changedSince=%s",
                        table_name,
                        rows,
                        incr_result["delta_rows"],
                        incr_result["changed_since_used"],
                    )
                except Exception as e:
                    logger.error("Incremental extract failed for %s: %s", table_name, e)
                    stats["tables_failed"] += 1
                    stats["errors"].append({"table": table_name, "error": str(e)})
                continue

            if sync_strategy == "partitioned":
                try:
                    partition_dir = data_dir / table_name
                    partition_dir.mkdir(exist_ok=True)
                    last_sync = _read_last_sync_for_tc(tc)
                    from connectors.keboola.partitioned import extract_partitioned

                    part_result = extract_partitioned(
                        table_config=tc,
                        output_dir=partition_dir,
                        last_sync=last_sync,
                        keboola_url=keboola_url,
                        keboola_token=keboola_token,
                    )
                    glob_lit = str(partition_dir / "*.parquet").replace("'", "''")
                    rows = part_result["rows"]
                    size = sum(p.stat().st_size for p in partition_dir.glob("*.parquet"))
                    conn.execute(f"CREATE OR REPLACE VIEW \"{table_name}\" AS SELECT * FROM read_parquet('{glob_lit}')")
                    conn.execute(
                        "INSERT INTO _meta VALUES (?, ?, ?, ?, ?, 'local')",
                        [table_name, tc.get("description", ""), rows, size, now],
                    )
                    stats["tables_extracted"] += 1
                    logger.info(
                        "Partitioned %s: %d rows across %d partition file(s)",
                        table_name,
                        rows,
                        part_result.get("partitions_touched", part_result.get("partitions_written", 0)),
                    )
                except Exception as e:
                    logger.error("Partitioned extract failed for %s: %s", table_name, e)
                    stats["tables_failed"] += 1
                    stats["errors"].append({"table": table_name, "error": str(e)})
                continue

            # full_refresh fall-through: existing extension/legacy logic.
            # When where_filters are set we MUST force the legacy path
            # (extension lacks whereFilters support).
            try:
                pq_path = str(data_dir / f"{table_name}.parquet")

                if resolved_filters:
                    _extract_via_legacy(
                        tc,
                        pq_path,
                        keboola_url,
                        keboola_token,
                        where_filters=resolved_filters,
                    )
                elif use_extension:
                    try:
                        _extract_via_extension(conn, tc, pq_path)
                    except Exception as ext_err:
                        # ATTACH succeeded but the per-table COPY failed —
                        # most commonly a Keboola QueryService permission error
                        # (`Schema '..."in.c-..."' does not exist or not
                        # authorized`, see keboola/duckdb-extension#17). The
                        # legacy Storage-API client doesn't go through
                        # QueryService at all, so queue for the parallel
                        # legacy fallback below.
                        logger.warning(
                            "Keboola extension scan failed for %s (%s); queued for legacy Storage-API fallback",
                            table_name,
                            ext_err,
                        )
                        legacy_queue.append((tc, pq_path))
                        continue
                else:
                    legacy_queue.append((tc, pq_path))
                    continue

                # Extension path succeeded — register _meta synchronously.
                _register_local_meta(conn, tc, pq_path, now)
                stats["tables_extracted"] += 1
                rows_log = conn.execute(
                    f"SELECT count(*) FROM read_parquet('{pq_path.replace(chr(39), chr(39) * 2)}')"
                ).fetchone()[0]
                logger.info("Extracted %s via extension: %d rows", table_name, rows_log)

            except Exception as e:
                logger.error("Failed to extract %s: %s", table_name, e)
                stats["tables_failed"] += 1
                stats["errors"].append({"table": table_name, "error": str(e)})

        # Detach Keboola if extension was used
        if use_extension:
            try:
                conn.execute("DETACH kbc")
            except Exception:
                pass

        # Phase 2: legacy fallback in parallel. Keboola Storage API export
        # jobs are independent per table — a worker pool of N workers fans
        # out the per-table HTTP roundtrips (export job submit + poll +
        # CSV download) instead of stacking them sequentially. Project-level
        # concurrency is bounded by the storage.jobsParallelism limit
        # (typically 10); default to 4 to leave headroom for other clients.
        # Override via AGNES_KEBOOLA_PARALLELISM env var.
        #
        # Workers are PROCESSES, not threads — `connectors/keboola/client.py:
        # export_table` does `os.chdir(temp_dir)` to redirect kbcstorage's
        # slice-file downloads into a per-call temp directory, and `os.chdir`
        # is process-global. With threads, two parallel exports race on CWD
        # and slice files end up in the wrong directory; the merge step then
        # fails with `[Errno 2] No such file or directory:
        # '<job_id>.csv_X_Y_Z.csv'`. ProcessPoolExecutor gives each worker
        # its own process and therefore its own CWD.
        if legacy_queue:
            parallelism = max(1, int(os.environ.get("AGNES_KEBOOLA_PARALLELISM", "8")))
            workers = min(parallelism, len(legacy_queue))
            logger.info(
                "Running legacy Storage-API fallback for %d tables across %d worker processes",
                len(legacy_queue),
                workers,
            )

            if workers == 1:
                legacy_results = [_legacy_worker(item, keboola_url, keboola_token) for item in legacy_queue]
            else:
                from concurrent.futures import ProcessPoolExecutor

                with ProcessPoolExecutor(max_workers=workers) as ex:
                    futures = [ex.submit(_legacy_worker, item, keboola_url, keboola_token) for item in legacy_queue]
                    legacy_results = [f.result() for f in futures]

            # Phase 3: serial _meta insert for legacy results. DuckDB conn
            # isn't thread-safe, so we collect parallel work and only touch
            # `conn` (and `stats`) here on the main thread.
            for tc_, pq_, err in legacy_results:
                tn = tc_["name"]
                if err is not None:
                    logger.error("Failed to extract %s via legacy: %s", tn, err)
                    stats["tables_failed"] += 1
                    stats["errors"].append({"table": tn, "error": err})
                    continue
                try:
                    _register_local_meta(conn, tc_, pq_, now)
                    stats["tables_extracted"] += 1
                    rows_log = conn.execute(
                        f"SELECT count(*) FROM read_parquet('{pq_.replace(chr(39), chr(39) * 2)}')"
                    ).fetchone()[0]
                    logger.info("Extracted %s via legacy: %d rows", tn, rows_log)
                except Exception as e:
                    logger.error("Failed to register _meta for %s: %s", tn, e)
                    stats["tables_failed"] += 1
                    stats["errors"].append({"table": tn, "error": str(e)})

    finally:
        conn.execute("CHECKPOINT")
        conn.close()

    # Atomic replace: swap temp DB into place, cleaning up any WAL files
    import shutil

    old_wal = Path(str(db_path) + ".wal")
    if old_wal.exists():
        old_wal.unlink()

    if tmp_db_path.exists():
        shutil.move(str(tmp_db_path), str(db_path))

    tmp_wal = Path(str(tmp_db_path) + ".wal")
    if tmp_wal.exists():
        tmp_wal.unlink()

    return stats


def _register_local_meta(
    conn: duckdb.DuckDBPyConnection,
    tc: Dict[str, Any],
    pq_path: str,
    extracted_at: datetime,
) -> None:
    """After a parquet has been written for a local-mode table, create the
    DuckDB view and register the row in `_meta`. Hoisted out of the run()
    body so both the serial extension-success path and the parallel
    legacy-result path share one implementation."""
    table_name = tc["name"]
    safe_pq_lit = pq_path.replace("'", "''")
    rows = conn.execute(f"SELECT count(*) FROM read_parquet('{safe_pq_lit}')").fetchone()[0]
    size = os.path.getsize(pq_path)
    conn.execute(f"CREATE OR REPLACE VIEW \"{table_name}\" AS SELECT * FROM read_parquet('{safe_pq_lit}')")
    conn.execute(
        "INSERT INTO _meta VALUES (?, ?, ?, ?, ?, 'local')",
        [table_name, tc.get("description", ""), rows, size, extracted_at],
    )


def _extract_via_extension(conn: duckdb.DuckDBPyConnection, tc: Dict[str, Any], pq_path: str) -> None:
    """Extract a table using the DuckDB Keboola extension."""
    bucket = tc.get("bucket", "")
    source_table = tc.get("source_table", tc["name"])
    # #81 Group D — defense-in-depth. The caller already validates these;
    # refuse here too in case a future caller forgets. Use the relaxed
    # quoted-identifier check that accepts Keboola's `in.c-foo` form.
    if not (is_safe_quoted_identifier(bucket) and is_safe_quoted_identifier(source_table)):
        raise ValueError(f"unsafe bucket/source_table: {bucket!r}/{source_table!r}")
    safe_pq_lit = pq_path.replace("'", "''")
    conn.execute(f'COPY (SELECT * FROM kbc."{bucket}"."{source_table}") TO \'{safe_pq_lit}\' (FORMAT PARQUET)')


def _legacy_worker(tc_pq, keboola_url: str, keboola_token: str):
    """Module-level wrapper for ProcessPoolExecutor — must be picklable.

    Returns `(tc, pq_path, error_str_or_None)` so the main process can
    aggregate results and update _meta serially on its DuckDB connection.
    """
    tc_, pq_ = tc_pq
    try:
        _extract_via_legacy(tc_, pq_, keboola_url, keboola_token)
        return (tc_, pq_, None)
    except Exception as exc:
        return (tc_, pq_, str(exc))


def _extract_via_legacy(
    tc: Dict[str, Any],
    pq_path: str,
    keboola_url: str,
    keboola_token: str,
    where_filters: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Per-table extract via the Storage API export-async path with typed parquet.

    Despite the name (kept for caller compatibility with `_legacy_worker`),
    this no longer goes through the `kbcstorage` SDK — it talks to the
    Storage API directly via `connectors/keboola/storage_api.py`. The old
    SDK path had a thread-unsafe `os.chdir(temp_dir)` that broke parallel
    execution; the direct path uses per-call temp directories and signed-URL
    downloads, so threads / processes don't trip on each other.

    `where_filters` (v27) — when present, builds an `ExportFilter` so the
    Storage API applies the row filter server-side before signing the
    parquet/CSV file. Caller has already resolved any date placeholders
    (see `connectors/keboola/where_filters.py:resolve_placeholders`). The
    DuckDB Keboola extension does not expose whereFilters, so any v27
    filter row forces this path.

    The CSV → parquet conversion uses `connectors/keboola/parquet_io.csv_to_parquet`
    with the PyArrow schema + pandas dtypes pulled from Keboola column metadata
    (provider cascade `user > ai-metadata-enrichment > keboola.snowflake-transformation > storage`).
    Falls back to string-typed parquet only when the metadata API is unreachable.
    Pre-v27 this path used `read_csv(all_varchar=true)` which flattened every
    column to VARCHAR.
    """
    import tempfile
    from connectors.keboola.client import KeboolaClient
    from connectors.keboola.parquet_io import csv_to_parquet
    from connectors.keboola.storage_api import (
        ExportFilter,
        KeboolaStorageClient,
        get_temp_root,
    )

    bucket = tc.get("bucket", "")
    source_table = tc.get("source_table", tc["name"])
    table_id = f"{bucket}.{source_table}" if bucket else tc.get("id", tc["name"])

    # Pull column-level metadata for typed parquet (v27 typed-parquet fix).
    # `KeboolaClient` (kbcstorage SDK) is the only source of provider-cascade
    # PyArrow schema; storage_api.KeboolaStorageClient handles the export
    # path. Both are kept side by side intentionally.
    metadata_client = KeboolaClient(token=keboola_token, url=keboola_url)
    try:
        pyarrow_schema = metadata_client.get_pyarrow_schema(table_id)
    except Exception as e:
        logger.warning(
            "Keboola schema unavailable for %s (%s); writing string-typed parquet",
            table_id,
            e,
        )
        pyarrow_schema = None
    try:
        dtypes = metadata_client.get_pandas_dtypes(table_id) if pyarrow_schema else {}
    except Exception:
        dtypes = {}
    try:
        date_columns = metadata_client.get_date_columns(table_id) if pyarrow_schema else []
    except Exception:
        date_columns = []

    export_filter = None
    if where_filters:
        # ExportFilter validates shape (column/operator/values keys) on
        # to_export_params(); raises ValueError on malformed entries which
        # the worker wrapper turns into a per-table error.
        export_filter = ExportFilter(where_filters=list(where_filters))

    with tempfile.TemporaryDirectory(
        prefix=f"kbc-export-{tc['name']}-",
        dir=get_temp_root(),
        ignore_cleanup_errors=True,
    ) as tmpdir:
        csv_path = Path(tmpdir) / f"{tc['name']}.csv"
        client = KeboolaStorageClient(url=keboola_url, token=keboola_token)
        client.export_table_to_csv(table_id, csv_path, export_filter=export_filter)

        if not csv_path.exists() or csv_path.stat().st_size == 0:
            # Storage API succeeded but produced no rows. Emit an empty
            # parquet rather than crashing — same defensive behavior as
            # `materialize_query`.
            _open_consolidation_conn().execute(
                f"COPY (SELECT 1 AS _empty WHERE FALSE) TO '{pq_path}' (FORMAT PARQUET)"
            ).close()
            return

        # v27 typed-parquet path: use csv_to_parquet with PyArrow schema
        # from Keboola metadata. Falls through to string typing when
        # pyarrow_schema is None (metadata API unreachable).
        csv_to_parquet(
            csv_path=csv_path,
            parquet_path=Path(pq_path),
            dtypes=dtypes,
            date_columns=date_columns,
            pyarrow_schema=pyarrow_schema,
            table_id=table_id,
        )


def compute_exit_code(stats: Dict[str, Any], total: int) -> int:
    """Map an extraction `stats` dict to a process exit code.

    Issue #81 Group B: distinguish full success from partial failure so
    the sync API and CLI consumers can alert on partial vs. full failure
    rather than treating any non-zero as one bucket.

    - ``0`` — every table succeeded (or no tables registered).
    - ``1`` — every table failed (full failure).
    - ``2`` — at least one succeeded and at least one failed (partial).

    `total` is the count of tables the extractor was asked to process.
    `stats["tables_failed"]` is the count it actually failed.
    """
    failed = stats.get("tables_failed", 0)
    if total == 0:
        return 0
    if failed == 0:
        return 0
    if failed >= total:
        return 1
    return 2


if __name__ == "__main__":
    """Standalone: reads config from env + table_registry, runs extraction.

    Used by sync trigger subprocess. Reads KEBOOLA_STORAGE_TOKEN and
    KEBOOLA_STACK_URL from environment, table list from DuckDB registry.
    """
    from app.logging_config import setup_logging

    setup_logging(__name__)

    # Read Keboola credentials — env > vault > instance.yaml fallback
    url = os.environ.get("KEBOOLA_STACK_URL", "")
    from app.datasource_secrets import datasource_secret as _datasource_secret

    token = _datasource_secret("KEBOOLA_STORAGE_TOKEN") or ""

    if not url or not token:
        try:
            from config.loader import load_instance_config

            config = load_instance_config()
            kbc_config = config.get("keboola", {})
            url = url or kbc_config.get("url", "")
            token_env = kbc_config.get("token_env", "KEBOOLA_STORAGE_TOKEN")
            token = token or os.environ.get(token_env, "")
        except Exception:
            pass

    if not url or not token:
        logger.error("Missing KEBOOLA_STACK_URL or KEBOOLA_STORAGE_TOKEN")
        exit(1)

    # Read table list from registry
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository

    sys_conn = get_system_db()
    try:
        repo = TableRegistryRepository(sys_conn)
        tables = repo.list_by_source("keboola")
    finally:
        sys_conn.close()

    if not tables:
        logger.warning("No Keboola tables registered in table_registry")
        exit(0)

    logger.info("Extracting %d tables from %s", len(tables), url)
    data_dir = Path(os.environ.get("DATA_DIR", "./data"))
    result = run(str(data_dir / "extracts" / "keboola"), tables, url, token)
    logger.info("Extraction complete: %s", result)

    code = compute_exit_code(result, len(tables))
    if code == 2:
        logger.error("Partial failure: %d of %d tables failed", result.get("tables_failed", 0), len(tables))
    elif code == 1:
        logger.error("All %d tables failed", len(tables))
    exit(code)
