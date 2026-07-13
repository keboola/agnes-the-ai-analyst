"""Sync endpoints — manifest, trigger, sync-settings, table-subscriptions."""

import hashlib
import json
import logging
import os
import subprocess
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, List

from fastapi import APIRouter, Body, Depends, HTTPException, BackgroundTasks, Query
from pydantic import BaseModel, Field
import duckdb

from app.auth.access import require_admin
from app.auth.dependencies import get_current_user, _get_db
from app.utils import get_data_dir as _get_data_dir
from src.audit_helpers import client_kind_from_user
from src.rbac import can_access_table
from src.scheduler import filter_due_tables, is_table_due

from src.repositories import (
    audit_repo,
    connection_secrets_repo,
    data_packages_repo,
    file_corpora_repo,
    memory_domains_repo,
    profile_repo,
    source_connections_repo,
    sync_settings_repo,
    sync_state_repo,
    table_registry_repo,
    usage_repo,
    users_repo,
)

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

# Race-protection: the trigger handler returns 200 BEFORE the background task
# acquires ``_sync_lock``. In that ~few-hundred-ms gap, ``/api/sync/status``
# would honestly report ``locked=False`` — and the host-side
# ``agnes-auto-upgrade.sh`` defer probe (which polls this endpoint) would
# proceed with ``docker compose up -d`` and SIGKILL the still-spawning
# extractor / materialized worker. Mid-sync container kill is the exact
# class of corruption the WAL replay auto-recovery is meant to be a
# safety net for, not a routine occurrence.
#
# Fix: stamp the trigger time alongside the lock. ``/api/sync/status`` also
# returns ``locked=True`` for ``_TRIGGER_HOLD_SEC`` seconds after the most
# recent trigger, even if the background task hasn't yet acquired the lock.
# The window is short enough that an operator-issued ``/api/sync/trigger``
# followed by an immediate ``GET /api/sync/status`` is consistent
# (locked=True), but long enough to cover the schedule → background-task
# spawn latency. Defense in depth: the real lock still gates the
# extractor subprocess.
_TRIGGER_HOLD_SEC = 30
_recent_trigger_at: float = 0.0  # monotonic clock; 0 = never triggered


def _file_hash(path: Path) -> str:
    if not path.exists():
        return ""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_extractor_stats(stdout: Optional[str]) -> Optional[dict]:
    """Parse the Keboola extractor subprocess's stats dict back out of its
    stdout (#754).

    The subprocess (the inline ``-c`` script built in ``_run_sync``) prints
    exactly one line — ``print(json.dumps(result))`` — as the LAST thing it
    writes to stdout, right before exiting with a code computed by
    ``compute_exit_code``. It cannot write per-table failures to
    ``system.duckdb`` itself: the parent process holds that connection's
    lock for the duration of the sync (see the module docstring on
    ``_sync_lock``), so a second writer would fight it. This stdout line is
    therefore the only channel for ``{tables_extracted, tables_failed,
    errors: [{table, error}]}`` to reach the parent, which is what
    previously discarded per-table extractor errors, leaving `agnes admin`
    / the admin UI with no explanation for "N total, 0 synced" beyond a
    generic exit-code message.

    Defensive: a truncated/garbled/empty stdout (e.g. the subprocess was
    SIGKILLed mid-flush) returns ``None`` rather than raising — the caller
    already has an exit-code-derived fallback message.
    """
    if not stdout:
        return None
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _materialize_table(
    *,
    table_id: str,
    sql: str,
    bq,
    output_dir: str,
    max_bytes: Optional[int],
    fetch_timeout_s: Optional[float] = None,
) -> dict:
    """Thin wrapper around `connectors.bigquery.extractor.materialize_query`
    so the trigger pass can be unit-tested by patching this seam without
    touching the real BqAccess factory or the duckdb import."""
    from connectors.bigquery.extractor import materialize_query

    return materialize_query(
        table_id=table_id,
        sql=sql,
        bq=bq,
        output_dir=output_dir,
        max_bytes=max_bytes,
        fetch_timeout_s=fetch_timeout_s,
    )


def _run_materialized_pass(
    conn: duckdb.DuckDBPyConnection,
    bq,
    tables: Optional[List[str]] = None,
    source_type: Optional[str] = None,
) -> dict:
    """Walk `table_registry` for `query_mode='materialized'` rows and run any
    that are due, dispatching by ``source_type`` to the correct connector's
    materialize_query. Honors per-table `sync_schedule` via `is_table_due()`,
    computes the file hash inline, and updates `sync_state` so the manifest
    can serve the row to `agnes pull` without re-hashing on every request.

    ``tables`` (when not None) restricts the pass to a specific subset —
    targeted re-syncs from the operator (POST /api/sync/trigger with a
    body) need this, otherwise an admin asking to re-sync `kbc_job` would
    re-process every other materialized row that's also due. Matched
    against both the registry id and name (admins often pass either).

    ``source_type`` (when not None) restricts the pass to rows whose
    registry ``source_type`` matches — the partial-rebuild path
    (POST /api/sync/trigger?source=bigquery) uses it so a BQ-only
    rebuild leaves Keboola materialized rows untouched, and vice versa.

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

    #754: skip reasons bounded enough to be worth persisting across a
    process restart (``source_filter``, ``not_in_target``, ``in_flight``)
    are ALSO written to ``sync_state`` via ``state.set_skipped(...)`` so
    ``GET /api/admin/registry`` / ``agnes admin list-tables`` can explain
    a "0 synced" run. The routine per-tick ``due_check`` skip is
    deliberately NOT persisted — it fires on nearly every scheduler tick
    for every scheduled table and would otherwise turn every tick into an
    UPDATE storm for information the row's own ``last_sync`` already
    conveys.
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
        "data_source",
        "bigquery",
        "max_bytes_per_materialize",
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

    # Fetch-phase watchdog for the COPY's client-side result download —
    # the phase the BQ extension's own timeout does not cover. Default
    # 15 min (a healthy fetch of a multi-hundred-MB result is ~1 min; a
    # wedged stream otherwise holds the per-table lock for hours and
    # starves the daily schedule). Explicit `0` disables.
    raw_fetch_timeout = get_value(
        "data_source",
        "bigquery",
        "materialize_fetch_timeout_seconds",
        default=900,
    )
    try:
        t = float(raw_fetch_timeout) if raw_fetch_timeout is not None else 900.0
    except (TypeError, ValueError):
        logger.warning(
            "data_source.bigquery.materialize_fetch_timeout_seconds is not "
            "numeric (%r); using the 900s default. Set a number of seconds "
            "or 0 to disable.",
            raw_fetch_timeout,
        )
        t = 900.0
    bq_fetch_timeout_s = t if t > 0 else None

    registry = table_registry_repo()
    state = sync_state_repo()

    summary = {"materialized": [], "skipped": [], "errors": []}
    # Per-connection-id cache of KeboolaStorageClient instances.
    # Keyed by connection_id (str) or None for the global/instance token.
    # A single client is shared across all rows that share the same
    # connection_id — requests.Session inside it reuses the HTTP keep-alive
    # pool across rows, same as the old single-client pattern.
    keboola_clients: dict = {}

    # Targeted-trigger filter. Compare against both id and name so an admin
    # who passes either form (the registry id slug, or the human-friendly
    # name) gets the same result. `None` means "no filter — process all
    # due materialized rows".
    target_set: Optional[set] = set(tables) if tables is not None else None

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

        # Partial-rebuild scoping (POST /api/sync/trigger?source=…). Compute
        # the row's source_type once, with the same `or "bigquery"` legacy
        # default the dispatch below uses, so the filter and the dispatch
        # agree on how a NULL-source_type row is classified.
        row_source_type = row.get("source_type") or "bigquery"  # legacy default
        if source_type is not None and row_source_type != source_type:
            summary["skipped"].append({"table": ref_name, "reason": "source_filter"})
            # Persisted (#754) — a partial `?source=` rebuild is an explicit,
            # bounded-frequency request (not a routine per-tick skip), so an
            # operator later looking at `GET /api/admin/registry` for "why
            # didn't this sync" sees the real reason instead of a stale row.
            state.set_skipped(ref_name, "source_filter")
            continue

        if target_set is not None and not (ref_name in target_set or row.get("id") in target_set):
            summary["skipped"].append({"table": ref_name, "reason": "not_in_target"})
            state.set_skipped(ref_name, "not_in_target")
            continue

        last = state.get_last_sync(ref_name)
        last_iso = last.isoformat() if last else None
        # Per-table schedule wins; fall through to AGNES_DEFAULT_SYNC_SCHEDULE
        # (operator override), then to ``every 1h`` (OSS-historical default).
        # The env knob lets a deployment dial down the platform-wide refresh
        # cadence without having to PUT every registry row — useful when
        # data freshness budget is "once per day" and the hourly default
        # over-fetches.
        schedule = row.get("sync_schedule") or os.environ.get("AGNES_DEFAULT_SYNC_SCHEDULE", "").strip() or "every 1h"
        if not is_table_due(schedule, last_iso):
            summary["skipped"].append({"table": ref_name, "reason": "due_check"})
            continue

        # Dispatch by source_type. BQ rows keep using `_materialize_table`
        # (the existing test seam); Keboola rows use the new Keboola
        # materialize_query via a lazily-initialized KeboolaAccess.
        try:
            if row_source_type == "bigquery":
                stats = _materialize_table(
                    table_id=ref_name,
                    sql=row["source_query"],
                    bq=bq,
                    output_dir=bq_output_dir,
                    max_bytes=bq_max_bytes,
                    fetch_timeout_s=bq_fetch_timeout_s,
                )
            elif row_source_type == "keboola":
                conn_id = row.get("connection_id")
                if conn_id not in keboola_clients:
                    from connectors.keboola.storage_api import KeboolaStorageClient

                    if conn_id:
                        # Per-connection resolution: look up the named
                        # source_connection record and resolve its token.
                        # Vault takes priority; falls back to the env var
                        # named in the record's token_env field.
                        sc = source_connections_repo().get(conn_id)
                        if not sc:
                            summary["errors"].append(
                                {
                                    "table": ref_name,
                                    "error": f"connection_id {conn_id!r} not found in source_connections",
                                }
                            )
                            continue
                        sc_url = sc["config"].get("stack_url", "")
                        sc_token = connection_secrets_repo().get(conn_id) or os.environ.get(
                            sc.get("token_env") or "", ""
                        )
                        if not (sc_url and sc_token):
                            summary["errors"].append(
                                {
                                    "table": ref_name,
                                    "error": f"connection {conn_id!r} missing URL or token",
                                }
                            )
                            continue
                    else:
                        # Global/instance token path (backwards compatible).
                        sc_url = get_value("data_source", "keboola", "stack_url", default="") or os.environ.get(
                            "KEBOOLA_STACK_URL", ""
                        )
                        token_env = (
                            get_value(
                                "data_source",
                                "keboola",
                                "token_env",
                                default="KEBOOLA_STORAGE_TOKEN",
                            )
                            or "KEBOOLA_STORAGE_TOKEN"
                        )
                        sc_token = os.environ.get(token_env, "")
                        if not sc_token:
                            from app.datasource_secrets import datasource_secret as _ds_secret

                            sc_token = _ds_secret("KEBOOLA_STORAGE_TOKEN") or ""
                        if not (sc_url and sc_token):
                            summary["errors"].append(
                                {
                                    "table": ref_name,
                                    "error": (
                                        "Keboola URL/token not configured for "
                                        "materialized path (data_source.keboola.stack_url "
                                        f"+ env {token_env})"
                                    ),
                                }
                            )
                            continue
                    keboola_clients[conn_id] = KeboolaStorageClient(
                        url=sc_url,
                        token=sc_token,
                    )
                keboola_access = keboola_clients[conn_id]
                kb_output_dir.mkdir(parents=True, exist_ok=True)
                from connectors.keboola.extractor import (
                    materialize_query as kb_materialize_query,
                )

                # Storage API needs the bucket+table split — registry rows
                # carry both fields per the standard register-table schema.
                bucket = row.get("bucket", "")
                source_table = row.get("source_table") or ref_name
                if not bucket:
                    summary["errors"].append(
                        {
                            "table": ref_name,
                            "error": (
                                "materialized keboola row is missing 'bucket'; re-register with --bucket <in.c-...>"
                            ),
                        }
                    )
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
                summary["errors"].append(
                    {
                        "table": ref_name,
                        "error": (f"materialized path not supported for source_type={row_source_type!r}"),
                    }
                )
                continue
        except MaterializeInFlightError:
            # In-flight on a sibling worker / scheduler tick — treat as
            # 'skipped, in-flight'. Do NOT call state.set_error: that
            # would flip status='error' on a healthy concurrent run and
            # the registry UI would surface a false-positive failure.
            # set_skipped (#754) persists the same non-error distinction so
            # `GET /api/admin/registry` explains the miss instead of leaving
            # the row's prior state unexplained.
            summary["skipped"].append({"table": ref_name, "reason": "in_flight"})
            state.set_skipped(ref_name, "in_flight")
            continue
        except MaterializeBudgetError as e:
            logger.warning(
                "Materialize cap exceeded for %s: %s bytes > %s bytes",
                e.table_id,
                f"{e.current:,}",
                f"{e.limit:,}",
            )
            summary["errors"].append(
                {
                    "table": ref_name,
                    "error": str(e),
                    "current": e.current,
                    "limit": e.limit,
                }
            )
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
            output_dir_for_hash = bq_output_dir if row_source_type == "bigquery" else str(kb_output_dir.parent)
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


def _run_sync(
    tables: Optional[List[str]] = None,
    source_type_filter: Optional[str] = None,
):
    """Run extractor as subprocess + orchestrator rebuild.

    Reads table configs from DuckDB (in main process which has the shared
    connection), passes them as JSON via stdin to the extractor subprocess.
    This avoids DuckDB lock conflicts — subprocess never opens system.duckdb.

    ``source_type_filter`` (POST /api/sync/trigger?source=…) restricts the
    rebuild to a single registered source:

      - the local-mode list is selected with ``list_local(source_type_filter)``
        so only matching rows reach the extractor subprocess;
      - the Keboola extractor subprocess (which only knows how to extract
        Keboola rows) is skipped entirely unless the filter is None or
        ``"keboola"``;
      - the materialized pass receives the same filter so only matching
        ``source_type`` rows are rebuilt.

    The orchestrator rebuild always runs — it re-ATTACHes whatever
    ``extract.duckdb`` files exist on disk and never rewrites the ones it
    reads, so a scoped rebuild leaves the other source's extract untouched.

    Singleton: only one invocation runs at a time per process (see
    `_sync_lock` module-level). The trigger handler also fast-fails with
    409 when the lock is held, so this branch is defense in depth.
    """
    import json as _json
    import sys as _sys

    if not _sync_lock.acquire(blocking=False):
        print(
            "[SYNC] another sync is already in flight — skipping",
            file=_sys.stderr,
            flush=True,
        )
        return

    # Accumulates per-table failures across the sync (materialized pass +
    # extractor) so both the per-table operator alert below and the fatal-path
    # alert in the outer `except` can report the same context.
    collected_errors: List[dict] = []

    try:
        from app.instance_config import get_data_source_type
        from src.db import get_system_db

        source_type = get_data_source_type()
        # Partial-rebuild scoping: when an explicit `?source=` filter is set,
        # it overrides the instance's configured source_type for row
        # selection (a dual-source deployment can ask to rebuild only BQ or
        # only Keboola). Falls back to the instance source_type for the
        # default full sweep.
        effective_source_type = source_type_filter or source_type
        data_dir = _get_data_dir()

        # Reclaim orphaned `kbc-export-*` staging dirs left behind when a
        # previous sync worker was hard-killed mid-export (SIGKILL / OOM /
        # auto-upgrade container recreate) so TemporaryDirectory.__exit__
        # never ran. Runs here — under `_sync_lock`, before any new scratch
        # is created — so it can never race a live in-flight export from this
        # process (and the age-gate covers any other container). Best-effort:
        # a sweep failure must never block the sync itself.
        try:
            from connectors.keboola.storage_api import sweep_orphaned_scratch

            sweep_orphaned_scratch()
        except Exception as _sweep_exc:  # pragma: no cover - defensive
            print(
                f"[SYNC] orphaned-scratch sweep skipped: {_sweep_exc}",
                file=_sys.stderr,
                flush=True,
            )

        # Read table configs in main process (has shared DuckDB connection)
        # Track whether the REGISTRY (not the post-filter list) was empty.
        # Auto-discovery must only fire on a truly empty registry; if the
        # filter returned [] because nothing was due, re-discovering would
        # bypass the schedule entirely on Keboola instances. (Devin BUG_0001
        # on ebb8cc9.)
        registry_has_tables = False
        repo = table_registry_repo()
        if tables:
            # Manual operator override — bypass schedule filter entirely
            # so an admin saying "sync these specific tables now" wins.
            all_configs = [repo.get(t) for t in tables]
            table_configs = [c for c in all_configs if c is not None]
            registry_has_tables = bool(table_configs)
        else:
            table_configs = repo.list_local(effective_source_type) if effective_source_type else repo.list_local()
            # Auto-discover gate must consider the WHOLE registry, not
            # just `local` rows. After the Keboola migration to
            # materialized (v25→v26), an instance can have 30
            # materialized Keboola rows and zero local rows — but
            # `bool(table_configs)` here would be False, and
            # `not registry_has_tables` would re-trigger
            # `_discover_and_register_tables` on every scheduler tick,
            # creating duplicate "auto-discovered" rows with the wrong
            # bucket prefix every time.
            # Use list_all (any source, any mode) for the gate.
            registry_has_tables = bool(repo.list_all())
            # Without this filter, every scheduler tick would re-sync
            # every table regardless of its sync_schedule cadence,
            # making the field a no-op at trigger time. Tables with
            # no schedule pass through unchanged (opt-in feature).
            state_repo = sync_state_repo()
            table_configs = filter_due_tables(table_configs, state_repo)

        if not table_configs:
            # Auto-discover tables on first sync when registry is empty.
            # `not registry_has_tables` is the load-bearing guard — without
            # it, "filter excluded everything" looks identical to "registry
            # empty" and we'd re-discover + re-sync every tick regardless of
            # sync_schedule.
            if not os.environ.get("KEBOOLA_STORAGE_TOKEN"):
                try:
                    from app.datasource_secrets import datasource_secret as _ds  # noqa: PLC0415

                    _kbc_token_available = bool(_ds("KEBOOLA_STORAGE_TOKEN"))
                except Exception:
                    _kbc_token_available = False
            else:
                _kbc_token_available = True
            if not registry_has_tables and source_type == "keboola" and _kbc_token_available:
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
                    table_configs = table_registry_repo().list_local(effective_source_type)
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
        # The extractor subprocess below only knows how to extract Keboola
        # rows (it runs `connectors.keboola.extractor`). A partial rebuild
        # scoped to a non-Keboola source must never invoke it — otherwise a
        # `?source=bigquery` trigger would rewrite the Keboola extract.duckdb
        # via the subprocess and the rebuild would not be isolated.
        keboola_extract_in_scope = source_type_filter in (None, "keboola")
        run_extractor_subprocess = bool(table_configs) and keboola_extract_in_scope
        if not run_extractor_subprocess:
            logger.info(
                "No local-mode tables to sync for source_type=%s "
                "(filter=%s) — skipping extractor subprocess; materialized "
                "pass + orchestrator rebuild still run.",
                effective_source_type,
                source_type_filter,
            )

        env = {**os.environ}
        if not env.get("KEBOOLA_STORAGE_TOKEN"):
            from app.datasource_secrets import datasource_secret

            _vt = datasource_secret("KEBOOLA_STORAGE_TOKEN")
            if _vt:
                env["KEBOOLA_STORAGE_TOKEN"] = _vt

        if run_extractor_subprocess:
            # v26: incremental + partitioned strategies need last_sync from
            # sync_state to compute changedSince. The subprocess MUST NOT
            # reopen system.duckdb (parent holds the lock — see contract at
            # the top of this function), so the parent reads watermarks
            # here and injects them into each table_config under the key
            # `__last_sync__`. extractor.run() picks them up via
            # _read_last_sync's first-check-config-then-fall-back pattern.
            ws_repo = sync_state_repo()
            for tc in table_configs:
                if tc.get("sync_strategy") in ("incremental", "partitioned"):
                    state = ws_repo.get_table_state(tc.get("id") or tc.get("name"))
                    if state and state.get("status") != "error":
                        ls = state.get("last_sync")
                        if ls is not None:
                            tc["__last_sync__"] = ls

            # Serialize configs — strip non-serializable fields
            serializable = []
            for tc in table_configs:
                serializable.append(
                    {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in tc.items() if v is not None}
                )

            # Run extractor subprocess with table configs via stdin
            # Subprocess does NOT open system.duckdb — no lock conflict
            cmd = [
                _sys.executable,
                "-c",
                """
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
""",
            ]

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
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
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
                    file=_sys.stderr,
                    flush=True,
                )
                result = None
                # Record the timeout so the per-table webhook alert fires —
                # this LOCAL catch (the common timeout path) sets result=None
                # and skips the exit-code error collection below, so without
                # this append a clean materialized pass + rebuild would leave
                # collected_errors empty and the operator never learns the
                # extractor stalled (#397, #648 review).
                collected_errors.append(
                    {
                        "table": "(keboola extractor)",
                        "error": f"extractor timed out after {extractor_timeout}s — process group killed",
                    }
                )

            if result is not None:
                if result.stdout:
                    print(f"[SYNC] Extractor stdout: {result.stdout.strip()[-500:]}", file=_sys.stderr, flush=True)
                if result.stderr:
                    print(f"[SYNC] Extractor stderr: {result.stderr[-500:]}", file=_sys.stderr, flush=True)

                # #754 — recover the subprocess's per-table stats (it can't
                # write system.duckdb itself; the parent holds that lock for
                # the duration of the sync) and persist real failures via
                # sync_state.set_error so `GET /api/admin/registry` /
                # `agnes admin list-tables` can explain a "N total, 0 synced"
                # run instead of an operator having to trawl container logs
                # for the 500-char stdout tail above.
                extractor_stats = _parse_extractor_stats(result.stdout)
                extractor_table_errors = (extractor_stats or {}).get("errors") or []
                if extractor_table_errors:
                    err_state = sync_state_repo()
                    for entry in extractor_table_errors:
                        tname = entry.get("table")
                        terror = entry.get("error")
                        if tname and terror:
                            err_state.set_error(tname, terror)
                            collected_errors.append({"table": tname, "error": terror})

                # Issue #81 Group B: three exit codes. 0 = full success,
                # 1 = full failure, 2 = partial. Partial is a data-quality
                # alert, not a crash — the orchestrator's per-table _meta
                # machinery already captured which tables succeeded; we just
                # need to log loudly so operator alerting can pick it up.
                if result.returncode == 0:
                    print("[SYNC] Extractor OK", file=_sys.stderr, flush=True)
                elif result.returncode == 2:
                    print(
                        "[SYNC] Extractor PARTIAL FAILURE (exit 2) — some tables "
                        "succeeded, some failed; see stderr for per-table errors. "
                        "Successful tables will still be published by the orchestrator.",
                        file=_sys.stderr,
                        flush=True,
                    )
                    # Real per-table entries (just persisted above) are more
                    # actionable than this placeholder — only fall back to it
                    # when the stats line couldn't be recovered at all.
                    if not extractor_table_errors:
                        collected_errors.append(
                            {
                                "table": "(keboola extractor)",
                                "error": "partial failure (exit 2) — see server logs for per-table errors",
                            }
                        )
                else:
                    print(f"[SYNC] Extractor FAILED (exit {result.returncode})", file=_sys.stderr, flush=True)
                    if not extractor_table_errors:
                        collected_errors.append(
                            {
                                "table": "(keboola extractor)",
                                "error": f"extractor failed (exit {result.returncode}) — see server logs",
                            }
                        )

            # Run custom connectors (Tier A: local mount) — only when there
            # were local-mode tables to drive the extractor. Custom connectors
            # currently piggyback on the same env as the Keboola extractor.
            connectors_dir = Path(
                os.environ.get("CONNECTORS_DIR", str(Path(__file__).parent.parent.parent / "connectors" / "custom"))
            )
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
                            env=env,
                            capture_output=True,
                            text=True,
                            timeout=600,
                            cwd=str(Path(__file__).parent.parent.parent),
                        )
                        if custom_result.returncode != 0:
                            logger.error(
                                "Custom connector %s failed: %s", connector_dir.name, custom_result.stderr[-500:]
                            )
                            # Symmetry with the Keboola extractor exit-code
                            # path — a failed custom connector must also reach
                            # the webhook alert, not just stderr (#648 review).
                            collected_errors.append(
                                {
                                    "table": f"(custom connector: {connector_dir.name})",
                                    "error": f"connector failed (exit {custom_result.returncode}) — see server logs",
                                }
                            )
                        else:
                            logger.info("Custom connector %s completed", connector_dir.name)
                    except subprocess.TimeoutExpired:
                        logger.error("Custom connector %s timed out", connector_dir.name)
                        collected_errors.append(
                            {
                                "table": f"(custom connector: {connector_dir.name})",
                                "error": "connector timed out after 600s",
                            }
                        )

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
                mat_summary = _run_materialized_pass(
                    mat_conn,
                    bq_access,
                    tables=tables,
                    source_type=source_type_filter,
                )
            finally:
                mat_conn.close()
            skipped_count = len(mat_summary["skipped"])
            in_flight_count = sum(1 for s in mat_summary["skipped"] if s.get("reason") == "in_flight")
            print(
                f"[SYNC] Materialized SQL: {len(mat_summary['materialized'])} ok, "
                f"{skipped_count} skipped (in_flight={in_flight_count}), "
                f"{len(mat_summary['errors'])} errors",
                file=_sys.stderr,
                flush=True,
            )
            for err in mat_summary["errors"]:
                print(
                    f"[SYNC]   {err['table']}: {err['error']}",
                    file=_sys.stderr,
                    flush=True,
                )
            # Carry the per-table failures forward for the operator alert
            # (fired after this block, and also surfaced if a later fatal
            # error hits the outer except).
            collected_errors.extend(mat_summary["errors"])
        except Exception as e:
            print(
                f"[SYNC] Materialized SQL pass FAILED: {e}",
                file=_sys.stderr,
                flush=True,
            )
            traceback.print_exc()
            # The whole materialized pass blowing up is itself a per-table-ish
            # failure operators should hear about; record it so the alert below
            # (and the fatal-path alert) include it.
            collected_errors.append({"table": "(materialized pass)", "error": str(e)})

        # Rebuild master views (reads extract.duckdb files, no write conflict)
        from src.orchestrator import SyncOrchestrator

        orch = SyncOrchestrator()
        views = orch.rebuild()
        print(
            f"[SYNC] Orchestrator rebuild: {{{', '.join(f'{k}: {len(v)}' for k, v in views.items())}}}",
            file=_sys.stderr,
            flush=True,
        )

        # Auto-profile synced tables (best-effort, don't fail sync on profile error).
        #
        # Each profile runs in a fresh Python subprocess (``src._profiler_worker``)
        # so all DuckDB allocator state — including the anon mmap arenas that
        # ``profile_table`` accumulates per call — is reliably reclaimed by the
        # OS on subprocess exit. Pre-subprocess, running this loop in-process
        # against ~30 tables would drift the resident set up by ~100-300 MiB
        # per iteration (Python's malloc keeps freed arenas, libc keeps the
        # heap), eventually tripping the cgroup OOM around 4 GiB even though
        # each individual ``profile_table`` cleaned up its DuckDB session
        # correctly. See PR notes for the empirical traces.
        #
        # The parent still owns the repository ``save(...)`` write so the
        # system.duckdb lock semantics stay single-writer: the worker
        # returns the profile dict, the parent persists it.
        try:
            from src._subprocess_runner import run_subprocess_job, SubprocessJobError

            data_dir = Path(os.environ.get("DATA_DIR", "./data"))
            extracts_dir = data_dir / "extracts"

            profiles = profile_repo()
            profiled = 0
            for source_name, table_names in views.items():
                for table_name in table_names[:10]:  # Limit per sync
                    pq_path = extracts_dir / source_name / "data" / f"{table_name}.parquet"
                    if not pq_path.exists():
                        continue
                    try:
                        profile = run_subprocess_job(
                            "src._profiler_worker",
                            {
                                "table_name": table_name,
                                "table_id": table_name,
                                "parquet_path": str(pq_path),
                            },
                            timeout_sec=600,
                        )
                        profiles.save(table_name, profile)
                        profiled += 1
                    except SubprocessJobError as pe:
                        # Worker-side failure — log subprocess stderr tail
                        # to surface the actual traceback to operators.
                        print(
                            f"[SYNC] Profile {table_name}: {pe}\n  stderr tail: {pe.stderr[-500:]}",
                            file=_sys.stderr,
                            flush=True,
                        )
                    except Exception as pe:
                        print(f"[SYNC] Profile {table_name}: {pe}", file=_sys.stderr, flush=True)
            print(f"[SYNC] Profiled {profiled} tables", file=_sys.stderr, flush=True)
        except Exception as e:
            print(f"[SYNC] Profiler skipped: {e}", file=_sys.stderr, flush=True)

        # Operator alert on per-table sync errors (non-fatal). Fired at the
        # END of the try — AFTER the orchestrator rebuild — not mid-flow:
        # if a later step (rebuild) raises, the fatal handler below sends a
        # single combined alert (failed_tables=collected_errors, fatal=e)
        # instead of this firing first and the fatal path firing a second,
        # overlapping POST for the same run (#648 review). Best-effort:
        # notify_sync_failure no-ops without a webhook and never raises.
        if collected_errors:
            try:
                from app.services.sync_notifier import notify_sync_failure

                notify_sync_failure(failed_tables=collected_errors, fatal=None)
            except Exception:
                logger.exception("sync-failure notifier raised on per-table path")

    except subprocess.TimeoutExpired as e:
        # Outer-handler fallback for any subprocess.run call site (e.g.
        # custom-connectors below) that didn't already catch its own
        # TimeoutExpired. Concrete timeout value isn't available here —
        # log generically.
        print("[SYNC] Extractor subprocess timed out", file=_sys.stderr, flush=True)
        # A swallowed timeout is exactly the silent failure this feature
        # exists to surface — alert operators, same best-effort wrapping as
        # the generic-exception path below (#397, #648 review).
        try:
            from app.services.sync_notifier import notify_sync_failure

            notify_sync_failure(failed_tables=collected_errors, fatal=e)
        except Exception:
            logger.exception("sync-failure notifier raised on timeout path")
    except Exception as e:
        print(f"[SYNC] FAILED: {e}", file=_sys.stderr, flush=True)
        traceback.print_exc()
        # Operator alert on the fatal path. Best-effort: notify_sync_failure
        # never raises, but wrap anyway so an import-time issue can't mask the
        # original failure or leave _sync_lock held.
        try:
            from app.services.sync_notifier import notify_sync_failure

            notify_sync_failure(failed_tables=collected_errors, fatal=e)
        except Exception:
            logger.exception("sync-failure notifier raised on fatal path")
    finally:
        _sync_lock.release()


# ---- Manifest ----


def _table_manifest_entry(state: dict, reg: dict) -> dict:
    """Shape one ``sync_state`` row + registry metadata into the per-table
    manifest object used in ``data_packages[].tables`` and ``direct_tables``.

    Tolerant to empty ``state`` (table is registered but never synced) and
    empty ``reg`` (sync_state row outlives the registry — race on unregister).
    Both happen in real installs; the manifest is the read path so we must
    not blow up on a partially-consistent snapshot.
    """
    name = state.get("table_id") or reg.get("name") or reg.get("id") or ""
    return {
        "id": reg.get("id") or name,
        "name": name,
        "hash": state.get("hash", ""),
        "md5": state.get("hash", ""),
        "size_bytes": state.get("file_size_bytes", 0),
        "rows": state.get("rows", 0),
        "query_mode": reg.get("query_mode") or "local",
        # #607 — distribution flag. Listed in the manifest (catalog + RBAC)
        # but `agnes pull` skips its parquet download when true.
        "server_only": bool(reg.get("server_only")),
        "source_type": reg.get("source_type") or "",
        "updated": (state.get("last_sync").isoformat() if state.get("last_sync") else None),
    }


def _build_data_packages_section(conn, user, registry_by_name: dict, states_by_table_id: dict) -> tuple[list, set]:
    """Build the ``data_packages`` array per Section 5.1 of the design.

    Returns the list plus a set of ``table_registry.id`` values that were
    surfaced via at least one package — used to subtract from
    ``direct_tables`` so a table belonging to a package doesn't double-render.
    """
    from app.resource_types import ResourceType
    from app.services.stack_resolver import StackResolver
    from app.auth.session_principal import SessionPrincipal

    resolver = StackResolver(conn)
    stack_subject = user if isinstance(user, SessionPrincipal) else user["id"]
    pkg_entries = resolver.stack(stack_subject, ResourceType.DATA_PACKAGE)
    if not pkg_entries:
        return [], set()
    repo = data_packages_repo()
    packaged_table_ids: set = set()
    out: list = []
    for entry in pkg_entries:
        pkg = repo.get(entry.id)
        if not pkg:
            continue
        table_rows = repo.list_tables(entry.id)
        tables_payload: list = []
        total_size_bytes = 0
        for t in table_rows:
            packaged_table_ids.add(t["id"])
            # registry_by_name keys on name; sync_state.table_id mirrors
            # registry.name today. Cover the id↔name asymmetry.
            reg = registry_by_name.get(t["name"]) or {}
            state = states_by_table_id.get(t["name"]) or states_by_table_id.get(t["id"]) or {}
            entry_obj = _table_manifest_entry(state, reg or {"id": t["id"]})
            tables_payload.append(entry_obj)
            total_size_bytes += int(entry_obj.get("size_bytes") or 0)
        out.append(
            {
                "id": pkg["id"],
                "slug": pkg["slug"],
                "name": pkg["name"],
                "icon": pkg.get("icon"),
                "color": pkg.get("color"),
                "description": pkg.get("description"),
                "requirement": entry.requirement,
                "tables": tables_payload,
                "total_size_bytes": total_size_bytes,
            }
        )
    return out, packaged_table_ids


def _build_knowledge_artifacts_section(user) -> list:
    """``knowledge_artifacts`` manifest array: K3 chunk artifacts + K4 digests.

    Two independent ``kind`` families share this one list (the seam K3 left
    open, ``src/knowledge_packaging.py`` module docstring): ``kind:"chunks"``
    per-corpus ``knowledge.duckdb`` artifacts, and ``kind:"digest"`` maintained
    digests (K4, #799). Each family has its own RBAC filter and its own
    "nothing built yet" empty case — a caller with digests but zero packaged
    corpora (or vice versa) must still see the family they DO have access to,
    so neither branch early-returns on the other's empty state. Both helpers
    always return a (possibly empty) list, so this key is ALWAYS present in
    the manifest — ``agnes pull`` gates its prune on key presence, mirroring
    the typed-sections gate.
    """
    return _chunk_artifact_entries(user) + _digest_entries(user)


def _chunk_artifact_entries(user) -> list:
    """Per-corpus knowledge.duckdb artifacts (K3, #798), collection-grant filtered.

    Reads ``DATA_DIR/knowledge/state.json`` (written by the packaging pass) and
    lists only corpora the caller may access — the same fail-closed filter as
    ``/api/collections``.
    """
    from app.api.collections import _accessible_corpus_ids
    from src.knowledge_packaging import artifacts_dir, load_state

    state = load_state()
    if not state:
        return []
    allowed = set(_accessible_corpus_ids(user))
    names = {c["id"]: c.get("name") for c in file_corpora_repo().list()}
    out = []
    for cid in sorted(state):
        if cid not in allowed or not (artifacts_dir() / f"{cid}.duckdb").exists():
            continue
        entry = state[cid]
        out.append(
            {
                "kind": "chunks",
                "corpus_id": cid,
                "name": names.get(cid),
                "md5": entry.get("md5", ""),
                "size_bytes": entry.get("size_bytes", 0),
                "chunks": entry.get("chunks", 0),
                "built_at": entry.get("built_at"),
                "url": f"/api/knowledge/artifacts/{cid}/download",
            }
        )
    return out


def _digest_entries(user) -> list:
    """``kind:"digest"`` manifest entries (K4, #799), knowledge-digest-grant filtered.

    Frozen shape (see the K4 plan): ``{kind, id, slug, title, status,
    status_reason, generated_at, md5, url}``. ``md5`` is a change-detection
    token — not a byte-level integrity check of the downloaded content (the
    JSON body over TLS+PAT is the truth, the per-domain md5 posture,
    ``_build_memory_domains_section`` above) — computed over
    ``slug|status|status_reason|generated_at|output_md`` so it flips when
    EITHER content OR staleness changes: a digest going stale must re-fetch
    so the staleness banner reaches ``agnes pull``'s ``.claude/rules/`` copy.

    Digests with no ``output_md`` yet (``status='pending'``, never
    generated) are never listed — nothing to distribute. Sorted by slug.
    """
    from app.api.knowledge_search import _caller_can_read_digest
    from src.repositories import knowledge_digests_repo

    out = []
    for d in knowledge_digests_repo().list():
        output_md = d.get("output_md") or ""
        if not output_md.strip():
            continue
        if not _caller_can_read_digest(user, d["id"]):
            continue
        status = d.get("status") or "pending"
        status_reason = d.get("status_reason")
        generated_at = d.get("generated_at")
        generated_at_str = generated_at.isoformat() if generated_at else None
        token = f"{d['slug']}|{status}|{status_reason or ''}|{generated_at_str or ''}|{output_md}"
        out.append(
            {
                "kind": "digest",
                "id": d["id"],
                "slug": d["slug"],
                "title": d["title"],
                "status": status,
                "status_reason": status_reason,
                "generated_at": generated_at_str,
                "md5": hashlib.md5(token.encode()).hexdigest(),
                "url": f"/api/knowledge/digests/{d['id']}/content",
            }
        )
    return sorted(out, key=lambda e: e["slug"])


def _build_memory_domains_section(conn, user) -> list:
    """Build the ``memory_domains`` array per Section 5.1.

    Each entry carries a per-domain ``md5`` derived from the concatenated
    item content/titles inside the domain — when the bundle changes the
    md5 flips so the CLI knows to re-fetch.

    TODO(phase-7): ``bundle_url`` points at a yet-to-implement per-domain
    bundle endpoint (``/api/memory/bundle?domain=<slug>``). The CLI in
    Phase 7 will need it; for now we emit the URL the future endpoint
    will live at so older clients keep parsing the manifest cleanly.
    """
    from app.resource_types import ResourceType
    from app.services.stack_resolver import StackResolver
    from app.auth.session_principal import SessionPrincipal

    resolver = StackResolver(conn)
    stack_subject = user if isinstance(user, SessionPrincipal) else user["id"]
    dom_entries = resolver.stack(stack_subject, ResourceType.MEMORY_DOMAIN)
    if not dom_entries:
        return []
    repo = memory_domains_repo()
    out: list = []
    for entry in dom_entries:
        dom = repo.get(entry.id)
        if not dom:
            continue
        items = repo.list_items_of_domain(entry.id, limit=10000)
        # Per-domain md5 — concatenate sorted item tuples so the hash
        # is stable under list ordering and flips on any content
        # mutation. MUST include ``is_required`` and ``content``
        # because the bundle rendered by ``_build_per_domain_markdown``
        # routes items between "## Required" and "## Approved" by
        # ``is_required`` and embeds the full ``content`` body; without
        # these in the hash, an admin edit of either dimension leaves
        # the manifest md5 unchanged → ``agnes pull`` skips the
        # re-fetch → analyst keeps a stale bundle.md.
        #
        # Filter to the SAME predicate the renderer uses (any
        # ``is_required`` item OR ``status='approved' AND not is_required``)
        # so edits to pending/rejected non-required items don't flip the
        # md5 against an identical-bytes bundle — the original Devin
        # review flagged this asymmetry (BUG-0001 fixed the hash inputs;
        # this commit closes the matching 🚩 ANALYSIS that the SET of
        # items hashed must also match what the renderer emits).
        h = hashlib.md5()
        renderable = [it for it in items if it.get("is_required") or it.get("status") == "approved"]
        for it in sorted(renderable, key=lambda r: r["id"]):
            h.update(
                f"{it['id']}|{it.get('title', '')}|{it.get('status', '')}|"
                f"{it.get('is_required', False)}|{it.get('content', '')}|".encode()
            )
        required_count = sum(1 for it in items if (it.get("status") == "approved" and it.get("is_required")))
        out.append(
            {
                "id": dom["id"],
                "slug": dom["slug"],
                "name": dom["name"],
                "icon": dom.get("icon"),
                "color": dom.get("color"),
                "description": dom.get("description"),
                "requirement": entry.requirement,
                "bundle_url": f"/api/memory/bundle?domain={dom['slug']}",
                "md5": h.hexdigest(),
                "items_count": len(items),
                "required_count": required_count,
            }
        )
    return out


def _build_direct_tables_section(
    conn,
    user: dict,
    registry_by_name: dict,
    states_by_table_id: dict,
    packaged_table_ids: set,
) -> list:
    """Always returns ``[]`` — per-table grants no longer manifest for
    analysts.

    The unified-stack design routes all analyst access through data
    packages: admins manage RBAC by adding tables to a package and
    granting the package. Ad-hoc ``resource_grants(group, 'table', …)``
    rows that aren't wrapped in a package used to ship as
    ``direct_tables[]`` here (for backwards-compat with pre-unified
    CLIs); that BC is now dropped because it silently leaked
    individually-granted tables into ``agnes catalog`` and the
    user-facing manifest, contradicting the "stack is the unit of
    access" promise of the new design.

    The empty array is kept in the manifest payload (instead of
    omitting the key) so older CLIs that destructure
    ``manifest["direct_tables"]`` don't KeyError.
    """
    return []


def _build_manifest_for_user(conn, user: dict) -> dict:
    """Build manifest dict filtered by user's accessible tables.

    Joins ``sync_state`` with ``table_registry`` so each table entry exposes
    ``query_mode`` and ``source_type``. The CLI uses these to decide whether
    to download a parquet (local) or skip it (remote, e.g. BigQuery views).

    Defensive defaults: if a sync_state row has no matching registry entry
    (race / manual deletion), fall back to ``query_mode='local'`` and
    ``source_type=''`` so the manifest still serializes cleanly.

    v49: extended with ``data_packages`` / ``memory_domains`` /
    ``direct_tables`` arrays per Section 5.1 of the unified-stack design.
    Legacy ``tables`` dict stays in parallel for one release — older CLIs
    still parse it; newer clients prefer the typed sections.
    """
    sync_repo = sync_state_repo()
    table_repo = table_registry_repo()
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
            # #607 — distribution flag consumed by the cli/lib/pull.py
            # download-set loop: listed here but its parquet is not fetched.
            "server_only": bool(reg.get("server_only")),
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

    # v49 unified-stack manifest extensions (Section 5.1).
    # DEPRECATED v49: ``tables`` dict above is kept paralel for one release —
    # older CLIs depend on it; new clients prefer ``direct_tables`` +
    # ``data_packages[].tables``.
    states_by_table_id = {s["table_id"]: s for s in all_states}
    try:
        data_packages, packaged_ids = _build_data_packages_section(
            conn,
            user,
            registry_by_name,
            states_by_table_id,
        )
    except Exception:
        logger.exception("manifest data_packages section build failed")
        data_packages, packaged_ids = [], set()
    try:
        memory_domains = _build_memory_domains_section(conn, user)
    except Exception:
        logger.exception("manifest memory_domains section build failed")
        memory_domains = []
    try:
        direct_tables = _build_direct_tables_section(
            conn,
            user,
            registry_by_name,
            states_by_table_id,
            packaged_ids,
        )
    except Exception:
        logger.exception("manifest direct_tables section build failed")
        direct_tables = []
    try:
        knowledge_artifacts = _build_knowledge_artifacts_section(user)
    except Exception:
        logger.exception("manifest knowledge_artifacts section build failed")
        knowledge_artifacts = []

    return {
        "tables": tables,
        "assets": assets,
        "server_time": datetime.now(timezone.utc).isoformat(),
        "data_packages": data_packages,
        "memory_domains": memory_domains,
        "direct_tables": direct_tables,
        "knowledge_artifacts": knowledge_artifacts,
    }


@router.get("/manifest")
async def sync_manifest(
    user=Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Return hash-based manifest of all synced data, filtered per user.

    Side-effect: stamps ``users.last_pull_at`` so the /home status frame
    can show when the analyst last pulled. This GET is the canonical
    "I am about to sync" signal — agnes pull hits it first, then
    downloads parquets whose hash changed. UI bumps (manifest browsed in
    a browser session) also count; cheap and accurate enough for a
    homepage card.
    """
    from app.auth.session_principal import SessionPrincipal

    if not isinstance(user, SessionPrincipal):
        try:
            users_repo().update(user["id"], last_pull_at=datetime.now(timezone.utc))
            # Also emit an audit_log row so /me/stats Sync activity has a
            # timeline of pulls (the column UPDATE only retains the most
            # recent one). Action `manifest.fetch` covers both `agnes pull`
            # via PAT and browser-driven manifest peeks; clients can
            # disambiguate via client_kind.
            audit_repo().log(
                user_id=user["id"],
                action="manifest.fetch",
                resource="manifest",
                result="ok",
                client_kind="api",
            )
        except Exception:
            # Never block a pull because the stamp UPDATE / audit row hit a
            # transient issue (locked WAL, partial migration window). The
            # manifest itself is the load-bearing payload.
            pass
        # v49 Section 9.2 — emit a server-side ``sync.pull_started`` event so
        # /admin/telemetry can count distinct pulls per user per day. Best-effort.
        try:
            usage_repo().emit_server_event(
                event_type="sync.pull_started",
                user_id=user["id"],
                username=user.get("email") or user["id"],
                props={"client_kind": client_kind_from_user(user)},
            )
        except Exception:
            pass
    return _build_manifest_for_user(conn, user)


# ---- Pull confirm (Phase 7, Task 7.6) ----


class PullConfirmTypeReport(BaseModel):
    added: int = 0
    updated: int = 0
    removed: int = 0


class PullConfirmRequest(BaseModel):
    """Per-type aggregate the CLI submits after every pull finishes.

    Pairs with the ``sync.pull_started`` event emitted by GET /manifest
    so admin telemetry can compute pull-success rates + duration
    distributions. Optional fields fall back to zero counts — older CLI
    versions that don't track a section emit nothing for it.
    """

    duration_ms: Optional[int] = None
    direct_tables: Optional[PullConfirmTypeReport] = None
    data_packages: Optional[PullConfirmTypeReport] = None
    memory_domains: Optional[PullConfirmTypeReport] = None
    errors: int = 0


@router.post("/pull-confirm")
async def pull_confirm(
    payload: PullConfirmRequest,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Telemetry hook the CLI fires at the end of every ``agnes pull``.

    Best-effort: a telemetry insert failure must NOT bubble up to the
    CLI (the user already has their parquets, the pull succeeded). The
    response is a fixed shape ``{"recorded": True}`` so older clients
    that ignore the body keep working when the field set evolves.
    """
    props: dict = {
        "duration_ms": payload.duration_ms,
        "errors": payload.errors,
        "client_kind": client_kind_from_user(user),
    }
    for section in ("direct_tables", "data_packages", "memory_domains"):
        section_payload = getattr(payload, section)
        if section_payload is not None:
            props[f"{section}_added"] = section_payload.added
            props[f"{section}_updated"] = section_payload.updated
            props[f"{section}_removed"] = section_payload.removed

    try:
        usage_repo().emit_server_event(
            event_type="sync.pull_completed",
            user_id=user["id"],
            username=user.get("email") or user["id"],
            props=props,
        )
    except Exception:
        logger.warning("usage_events emit failed for sync.pull_completed")
    return {"recorded": True}


# ---- Status ----


@router.get("/status")
async def sync_status():
    """Whether a sync is currently in flight on this app process.

    Public (no auth) — used by the host-side ``agnes-auto-upgrade.sh``
    cron to decide whether to skip a `docker compose up -d` that would
    kill a running extractor / materialized pass mid-flight. Cheap to
    serve (single Lock.locked() check) and contains no sensitive data.

    Returns:
        ``{"locked": bool}`` — True if `_sync_lock` is currently held by
        a `_run_sync` invocation, OR a sync was triggered within the
        last ``_TRIGGER_HOLD_SEC`` seconds (so the FastAPI background
        task hasn't yet acquired the lock). Without the trigger-hold
        window, an auto-upgrade probe firing in the gap between the
        trigger handler's 200 response and the background task's
        ``_sync_lock.acquire()`` would see ``locked=False`` and proceed
        with ``up -d`` — killing the just-spawning extractor.
    """
    locked = _sync_lock.locked()
    if not locked and _recent_trigger_at:
        # Monotonic deadline; clock skew / DST jumps don't matter.
        locked = (time.monotonic() - _recent_trigger_at) < _TRIGGER_HOLD_SEC
    return {"locked": locked}


# ---- Trigger ----


@router.post("/trigger")
async def trigger_sync(
    background_tasks: BackgroundTasks,
    body: Optional[Any] = Body(None),
    source: Optional[str] = Query(
        None,
        description=(
            "Restrict the rebuild to one registered source_type (e.g. `keboola`, `bigquery`). Omit for a full sweep."
        ),
    ),
    user: dict = Depends(require_admin),
):
    """Trigger data sync from configured source. Admin only. Runs in background.

    Body accepts three shapes (all optional — empty body / `null` syncs
    every registered table):

      - ``["kbc_job", "orders"]`` — bare JSON array of table ids
      - ``{"tables": ["kbc_job", "orders"]}`` — object with a ``tables``
        key (matches the wire shape of the response, more discoverable
        for clients building requests by hand)
      - ``null`` / no body — sync everything

    Both array forms have shipped at different times; accepting both
    keeps older clients (PR-build CLIs, helper scripts) working while
    surfacing the shape that mirrors the response payload. Anything
    else returns HTTP 422 with a structured detail.

    ``?source=<source_type>`` scopes the rebuild to a single registered
    source (partial rebuild): only that source's local + materialized
    rows are rebuilt, and the other source's ``extract.duckdb`` is left
    untouched. Useful on dual-source deployments where a BQ refresh
    should not pay the cost of re-extracting every Keboola table.

    Returns 409 if a previously-triggered sync is still running. Two
    concurrent extractor subprocesses fight for the same `extract.duckdb`
    file lock — that contention starves uvicorn, makes `/api/health` time
    out, flips the container to `unhealthy`, and (behind a `reverse_proxy`
    upstream like the bundled Caddy overlay) bricks external traffic
    until contention drains. Fast-fail here keeps that from happening.
    """
    if body is None:
        tables: Optional[List[str]] = None
    elif isinstance(body, list):
        tables = list(body)
    elif isinstance(body, dict):
        tables = body.get("tables")
        if tables is not None and not isinstance(tables, list):
            raise HTTPException(
                status_code=422,
                detail="`tables` must be a list of strings",
            )
    else:
        raise HTTPException(
            status_code=422,
            detail=("body must be a list of table ids, an object with a `tables` list, or null"),
        )
    if tables is not None and not all(isinstance(t, str) for t in tables):
        raise HTTPException(
            status_code=422,
            detail="all entries in `tables` must be strings",
        )

    # Normalize + validate the `?source=` partial-rebuild filter. Reuse the
    # registry's canonical source-type set so an unknown value fails fast
    # with a clear 422 instead of silently rebuilding nothing.
    if source is not None:
        source = source.strip().lower()
        if not source:
            source = None
    if source is not None:
        from app.api.admin import _VALID_SOURCE_TYPES

        if source not in _VALID_SOURCE_TYPES:
            raise HTTPException(
                status_code=422,
                detail=(f"source must be one of {sorted(_VALID_SOURCE_TYPES)}, got {source!r}"),
            )

    if _sync_lock.locked():
        try:
            audit_repo().log(
                user_id=user.get("id"),
                action="sync.trigger",
                resource=((tables[0] if len(tables) == 1 else f"{len(tables)} tables") if tables else "all_tables")[
                    :256
                ],
                params={"requested_at": datetime.now(timezone.utc).isoformat(), "tables": tables, "source": source},
                result="error.in_progress",
                client_kind=client_kind_from_user(user),
            )
        except Exception:
            logger.exception("audit_log write failed for sync.trigger (in_progress); continuing")
        raise HTTPException(
            status_code=409,
            detail="sync_already_in_progress",
        )
    _t0 = time.monotonic()
    # Stamp the trigger time so `/api/sync/status` reports locked=True
    # for the next ``_TRIGGER_HOLD_SEC`` even though the background
    # task hasn't yet acquired ``_sync_lock``. Closes the race window
    # the host-side ``agnes-auto-upgrade.sh`` defer probe was hitting.
    global _recent_trigger_at
    _recent_trigger_at = _t0
    background_tasks.add_task(_run_sync, tables, source)
    try:
        audit_repo().log(
            user_id=user.get("id"),
            action="sync.trigger",
            resource=((tables[0] if len(tables) == 1 else f"{len(tables)} tables") if tables else "all_tables")[:256],
            params={"requested_at": datetime.now(timezone.utc).isoformat(), "tables": tables, "source": source},
            result="success",
            duration_ms=int((time.monotonic() - _t0) * 1000),
            client_kind=client_kind_from_user(user),
        )
    except Exception:
        logger.exception("audit_log write failed for sync.trigger; continuing")
    return {
        "status": "triggered",
        "tables": tables or "all",
        "source": source or "all",
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
    repo = sync_settings_repo()
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
    user=Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Update user's dataset sync settings.

    A dataset can only be enabled when the user has access (via
    ``resource_grants(group, "table", dataset)`` or Admin membership). The
    user_sync_settings layer is per-user preference, not authorization —
    the gate stops users from enabling sync on tables they cannot read.
    """
    from app.auth.session_principal import SessionPrincipal

    if isinstance(user, SessionPrincipal):
        raise HTTPException(403, "co_session cannot mutate user settings")
    from app.auth.access import can_access
    from app.resource_types import ResourceType

    settings_repo = sync_settings_repo()
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
    tables: dict = Field(default_factory=dict, max_length=500)  # {table_name: bool}


@router.get("/table-subscriptions")
async def get_table_subscriptions(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Get user's per-table subscription settings."""
    repo = sync_settings_repo()
    settings = repo.get_user_settings(user["id"])
    return {"user_id": user["id"], "subscriptions": settings}


@router.post("/table-subscriptions")
async def update_table_subscriptions(
    request: TableSubscriptionUpdate,
    user=Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Update per-table subscription preferences.

    Mirrors the RBAC gate in POST /settings: a table can only be subscribed
    to when the user holds a resource_grants row for it (or is Admin). This
    prevents an authenticated user from subscribing to tables they cannot read.
    """
    from app.auth.session_principal import SessionPrincipal

    if isinstance(user, SessionPrincipal):
        raise HTTPException(403, "co_session cannot mutate user settings")
    from app.auth.access import can_access
    from app.resource_types import ResourceType

    repo = sync_settings_repo()
    results = {}
    for table_name, enabled in request.tables.items():
        if not can_access(user["id"], ResourceType.TABLE.value, table_name, conn):
            results[table_name] = {"error": "no permission"}
            continue
        repo.set_dataset_enabled(user["id"], table_name, enabled)
        results[table_name] = {"enabled": enabled}
    return {"table_mode": request.table_mode, "updated": results}
