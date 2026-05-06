"""Sync endpoints — manifest, trigger, sync-settings, table-subscriptions."""

import hashlib
import logging
import os
import subprocess
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
import duckdb

from app.auth.access import require_admin
from app.auth.dependencies import get_current_user, _get_db
from app.utils import get_data_dir as _get_data_dir
from src.repositories.sync_state import SyncStateRepository
from src.repositories.sync_settings import SyncSettingsRepository
from src.repositories.table_registry import TableRegistryRepository
from src.rbac import can_access_table
from src.scheduler import filter_due_tables, is_table_due

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/sync", tags=["sync"])

# Process-wide guard against overlapping `_run_sync` invocations. Two
# concurrent extractor subprocesses both write `extract.duckdb` and fight
# for its file lock — the first sync stalls, the second crashes, and the
# `/api/health` check times out long enough that Docker flips the
# container to `unhealthy`, which (behind a `reverse_proxy` upstream)
# bricks external traffic until contention drains. The singleton-ness is
# enforced both in the trigger handler (return 409 fast, before the work
# is scheduled) and in `_run_sync` itself (defense in depth, in case
# something bypasses the handler).
_sync_lock = threading.Lock()


def _file_hash(path: Path) -> str:
    if not path.exists():
        return ""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _materialize_table(
    *,
    table_id: str,
    sql: str,
    bq,
    output_dir: str,
    max_bytes: Optional[int],
) -> dict:
    """Thin wrapper around `connectors.bigquery.extractor.materialize_query`
    so the trigger pass can be unit-tested by patching this seam without
    touching the real BqAccess factory or the duckdb import."""
    from connectors.bigquery.extractor import materialize_query
    return materialize_query(
        table_id=table_id, sql=sql, bq=bq,
        output_dir=output_dir, max_bytes=max_bytes,
    )


def _run_materialized_pass(conn: duckdb.DuckDBPyConnection, bq) -> dict:
    """Walk `table_registry` for `query_mode='materialized'` rows and run any
    that are due, dispatching by ``source_type`` to the correct connector's
    materialize_query. Honors per-table `sync_schedule` via `is_table_due()`,
    computes the file hash inline, and updates `sync_state` so the manifest
    can serve the row to `agnes pull` without re-hashing on every request.

    BigQuery rows go through BqAccess + bigquery_query() (jobs API),
    optionally cost-guarded by ``max_bytes_per_materialize``.
    Keboola rows go through KeboolaAccess + ATTACH-and-COPY, no
    guardrail (extension has no dry-run primitive).

    Returns:
        ``{"materialized": [ids], "skipped": [ids], "errors": [{table, error}]}``

    Errors are aggregated per row — one budget-blown table doesn't stop a
    healthy sibling. ``MaterializeBudgetError`` is caught and rendered with
    its structured fields so operator alerting can pick out the cap-vs-actual
    bytes from the log line.
    """
    from app.instance_config import get_value
    from connectors.bigquery.extractor import MaterializeBudgetError, MaterializeInFlightError

    bq_output_dir = str(Path(_get_data_dir()) / "extracts" / "bigquery")
    kb_output_dir = Path(_get_data_dir()) / "extracts" / "keboola" / "data"

    # Sentinel: max_bytes <= 0 (or None) disables the guardrail. `get_value()`
    # treats YAML `null` as "missing" → returns the default; operators must use
    # the explicit `0` sentinel to disable. See config/instance.yaml.example.
    # YAML accepts floats too (e.g. `10737418240.0`), and operators may
    # write `1e10` for readability; coerce to int and tolerate non-numeric
    # entries by falling through to the disable path with a warning.
    raw_max = get_value(
        "data_source", "bigquery", "max_bytes_per_materialize",
        default=10 * 2**30,
    )
    try:
        n = int(raw_max) if raw_max is not None else 0
    except (TypeError, ValueError):
        logger.warning(
            "data_source.bigquery.max_bytes_per_materialize is not numeric "
            "(%r); cost guardrail disabled. Set an integer or 0 to disable.",
            raw_max,
        )
        n = 0
    bq_max_bytes = n if n > 0 else None

    registry = TableRegistryRepository(conn)
    state = SyncStateRepository(conn)

    summary = {"materialized": [], "skipped": [], "errors": []}
    keboola_access = None  # lazy-init on first Keboola row

    for row in registry.list_all():
        if row.get("query_mode") != "materialized":
            continue

        # Convention across connectors: sync_state.table_id and the parquet
        # filename are keyed by `table_registry.name` (matches Keboola's
        # `_meta.table_name`) so the manifest's `registry_by_name` lookup
        # at `_build_manifest_for_user` resolves cleanly. Without this,
        # admins who register `name="Orders_90d"` (id slugified to
        # `orders_90d`) would see `query_mode` default to `"local"` in the
        # manifest because the lookup misses on `id`.
        ref_name = row["name"]

        last = state.get_last_sync(ref_name)
        last_iso = last.isoformat() if last else None
        schedule = row.get("sync_schedule") or "every 1h"
        if not is_table_due(schedule, last_iso):
            summary["skipped"].append({"table": ref_name, "reason": "due_check"})
            continue

        source_type = row.get("source_type") or "bigquery"  # legacy default

        # Dispatch by source_type. BQ rows keep using `_materialize_table`
        # (the existing test seam); Keboola rows use the new Keboola
        # materialize_query via a lazily-initialized KeboolaAccess.
        try:
            if source_type == "bigquery":
                stats = _materialize_table(
                    table_id=ref_name,
                    sql=row["source_query"],
                    bq=bq,
                    output_dir=bq_output_dir,
                    max_bytes=bq_max_bytes,
                )
            elif source_type == "keboola":
                if keboola_access is None:
                    # Lazy-init the Storage API client (replaces the old
                    # DuckDB extension `KeboolaAccess`). One client is shared
                    # across all keboola materialized rows in this pass —
                    # `requests.Session` inside it is thread-safe and reuses
                    # the connection pool for HTTP keep-alive across rows.
                    # Variable name kept as `keboola_access` to minimise
                    # diff churn against the surrounding error-handling
                    # block; the type is now `KeboolaStorageClient`.
                    from connectors.keboola.storage_api import KeboolaStorageClient
                    keboola_url = get_value(
                        "data_source", "keboola", "stack_url", default=""
                    ) or os.environ.get("KEBOOLA_STACK_URL", "")
                    token_env = get_value(
                        "data_source", "keboola", "token_env",
                        default="KEBOOLA_STORAGE_TOKEN",
                    ) or "KEBOOLA_STORAGE_TOKEN"
                    keboola_token = os.environ.get(token_env, "")
                    if not (keboola_url and keboola_token):
                        summary["errors"].append({
                            "table": ref_name,
                            "error": (
                                "Keboola URL/token not configured for "
                                "materialized path (data_source.keboola.stack_url "
                                f"+ env {token_env})"
                            ),
                        })
                        continue
                    keboola_access = KeboolaStorageClient(
                        url=keboola_url, token=keboola_token,
                    )
                kb_output_dir.mkdir(parents=True, exist_ok=True)
                from connectors.keboola.extractor import (
                    materialize_query as kb_materialize_query,
                )
                # Storage API needs the bucket+table split — registry rows
                # carry both fields per the standard register-table schema.
                bucket = row.get("bucket", "")
                source_table = row.get("source_table") or ref_name
                if not bucket:
                    summary["errors"].append({
                        "table": ref_name,
                        "error": (
                            "materialized keboola row is missing 'bucket'; "
                            "re-register with --bucket <in.c-...>"
                        ),
                    })
                    continue
                kb_stats = kb_materialize_query(
                    table_id=ref_name,
                    bucket=bucket,
                    source_table=source_table,
                    source_query=row.get("source_query"),
                    storage_client=keboola_access,
                    output_dir=kb_output_dir,
                )
                # Normalize Keboola materialize_query output to the shape the
                # BQ branch uses for downstream sync_state updates. KB returns
                # {table_id, path, rows, bytes, md5}; map to
                # {rows, size_bytes, hash}.
                stats = {
                    "rows": kb_stats["rows"],
                    "size_bytes": kb_stats["bytes"],
                    "hash": kb_stats["md5"],
                    "query_mode": "materialized",
                }
            else:
                summary["errors"].append({
                    "table": ref_name,
                    "error": (
                        f"materialized path not supported for "
                        f"source_type={source_type!r}"
                    ),
                })
                continue
        except MaterializeInFlightError:
            # In-flight on a sibling worker / scheduler tick — treat as
            # 'skipped, in-flight'. Do NOT call state.set_error: that
            # would flip status='error' on a healthy concurrent run and
            # the registry UI would surface a false-positive failure.
            summary["skipped"].append({"table": ref_name, "reason": "in_flight"})
            continue
        except MaterializeBudgetError as e:
            logger.warning(
                "Materialize cap exceeded for %s: %s bytes > %s bytes",
                e.table_id, f"{e.current:,}", f"{e.limit:,}",
            )
            summary["errors"].append({
                "table": ref_name,
                "error": str(e),
                "current": e.current,
                "limit": e.limit,
            })
            # Persist the failure so `GET /api/admin/registry` can surface
            # `last_sync_error` to the admin UI / `agnes admin status`.
            # Without this, scheduler stderr was the only place the cap
            # failure showed up and operators had no API path to it.
            state.set_error(ref_name, str(e))
            continue
        except Exception as e:
            logger.exception("Materialize failed for %s", ref_name)
            summary["errors"].append({"table": ref_name, "error": str(e)})
            state.set_error(ref_name, str(e))
            continue

        # `materialize_query` returns the parquet's MD5 inline — hashing
        # there means we don't re-read a multi-GB file on the request
        # thread. Fallback to `_file_hash(parquet_path)` if for some
        # reason the stats dict didn't carry it (defensive).
        parquet_hash = stats.get("hash")
        if not parquet_hash:
            output_dir_for_hash = (
                bq_output_dir if source_type == "bigquery" else str(kb_output_dir.parent)
            )
            parquet_path = Path(output_dir_for_hash) / "data" / f"{ref_name}.parquet"
            parquet_hash = _file_hash(parquet_path)
        # `update_sync` resets `status='ok'` / `error=NULL` on the upsert
        # path (its argument defaults), so a row that previously errored
        # has the failure cleared by this call. No separate clear_error
        # needed here — the test invariant is that a successful materialize
        # leaves status='ok' and error='', which `update_sync` already
        # establishes.
        state.update_sync(
            table_id=ref_name,
            rows=stats["rows"],
            file_size_bytes=stats["size_bytes"],
            hash=parquet_hash,
        )
        summary["materialized"].append(ref_name)

    return summary


def _run_sync(tables: Optional[List[str]] = None):
    """Run extractor as subprocess + orchestrator rebuild.

    Reads table configs from DuckDB (in main process which has the shared
    connection), passes them as JSON via stdin to the extractor subprocess.
    This avoids DuckDB lock conflicts — subprocess never opens system.duckdb.

    Singleton: only one invocation runs at a time per process (see
    `_sync_lock` module-level). The trigger handler also fast-fails with
    409 when the lock is held, so this branch is defense in depth.
    """
    import json as _json
    import sys as _sys

    if not _sync_lock.acquire(blocking=False):
        print(
            "[SYNC] another sync is already in flight — skipping",
            file=_sys.stderr, flush=True,
        )
        return

    try:
        from app.instance_config import get_data_source_type, get_value
        from src.db import get_system_db

        source_type = get_data_source_type()
        data_dir = _get_data_dir()

        # Read table configs in main process (has shared DuckDB connection)
        sys_conn = get_system_db()
        # Track whether the REGISTRY (not the post-filter list) was empty.
        # Auto-discovery must only fire on a truly empty registry; if the
        # filter returned [] because nothing was due, re-discovering would
        # bypass the schedule entirely on Keboola instances. (Devin BUG_0001
        # on ebb8cc9.)
        registry_has_tables = False
        try:
            repo = TableRegistryRepository(sys_conn)
            if tables:
                # Manual operator override — bypass schedule filter entirely
                # so an admin saying "sync these specific tables now" wins.
                all_configs = [repo.get(t) for t in tables]
                table_configs = [c for c in all_configs if c is not None]
                registry_has_tables = bool(table_configs)
            else:
                table_configs = repo.list_local(source_type) if source_type else repo.list_local()
                registry_has_tables = bool(table_configs)
                # Without this filter, every scheduler tick would re-sync
                # every table regardless of its sync_schedule cadence,
                # making the field a no-op at trigger time. Tables with
                # no schedule pass through unchanged (opt-in feature).
                state_repo = SyncStateRepository(sys_conn)
                table_configs = filter_due_tables(table_configs, state_repo)
        finally:
            sys_conn.close()

        if not table_configs:
            # Auto-discover tables on first sync when registry is empty.
            # `not registry_has_tables` is the load-bearing guard — without
            # it, "filter excluded everything" looks identical to "registry
            # empty" and we'd re-discover + re-sync every tick regardless of
            # sync_schedule.
            if not registry_has_tables and source_type == "keboola" and os.environ.get("KEBOOLA_STORAGE_TOKEN"):
                logger.info("No tables registered — running auto-discovery from Keboola")
                try:
                    from app.api.admin import _discover_and_register_tables
                    auto_conn = get_system_db()
                    try:
                        result = _discover_and_register_tables(auto_conn, "auto-discovery")
                        logger.info("Auto-discovered %d tables, skipped %d", result["registered"], result["skipped"])
                    finally:
                        auto_conn.close()
                    # Re-read table configs after auto-registration
                    sys_conn2 = get_system_db()
                    try:
                        table_configs = TableRegistryRepository(sys_conn2).list_local(source_type)
                    finally:
                        sys_conn2.close()
                except Exception as e:
                    logger.warning("Auto-discovery failed: %s", e)

        # CRITICAL: don't early-return when local-mode tables are empty.
        # `list_local("bigquery")` is always empty on BQ-only deployments
        # (BQ rows are always remote or materialized, never local), so an
        # early return would prevent the materialized pass AND the
        # orchestrator rebuild from ever firing on a BQ-only instance.
        # Devin BUG_0002 on PR #148 commit 2fa44f2. Just flag whether the
        # Keboola subprocess + custom-connectors should run; everything
        # below (materialized pass, orchestrator rebuild, profiler) runs
        # unconditionally so a registry with materialized rows but no
        # local rows still publishes them.
        run_extractor_subprocess = bool(table_configs)
        if not run_extractor_subprocess:
            logger.info(
                "No local-mode tables to sync for source_type=%s — "
                "skipping extractor subprocess; materialized pass + "
                "orchestrator rebuild still run.",
                source_type,
            )

        env = {**os.environ}

        if run_extractor_subprocess:
            # Serialize configs — strip non-serializable fields
            serializable = []
            for tc in table_configs:
                serializable.append({k: (v.isoformat() if hasattr(v, 'isoformat') else v)
                                     for k, v in tc.items() if v is not None})

            # Run extractor subprocess with table configs via stdin
            # Subprocess does NOT open system.duckdb — no lock conflict
            cmd = [_sys.executable, "-c", """
import json, sys, os, logging, signal
from pathlib import Path

# Subprocess inherits no logging config — without basicConfig, Python's
# lastResort handler only surfaces WARNING+ to stderr and INFO-level
# extraction progress from connectors.keboola.extractor.run() is silently
# dropped. capture_output=True in the parent then swallows the rest.
# Devin BUG_0002 on PR #136 review.
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# Convert SIGTERM into a controlled SystemExit so the ProcessPoolExecutor
# `with` block in connectors.keboola.extractor.run() runs its __exit__
# (shutdown/wait_for_workers) before this process dies. Without this,
# SIGTERM kills the parent abruptly, leaving the OS to clean up the pool
# children — but each worker holds an open Keboola Storage export job
# whose lifetime is tied to the HTTP poll loop, and those leak until the
# Keboola side TTLs them out. The parent extractor calls this from
# app.api.sync._run_sync after `subprocess.Popen(start_new_session=True)`
# + `os.killpg(SIGTERM)` on timeout.
def _exit_on_sigterm(signum, frame):
    sys.exit(143)
signal.signal(signal.SIGTERM, _exit_on_sigterm)

configs = json.load(sys.stdin)
url = os.environ.get("KEBOOLA_STACK_URL", "")
token = os.environ.get("KEBOOLA_STORAGE_TOKEN", "")

if not url or not token:
    print("ERROR: Missing KEBOOLA_STACK_URL or KEBOOLA_STORAGE_TOKEN", file=sys.stderr)
    sys.exit(1)

from connectors.keboola.extractor import run, compute_exit_code
data_dir = Path(os.environ.get("DATA_DIR", "./data"))
result = run(str(data_dir / "extracts" / "keboola"), configs, url, token)
print(json.dumps(result))
# Issue #81 Group B: surface partial-failure as exit 2 so the API
# caller can distinguish "every table failed" from "9/10 succeeded".
sys.exit(compute_exit_code(result, len(configs)))
"""]

            print(f"[SYNC] Starting extractor subprocess for {len(table_configs)} tables", file=_sys.stderr, flush=True)

            # Run in a new process group (start_new_session=True) so a
            # timeout can take down the whole tree — the extractor itself
            # plus any ProcessPoolExecutor workers it spawned for parallel
            # legacy-fallback. Without this, plain `subprocess.run` on
            # timeout SIGKILLs only the immediate child; the pool workers
            # are reparented to PID 1 and continue holding open Keboola
            # Storage export jobs, blocking the next sync cycle's
            # connectivity to those same job IDs.
            extractor_timeout = int(os.environ.get("AGNES_EXTRACTOR_TIMEOUT_SEC", "3600"))
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env=env,
                cwd=str(Path(__file__).parent.parent.parent),
                start_new_session=True,
            )
            try:
                stdout, stderr = proc.communicate(input=_json.dumps(serializable), timeout=extractor_timeout)
                result = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                # SIGTERM the whole process group first to give workers a
                # chance to shut down cleanly (release Keboola export jobs,
                # close DuckDB conns), then SIGKILL the stragglers after a
                # short grace window.
                import signal
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    proc.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    try:
                        proc.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                # Catch the timeout LOCALLY so the materialized BQ pass and
                # orchestrator rebuild below still fire — pre-fix the timeout
                # propagated to the outer except handler and skipped the rest
                # of `_run_sync` (Devin BUG_0001 on PR #148 commit 2219255).
                print(
                    f"[SYNC] Extractor timed out after {extractor_timeout}s — process "
                    "group killed; continuing to materialized pass + orchestrator rebuild",
                    file=_sys.stderr, flush=True,
                )
                result = None

            if result is not None:
                if result.stdout:
                    print(f"[SYNC] Extractor stdout: {result.stdout.strip()[-500:]}", file=_sys.stderr, flush=True)
                if result.stderr:
                    print(f"[SYNC] Extractor stderr: {result.stderr[-500:]}", file=_sys.stderr, flush=True)
                # Issue #81 Group B: three exit codes. 0 = full success,
                # 1 = full failure, 2 = partial. Partial is a data-quality
                # alert, not a crash — the orchestrator's per-table _meta
                # machinery already captured which tables succeeded; we just
                # need to log loudly so operator alerting can pick it up.
                if result.returncode == 0:
                    print(f"[SYNC] Extractor OK", file=_sys.stderr, flush=True)
                elif result.returncode == 2:
                    print(
                        f"[SYNC] Extractor PARTIAL FAILURE (exit 2) — some tables "
                        f"succeeded, some failed; see stderr for per-table errors. "
                        f"Successful tables will still be published by the orchestrator.",
                        file=_sys.stderr, flush=True,
                    )
                else:
                    print(f"[SYNC] Extractor FAILED (exit {result.returncode})", file=_sys.stderr, flush=True)

            # Run custom connectors (Tier A: local mount) — only when there
            # were local-mode tables to drive the extractor. Custom connectors
            # currently piggyback on the same env as the Keboola extractor.
            connectors_dir = Path(os.environ.get("CONNECTORS_DIR", str(Path(__file__).parent.parent.parent / "connectors" / "custom")))
            if connectors_dir.exists():
                for connector_dir in sorted(connectors_dir.iterdir()):
                    if not connector_dir.is_dir():
                        continue
                    extractor = connector_dir / "extractor.py"
                    if not extractor.exists():
                        continue
                    logger.info("Running custom connector: %s", connector_dir.name)
                    try:
                        custom_result = subprocess.run(
                            [_sys.executable, str(extractor)],
                            env=env, capture_output=True, text=True, timeout=600,
                            cwd=str(Path(__file__).parent.parent.parent),
                        )
                        if custom_result.returncode != 0:
                            logger.error("Custom connector %s failed: %s", connector_dir.name, custom_result.stderr[-500:])
                        else:
                            logger.info("Custom connector %s completed", connector_dir.name)
                    except subprocess.TimeoutExpired:
                        logger.error("Custom connector %s timed out", connector_dir.name)

        # Materialized SQL pass — runs admin-registered SQL through the
        # source's DuckDB extension (BQ via BqAccess, Keboola via
        # KeboolaAccess) and writes parquet for due rows. _run_materialized_pass
        # itself dispatches by source_type, so we always run it regardless of
        # which (or both) source types have a `project` / `stack_url` set —
        # Keboola-only instances would otherwise silently skip Keboola
        # materialized rows just because no BQ project is configured (Devin
        # finding 2026-05-01: BUG_pr-review-job-3fbd31c9_0001). The BQ
        # branch inside _run_materialized_pass uses a per-row try/except so
        # the sentinel BqAccess (not_configured) raises a typed error that
        # gets recorded against that row only — no cascade.
        try:
            from connectors.bigquery.access import get_bq_access
            from src.db import get_system_db as _get_system_db
            bq_access = get_bq_access()  # sentinel if no BQ project; OK
            mat_conn = _get_system_db()
            try:
                mat_summary = _run_materialized_pass(mat_conn, bq_access)
            finally:
                mat_conn.close()
            skipped_count = len(mat_summary["skipped"])
            in_flight_count = sum(
                1 for s in mat_summary["skipped"] if s.get("reason") == "in_flight"
            )
            print(
                f"[SYNC] Materialized SQL: {len(mat_summary['materialized'])} ok, "
                f"{skipped_count} skipped (in_flight={in_flight_count}), "
                f"{len(mat_summary['errors'])} errors",
                file=_sys.stderr, flush=True,
            )
            for err in mat_summary["errors"]:
                print(
                    f"[SYNC]   {err['table']}: {err['error']}",
                    file=_sys.stderr, flush=True,
                )
        except Exception as e:
            print(
                f"[SYNC] Materialized SQL pass FAILED: {e}",
                file=_sys.stderr, flush=True,
            )
            traceback.print_exc()

        # Rebuild master views (reads extract.duckdb files, no write conflict)
        from src.orchestrator import SyncOrchestrator
        orch = SyncOrchestrator()
        views = orch.rebuild()
        print(f"[SYNC] Orchestrator rebuild: {{{', '.join(f'{k}: {len(v)}' for k, v in views.items())}}}", file=_sys.stderr, flush=True)

        # Auto-profile synced tables (best-effort, don't fail sync on profile error)
        try:
            from src.profiler import profile_table, TableInfo
            from src.repositories.profiles import ProfileRepository

            data_dir = Path(os.environ.get("DATA_DIR", "./data"))
            extracts_dir = data_dir / "extracts"

            sys_conn = get_system_db()
            try:
                profile_repo = ProfileRepository(sys_conn)
                profiled = 0
                for source_name, table_names in views.items():
                    for table_name in table_names[:10]:  # Limit per sync
                        pq_path = extracts_dir / source_name / "data" / f"{table_name}.parquet"
                        if not pq_path.exists():
                            continue
                        try:
                            table_info = TableInfo(name=table_name, table_id=table_name)
                            profile = profile_table(table_info, pq_path, [], {}, {})
                            profile_repo.save(table_name, profile)
                            profiled += 1
                        except Exception as pe:
                            print(f"[SYNC] Profile {table_name}: {pe}", file=_sys.stderr, flush=True)
                print(f"[SYNC] Profiled {profiled} tables", file=_sys.stderr, flush=True)
            finally:
                sys_conn.close()
        except Exception as e:
            print(f"[SYNC] Profiler skipped: {e}", file=_sys.stderr, flush=True)

    except subprocess.TimeoutExpired:
        # Outer-handler fallback for any subprocess.run call site (e.g.
        # custom-connectors below) that didn't already catch its own
        # TimeoutExpired. Concrete timeout value isn't available here —
        # log generically.
        print("[SYNC] Extractor subprocess timed out", file=_sys.stderr, flush=True)
    except Exception as e:
        print(f"[SYNC] FAILED: {e}", file=_sys.stderr, flush=True)
        traceback.print_exc()
    finally:
        _sync_lock.release()


# ---- Manifest ----

def _build_manifest_for_user(conn, user: dict) -> dict:
    """Build manifest dict filtered by user's accessible tables.

    Joins ``sync_state`` with ``table_registry`` so each table entry exposes
    ``query_mode`` and ``source_type``. The CLI uses these to decide whether
    to download a parquet (local) or skip it (remote, e.g. BigQuery views).

    Defensive defaults: if a sync_state row has no matching registry entry
    (race / manual deletion), fall back to ``query_mode='local'`` and
    ``source_type=''`` so the manifest still serializes cleanly.
    """
    sync_repo = SyncStateRepository(conn)
    table_repo = TableRegistryRepository(conn)
    all_states = sync_repo.get_all_states()
    # `sync_state.table_id` is sourced from `_meta.table_name` which equals
    # `table_registry.name`, NOT `table_registry.id`. Auto-discovered Keboola
    # tables and manually-registered ones with mixed-case/spaced names produce
    # id != name; an id-keyed lookup would miss them and silently default to
    # `query_mode=local`, causing the CLI to try downloading remote tables.
    registry_by_name = {t["name"]: t for t in table_repo.list_all()}

    # Filter by user's accessible tables. `can_access_table` has its own
    # admin shortcut (Admin group → True). Lookup translates name→id first
    # because `s["table_id"]` is sourced from `_meta.table_name` = registry
    # `name` while `can_access_table` keys on registry `id`; when id != name
    # an id-keyed call would miss.
    def _id_for(state):
        reg = registry_by_name.get(state["table_id"])
        return reg["id"] if reg else state["table_id"]
    all_states = [s for s in all_states if can_access_table(user, _id_for(s), conn)]

    data_dir = _get_data_dir()
    tables = {}
    for state in all_states:
        table_id = state["table_id"]
        reg = registry_by_name.get(table_id, {})
        tables[table_id] = {
            "hash": state.get("hash", ""),
            "updated": state.get("last_sync").isoformat() if state.get("last_sync") else None,
            "size_bytes": state.get("file_size_bytes", 0),
            "rows": state.get("rows", 0),
            "query_mode": reg.get("query_mode") or "local",
            "source_type": reg.get("source_type") or "",
        }

    # Asset hashes
    docs_dir = data_dir / "docs"
    assets = {}
    for asset_name, asset_path in [
        ("docs", docs_dir),
        ("profiles", data_dir / "src_data" / "metadata" / "profiles.json"),
    ]:
        if asset_path.exists():
            if asset_path.is_file():
                assets[asset_name] = {"hash": _file_hash(asset_path)}
            else:
                newest = max(
                    (f.stat().st_mtime for f in asset_path.rglob("*") if f.is_file()),
                    default=0,
                )
                assets[asset_name] = {"hash": str(int(newest))}

    return {
        "tables": tables,
        "assets": assets,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/manifest")
async def sync_manifest(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Return hash-based manifest of all synced data, filtered per user."""
    return _build_manifest_for_user(conn, user)


# ---- Trigger ----

@router.post("/trigger")
async def trigger_sync(
    background_tasks: BackgroundTasks,
    tables: Optional[List[str]] = None,
    user: dict = Depends(require_admin),
):
    """Trigger data sync from configured source. Admin only. Runs in background.

    Returns 409 if a previously-triggered sync is still running. Two
    concurrent extractor subprocesses fight for the same `extract.duckdb`
    file lock — that contention starves uvicorn, makes `/api/health` time
    out, flips the container to `unhealthy`, and (behind a `reverse_proxy`
    upstream like the bundled Caddy overlay) bricks external traffic
    until contention drains. Fast-fail here keeps that from happening.
    """
    if _sync_lock.locked():
        raise HTTPException(
            status_code=409,
            detail="sync_already_in_progress",
        )
    background_tasks.add_task(_run_sync, tables)
    return {
        "status": "triggered",
        "tables": tables or "all",
        "message": "Data sync started in background. Check /api/health for progress.",
    }


# ---- Sync Settings (dataset subscriptions) ----

class SyncSettingsUpdate(BaseModel):
    datasets: dict  # {dataset_name: bool}


@router.get("/settings")
async def get_sync_settings(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Get user's dataset sync settings."""
    repo = SyncSettingsRepository(conn)
    settings = repo.get_user_settings(user["id"])
    enabled = repo.get_enabled_datasets(user["id"])
    return {
        "user_id": user["id"],
        "settings": settings,
        "enabled_datasets": enabled,
    }


@router.post("/settings")
async def update_sync_settings(
    request: SyncSettingsUpdate,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Update user's dataset sync settings.

    A dataset can only be enabled when the user has access (via
    ``resource_grants(group, "table", dataset)`` or Admin membership). The
    user_sync_settings layer is per-user preference, not authorization —
    the gate stops users from enabling sync on tables they cannot read.
    """
    from app.auth.access import can_access
    from app.resource_types import ResourceType

    settings_repo = SyncSettingsRepository(conn)
    results = {}
    for dataset, enabled in request.datasets.items():
        if not can_access(user["id"], ResourceType.TABLE.value, dataset, conn):
            results[dataset] = {"error": "no permission"}
            continue
        settings_repo.set_dataset_enabled(user["id"], dataset, enabled)
        results[dataset] = {"enabled": enabled}

    return {"updated": results}


# ---- Table Subscriptions ----

class TableSubscriptionUpdate(BaseModel):
    table_mode: str = "all"  # "all" or "explicit"
    tables: dict = {}  # {table_name: bool}


@router.get("/table-subscriptions")
async def get_table_subscriptions(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Get user's per-table subscription settings."""
    repo = SyncSettingsRepository(conn)
    settings = repo.get_user_settings(user["id"])
    return {"user_id": user["id"], "subscriptions": settings}


@router.post("/table-subscriptions")
async def update_table_subscriptions(
    request: TableSubscriptionUpdate,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Update per-table subscription preferences."""
    repo = SyncSettingsRepository(conn)
    for table_name, enabled in request.tables.items():
        repo.set_dataset_enabled(user["id"], table_name, enabled)
    return {"table_mode": request.table_mode, "updated": len(request.tables)}
