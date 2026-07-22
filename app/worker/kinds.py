"""Real job kinds for the worker runtime (wave-2B, spec §3.3 — Task 4;
``ducklake-maintenance`` added in wave-2G Task 5; ``analytics-migrate``
added in wave-2G Task 6; ``distribution-mirror`` added in wave-2H Task
WF-3 — see
``docs/superpowers/plans/2026-07-20-three-plane-wave2h-distribution.md``).

``register_all_kinds()`` registers the ten kinds the scheduler's
current HTTP-driven jobs (plus the analytics migrate command, the
distribution mirror, and the api-role write conversions) map onto:

- ``data-refresh``       (HEAVY) — wraps ``app.api.sync._run_sync``, the
  body behind ``POST /api/sync/trigger``.
- ``marketplaces-sync``  (LIGHT) — wraps ``src.marketplace.sync_marketplaces``,
  the body behind ``POST /api/marketplaces/sync-all``.
- ``session-collector``  (LIGHT) — wraps ``services.session_collector.collector.run``,
  the body behind ``POST /api/admin/run-session-collector``.
- ``corporate-memory``   (LIGHT) — wraps ``services.corporate_memory.collector.collect_all``,
  the body behind ``POST /api/admin/run-corporate-memory``.
- ``jira-refresh``       (HEAVY) — wraps ``SyncOrchestrator().rebuild_source("jira")``,
  previously called inline from the Jira webhook's incremental-transform
  path (``connectors/jira/service.py:trigger_incremental_transform``).
- ``ducklake-maintenance`` (LIGHT) — runs the POC-verified DuckLake
  maintenance sequence (merge → expire snapshots → cleanup old files →
  catalog VACUUM) on the writer session. No-ops when
  ``analytics.backend`` is not ``ducklake`` — see
  ``_run_ducklake_maintenance`` below.
- ``analytics-migrate``  (HEAVY) — wraps
  ``SyncOrchestrator().migrate_to_backend(to)``, the body behind
  ``POST /api/admin/analytics/migrate``. Admin-triggered only (no
  scheduler row) — see ``_run_analytics_migrate`` below.
- ``distribution-mirror`` (LIGHT) — mirrors every downloadable local/
  materialized parquet whose ``sync_state.hash`` differs from the
  configured object store's stamped metadata, then writes a marker index
  of what's currently mirrored. No-ops (clean, no ``boto3`` import) when
  ``src.object_store.object_store()`` returns ``None`` — signed-URL
  distribution off or no store configured — see
  ``_run_distribution_mirror`` below. Enqueued automatically after a
  successful ``data-refresh`` (see ``_maybe_enqueue_distribution_mirror``);
  no scheduler row — event-chained, not cron.
- ``analytics-rebuild``  (HEAVY) — wraps ``app.api.admin._materialize_bigquery_extract``
  (BQ extract rebuild + master views); enqueued by admin register-table /
  registry-rebuild / BQ-row-update endpoints when the process lacks the
  worker role (three-plane §3.1: api plane is analytics-write-free).
- ``collections-purge``  (HEAVY) — wraps the derived-table purge helpers in
  ``app.api.collections`` (extract.duckdb surgery + ``rebuild_source``);
  enqueued by collection/file delete endpoints when the process lacks the
  worker role. Single-box ``all`` deployments never enqueue either kind —
  the original synchronous/BackgroundTask paths are unchanged there.

Every handler below is a THIN ADAPTER — it imports and calls the existing
function/method and does not reimplement any of its logic. Each import is
deferred (inside the handler, not at module import time) for the same
reason ``app/worker/runtime.py``'s ``_jobs_repo()`` and
``_sweep_stale_scratch()`` defer theirs: this module must not carry an
import-time dependency on heavyweight subsystems (LLM clients, the
DuckDB/BigQuery extractor stack, marketplace git plumbing) that may not
be configured on every process that imports ``app.worker.kinds`` (e.g. a
test importing just the registry), and so tests can monkeypatch the
target module attribute freely without this module having already bound
a stale reference to it at import time.

Called once from ``app/main.py``'s lifespan, before the worker loop task
is created (see the comment there) — registration is idempotent
(``register_kind`` replaces any existing entry by name), so calling it
more than once (e.g. across re-imports in a test process) is harmless.

Lease/retry tuning:

- ``data-refresh`` gets the longest lease (``AGNES_DATA_REFRESH_LEASE_S``,
  default 900s / 15min) — a full Keboola extractor subprocess run +
  materialized pass + orchestrator rebuild can legitimately take that
  long on a large registry; the worker's heartbeat keeps the lease alive
  every ``lease_seconds/3`` while the handler thread runs, so this is a
  ceiling on "how long before a crashed/stuck run is reclaimed", not a
  hard timeout on the sync itself.
- ``jira-refresh`` is also HEAVY (shares the lane with ``data-refresh``,
  and both run through ``_sweep_stale_scratch()`` before every HEAVY
  claim — see ``app/worker/runtime.py``) but is a plain orchestrator
  rebuild (re-ATTACH + view creation over already-written parquet), so a
  much shorter lease (300s) is plenty.
- The LIGHT kinds (``marketplaces-sync``, ``session-collector``,
  ``corporate-memory``) default to 300s — bulk git clones / LLM catalog
  refresh / filesystem walks, but bounded by their own internal
  timeouts, not multi-minute by design.
"""

from __future__ import annotations

import logging
import os

from app.worker.registry import HEAVY_LANE, LIGHT_LANE, JobKind, register_kind

logger = logging.getLogger(__name__)

_DEFAULT_DATA_REFRESH_LEASE_S = 900
_DEFAULT_JIRA_REFRESH_LEASE_S = 300
_DEFAULT_LIGHT_LEASE_S = 300
# merge_adjacent_files/expire_snapshots/cleanup_old_files/VACUUM can each
# take a while over a large lake — same "generous ceiling, not a hard
# timeout" reasoning as _DEFAULT_DATA_REFRESH_LEASE_S (the worker's
# heartbeat keeps the lease alive every lease_seconds/3 while the handler
# thread runs).
_DEFAULT_DUCKLAKE_MAINTENANCE_LEASE_S = 900


def _data_refresh_lease_seconds() -> int:
    raw = os.environ.get("AGNES_DATA_REFRESH_LEASE_S")
    if raw is None:
        return _DEFAULT_DATA_REFRESH_LEASE_S
    try:
        return max(int(raw), 1)
    except ValueError:
        return _DEFAULT_DATA_REFRESH_LEASE_S


def _run_data_refresh(payload: dict) -> None:
    """Wrap ``app.api.sync._run_sync`` — same defaults as the HTTP trigger
    path (``tables=None`` syncs every registered table). ``payload`` may
    carry ``tables`` (list[str]) and/or ``source`` (source_type filter),
    mirroring ``POST /api/sync/trigger``'s body/`` ?source=`` params, but
    an empty payload (the scheduler's normal enqueue) behaves identically
    to the old unfiltered trigger.

    ``_run_sync`` itself acquires the module-level ``_sync_lock`` and
    fast-returns if another sync is already in flight (see its
    docstring). That fast-fail is harmless here too: the worker's HEAVY
    lane already runs at concurrency 1, so within a single worker process
    two ``data-refresh`` jobs can never be mid-handler simultaneously.
    ``_sync_lock`` is a plain ``threading.Lock`` — it is invisible across
    processes, so it does NOT guard against the legacy HTTP trigger path
    running in a separate ``api`` process (or a second worker process)
    racing this one. Cross-process serialization of the actual rebuild
    critical section is handled independently, inside
    ``SyncOrchestrator.rebuild()``/``rebuild_source()``, via
    ``src.db_pg.rebuild_lease()`` (a Postgres advisory lock; no-op on the
    DuckDB backend, where a single-process startup guard already applies).

    Job-outcome honesty (wave-2B review carry-over, W2B-4/7): ``_run_sync``
    used to swallow every failure internally (log + best-effort webhook
    notify) and return nothing, so a ``data-refresh`` job always finalized
    ``'done'`` even when the underlying sync failed outright or partially
    — ``GET /api/jobs/{id}`` had no way to show it, and the job's
    retry-on-failure semantics (``retry_in_seconds=300`` below) never
    engaged. ``_run_sync`` now returns ``True`` (clean run), ``False``
    (fatal exception or any per-table failure), or ``None`` (this call
    was a no-op — another same-process invocation already held
    ``_sync_lock``, not a failure of this job). Only ``False`` raises —
    the worker's lane-slot handler (``app/worker/runtime.py``) turns an
    uncaught exception into ``jobs_repo().fail(..., retry_in_seconds=...)``,
    so this is the sole mechanism needed for the job to record `failed`
    and retry.
    """
    from app.api.sync import _run_sync

    ok = _run_sync(payload.get("tables"), payload.get("source"))
    if ok is False:
        raise RuntimeError("data-refresh sync failed — see server logs and sync_state for per-table errors")
    if ok:
        # `ok is True` here (the `False` branch above already raised) — a
        # real sync just completed in THIS call, so the extracts tree is
        # settled and safe to mirror. `ok is None` (another same-process
        # `_run_sync` held the lock) is deliberately excluded: that means a
        # sync may still be in flight elsewhere, and mirroring now could
        # read a half-written parquet.
        _maybe_enqueue_distribution_mirror()


def _run_analytics_rebuild(payload: dict) -> None:
    """BQ extract + master-view rebuild, enqueued by api-role admin endpoints.

    Three-plane §3.1: the api plane must not write analytics in-process. On a
    role-split deployment `POST /api/admin/register-table` / `/registry/rebuild`
    (and BQ-row updates) enqueue this kind instead of running the rebuild in a
    FastAPI BackgroundTask; single-box ``all`` deployments keep the original
    synchronous/BackgroundTask path and never enqueue it. Payload is empty —
    the rebuild is registry-wide by design (same body as the BackgroundTask
    wrapper it replaces).

    Lazy import from ``app.api.admin`` mirrors ``_run_data_refresh``'s import
    of ``app.api.sync._run_sync`` — the handler bodies live next to their HTTP
    siblings so the two invocation paths can't drift.
    """
    from app.api.admin import _materialize_bigquery_extract

    result = _materialize_bigquery_extract() or {}
    errors = result.get("errors") or []
    if errors:
        raise RuntimeError(f"analytics-rebuild surfaced {len(errors)} error(s); first: {errors[:3]}")


def _run_collections_purge(payload: dict) -> None:
    """Derived-table purge for a deleted collection/file (extract.duckdb
    surgery + ``rebuild_source``), enqueued by api-role collection deletes.

    ``payload``: ``corpus_id`` (required), ``file_id`` (optional — present for
    a single-file delete, absent for a whole-collection delete). Same §3.1
    rationale and single-box behavior as ``_run_analytics_rebuild``.
    """
    from app.api.collections import (
        _purge_derived_tabular_row_for_file,
        _purge_derived_tabular_rows,
    )

    corpus_id = payload["corpus_id"]
    file_id = payload.get("file_id")
    if file_id:
        _purge_derived_tabular_row_for_file(corpus_id, file_id)
    else:
        _purge_derived_tabular_rows(corpus_id)


def _maybe_enqueue_distribution_mirror() -> None:
    """Enqueue a ``distribution-mirror`` job after a successful
    ``data-refresh`` (wave-2H WF-3) — but only when signed-URL distribution
    is actually configured. Legacy/no-store instances must never accumulate
    ``distribution-mirror`` rows in the ``jobs`` table for nothing; checking
    ``object_store()`` here (not just relying on the handler's own no-op
    guard) keeps the queue clean on every sync for the common case.

    Mirrors the Jira webhook's enqueue-and-log-on-failure shape
    (``connectors/jira/service.py::trigger_incremental_transform``):
    best-effort, a failure to enqueue must never fail the ``data-refresh``
    job that already succeeded.
    """
    from src.object_store import object_store

    if object_store() is None:
        return
    try:
        from src.repositories import jobs_repo

        jobs_repo().enqueue("distribution-mirror", {}, idempotency_key="distribution-mirror")
    except Exception:
        logger.warning("distribution-mirror: failed to enqueue follow-up job", exc_info=True)


def _run_marketplaces_sync(payload: dict) -> None:
    """Wrap ``src.marketplace.sync_marketplaces`` — the body behind
    ``POST /api/marketplaces/sync-all``. No payload fields are consumed;
    it always syncs every registered (non-builtin) marketplace, same as
    the HTTP endpoint."""
    from src.marketplace import sync_marketplaces

    sync_marketplaces()


def _run_session_collector(payload: dict) -> None:
    """Wrap ``services.session_collector.collector.run`` — the body
    behind ``POST /api/admin/run-session-collector``. Called with the
    same ``dry_run=False, verbose=False`` defaults as that endpoint."""
    from services.session_collector import collector

    collector.run(dry_run=False, verbose=False)


def _run_corporate_memory(payload: dict) -> None:
    """Wrap ``services.corporate_memory.collector.collect_all`` — the
    body behind ``POST /api/admin/run-corporate-memory``. Called with
    the same ``dry_run=False`` default as that endpoint."""
    from services.corporate_memory.collector import collect_all

    collect_all(dry_run=False)


def _run_jira_refresh(payload: dict) -> None:
    """Wrap ``SyncOrchestrator().rebuild_source("jira")`` — previously
    called inline from ``connectors/jira/service.py``'s
    ``trigger_incremental_transform`` after every webhook-driven
    incremental parquet transform. Now enqueued instead (see that
    module), deduped via the ``"jira-refresh"`` idempotency key so a
    burst of webhook events collapses into a single rebuild."""
    from src.orchestrator import SyncOrchestrator

    SyncOrchestrator().rebuild_source("jira")


def _ducklake_expire_older_than_sql(retention_days: int) -> str:
    """Build the ``older_than => ...`` argument for ``ducklake_expire_snapshots``,
    enforcing :func:`src.analytics_backend.ducklake_min_retention_floor_seconds`
    as an absolute safety floor.

    ``ducklake_snapshot_retention_days()`` deliberately allows ``0`` ("no
    retention grace" — see its docstring), but ``0`` with no further
    guardrail would let this job expire a snapshot a live analyst query is
    still reading from: there is no hard statement timeout on local
    DuckLake queries (nothing in this codebase caps how long
    ``agnes query`` / ``/api/query`` can run), so a long-running query
    holding a reference to "the current snapshot at the time it started"
    must not have that snapshot pulled out from under it mid-query.

    ``retention_days * 86400`` is compared against the floor in seconds;
    whenever the configured retention is below the floor (in practice only
    ``retention_days == 0``, since any ``retention_days >= 1`` is already
    ``86400s >= `` the 3600s default floor), the clamped floor value is
    used instead — expressed in seconds (not days) so the clamp doesn't
    round down to zero days again. A warning is logged so an operator who
    intentionally configured aggressive reclamation knows why the actual
    cutoff differs from what they set.
    """
    from src.analytics_backend import ducklake_min_retention_floor_seconds

    floor_seconds = ducklake_min_retention_floor_seconds()
    retention_seconds = retention_days * 86400
    if retention_seconds < floor_seconds:
        logger.warning(
            "ducklake-maintenance: configured snapshot_retention_days=%d (%ds) is below the "
            "%ds safety floor (max plausible in-flight analytic query duration + margin) — "
            "clamping older_than to now() - %ds so an active reader's held snapshot is never "
            "expired out from under it",
            retention_days,
            retention_seconds,
            floor_seconds,
            floor_seconds,
        )
        return f"now() - INTERVAL '{floor_seconds} seconds'"
    # retention_days is always a non-negative int (validated by
    # ducklake_snapshot_retention_days()) — safe to interpolate directly
    # into the INTERVAL literal.
    return f"now() - INTERVAL '{retention_days} days'"


def _run_ducklake_maintenance(payload: dict) -> None:
    """Run the POC-verified DuckLake maintenance sequence on the writer
    session, in order:

    1. ``CALL lake.merge_adjacent_files()`` — compacts small adjacent
       Parquet files written by successive copy-ingest rebuilds.
    2. ``CALL ducklake_expire_snapshots('lake', older_than => now() -
       INTERVAL '<N> days')`` — drops catalog snapshots older than the
       configured retention window (``src.analytics_backend
       .ducklake_snapshot_retention_days()``, default 7 days; floored by
       :func:`_ducklake_expire_older_than_sql`), freeing the files that
       only they referenced for step 3 to reclaim.
    3. ``CALL ducklake_cleanup_old_files('lake', cleanup_all => true)`` —
       physically deletes data files no longer referenced by any
       remaining snapshot.
    4. Catalog ``VACUUM`` (``src.ducklake_session.vacuum_ducklake_catalog``)
       — Postgres-catalog only; a no-op (logged, not an error) on a
       DuckDB-file catalog, which has no equivalent storage-compaction
       VACUUM.

    Every CALL signature here was verified directly against the real
    ``ducklake`` extension (DuckDB 1.5.2) before being written — see the
    task 5 report for the scratch session that exercised each one
    (snapshot count dropping from N to 1 after
    ``ducklake_expire_snapshots`` + ``ducklake_cleanup_old_files``, and a
    direct ``psycopg`` ``VACUUM`` against a live pgserver-backed catalog).

    **No-op on the legacy backend.** A ``ducklake-maintenance`` job can
    only ever be enqueued by this instance's own scheduler row (daily,
    see ``services/scheduler/__main__.py::build_jobs``), but the backend
    could have been flipped back to ``legacy`` between the job being
    queued and a worker claiming it (or a stray manual enqueue via
    ``POST /api/jobs`` on a legacy instance) — checking here, not just
    trusting the scheduler's own gate, makes a stray/stale enqueue
    harmless instead of raising ``ducklake`` extension errors against a
    backend that was never attached.

    **Mutual exclusion with rebuild (wave-2G Task 5 review carry-over,
    finding 1-concurrency).** ``ducklake-maintenance`` (LIGHT lane) and
    ``SyncOrchestrator.rebuild()``/``rebuild_source()`` (HEAVY lane, via
    ``data-refresh``/``jira-refresh``) both write the lake through the
    same ``get_ducklake_write()`` singleton, and both lanes run in the
    same worker process on independent OS threads (see
    ``app/worker/runtime.py``) — so a long rebuild running past this job's
    schedule could otherwise race a catalog-wide expire/cleanup pass
    against an in-progress per-table ``CREATE OR REPLACE TABLE``. Wrapping
    the whole write section in ``src.orchestrator.rebuild_mutex()`` — the
    identical in-process lock + cross-process Postgres advisory lease pair
    ``rebuild()``/``rebuild_source()`` already take, in the same order —
    makes maintenance and rebuild mutually exclusive without introducing a
    second lock-acquisition order (which would risk deadlock).
    """
    from src.analytics_backend import analytics_backend, ducklake_snapshot_retention_days

    if analytics_backend() != "ducklake":
        logger.info("ducklake-maintenance: analytics.backend is not 'ducklake' — no-op")
        return

    from src.ducklake_session import get_ducklake_write, vacuum_ducklake_catalog
    from src.orchestrator import rebuild_mutex

    retention_days = ducklake_snapshot_retention_days()
    older_than_sql = _ducklake_expire_older_than_sql(retention_days)

    with rebuild_mutex():
        conn = get_ducklake_write()
        try:
            conn.execute("CALL lake.merge_adjacent_files()")
            conn.execute(f"CALL ducklake_expire_snapshots('lake', older_than => {older_than_sql})")
            conn.execute("CALL ducklake_cleanup_old_files('lake', cleanup_all => true)")
        finally:
            conn.close()

        vacuumed = vacuum_ducklake_catalog()

    logger.info(
        "ducklake-maintenance: merge/expire(retention=%dd, older_than=%s)/cleanup done; catalog VACUUM %s",
        retention_days,
        older_than_sql,
        "ran" if vacuumed else "skipped (file catalog)",
    )


def _run_analytics_migrate(payload: dict) -> None:
    """Wrap ``SyncOrchestrator().migrate_to_backend(to)`` — the body
    behind ``POST /api/admin/analytics/migrate`` (wave-2G Task 6).

    ``payload["to"]`` is ``"ducklake"`` or ``"legacy"``, already validated
    by the endpoint before enqueueing (an unknown value re-raises via
    ``migrate_to_backend``'s own ``ValueError``, which the worker turns
    into a failed job the same way any other handler exception does).
    Unlike ``data-refresh``/``jira-refresh``, this rebuilds into the
    EXPLICITLY named target backend regardless of the currently
    configured ``analytics.backend`` — see ``migrate_to_backend``'s
    docstring for why that distinction matters (config is boot-time
    cached, not hot-reloaded)."""
    from src.orchestrator import SyncOrchestrator

    SyncOrchestrator().migrate_to_backend(payload.get("to"))


def _run_distribution_mirror(payload: dict) -> None:
    """Mirror every downloadable local/materialized parquet to the
    configured object store, then write the marker index of what's
    currently mirrored (wave 2-H, WF-3 — see
    ``docs/superpowers/plans/2026-07-20-three-plane-wave2h-distribution.md``).

    **Clean no-op, no ``boto3`` import** when
    ``src.object_store.object_store()`` returns ``None`` (signed-URL
    distribution off, or no store configured) — the common case for
    every S/M-tier instance and any instance that never installed the
    ``distribution`` extra. This check happens before any other import in
    this function, so a legacy instance never even imports ``boto3``.

    Enumerates the same download set ``agnes pull`` computes
    (``cli/lib/pull.py``): ``sync_state`` rows whose registry
    ``query_mode`` is ``local`` or ``materialized``, excluding
    ``server_only`` rows (kept fresh server-side, never distributed as a
    parquet) — joined by ``table_registry.name`` the same way
    ``app/api/sync.py::_build_manifest_for_user`` does (``sync_state.table_id``
    is sourced from ``_meta.table_name``, which equals registry ``name``,
    not ``id``).

    The md5 compared/stamped is ``sync_state.hash`` — the SAME hash the
    manifest exposes to ``agnes pull`` (computed once, in
    ``src.orchestrator._update_sync_state`` / the materialized-pass
    equivalent) — never recomputed here, so the marker index and the
    manifest never disagree about "is this the current content".

    Idempotent: a table whose object already carries the current md5
    (``head_md5(key) == current_md5``) is skipped, not re-uploaded. Per-file
    failures (network blip, permissions) are logged and do not abort the
    run — a partial mirror is safe, since the marker index below only
    lists tables that ARE currently mirrored; WF-2's manifest presign reads
    that index and simply omits ``signed_url`` for anything not in it, so
    the client falls back to the app-served download path.
    """
    from src.object_store import object_store

    store = object_store()
    if store is None:
        logger.info("distribution mirror: no object store configured, skipping")
        return

    from app.utils import resolve_local_parquet
    from src.distribution import write_mirror_index
    from src.repositories import sync_state_repo, table_registry_repo

    registry_by_name = {t["name"]: t for t in table_registry_repo().list_all()}

    uploaded = 0
    skipped = 0
    failed = 0
    mirrored: dict[str, str] = {}

    for state in sync_state_repo().get_all_states():
        table_id = state["table_id"]
        reg = registry_by_name.get(table_id, {})
        query_mode = reg.get("query_mode") or "local"
        if query_mode not in ("local", "materialized"):
            continue
        if reg.get("server_only"):
            continue
        current_md5 = state.get("hash") or ""
        if not current_md5:
            # Never successfully synced yet — nothing on disk to mirror.
            continue
        parquet_path = resolve_local_parquet(table_id, reg.get("source_type"))
        if parquet_path is None:
            logger.warning("distribution mirror: no on-disk parquet found for %s, skipping", table_id)
            continue

        key = f"{table_id}.parquet"
        try:
            existing_md5 = store.head_md5(key)
        except Exception:
            logger.exception("distribution mirror: head_md5 failed for %s", table_id)
            failed += 1
            continue

        if existing_md5 == current_md5:
            skipped += 1
            mirrored[table_id] = current_md5
            continue

        try:
            store.put_file(parquet_path, key, md5=current_md5)
        except Exception:
            logger.exception("distribution mirror: upload failed for %s", table_id)
            failed += 1
            continue

        uploaded += 1
        mirrored[table_id] = current_md5

    write_mirror_index(store, mirrored)

    logger.info(
        "distribution mirror: uploaded=%d skipped=%d failed=%d mirrored_total=%d",
        uploaded,
        skipped,
        failed,
        len(mirrored),
    )


def register_all_kinds() -> None:
    """Register the ten real job kinds. Idempotent — safe to call more
    than once (e.g. across test re-imports); ``register_kind`` replaces
    any existing entry of the same name rather than erroring."""
    register_kind(
        JobKind(
            name="data-refresh",
            handler=_run_data_refresh,
            lane=HEAVY_LANE,
            lease_seconds=_data_refresh_lease_seconds(),
            retry_in_seconds=300,
        )
    )
    register_kind(
        JobKind(
            name="marketplaces-sync",
            handler=_run_marketplaces_sync,
            lane=LIGHT_LANE,
            lease_seconds=_DEFAULT_LIGHT_LEASE_S,
            retry_in_seconds=300,
        )
    )
    register_kind(
        JobKind(
            name="session-collector",
            handler=_run_session_collector,
            lane=LIGHT_LANE,
            lease_seconds=_DEFAULT_LIGHT_LEASE_S,
            retry_in_seconds=300,
        )
    )
    register_kind(
        JobKind(
            name="corporate-memory",
            handler=_run_corporate_memory,
            lane=LIGHT_LANE,
            lease_seconds=_DEFAULT_LIGHT_LEASE_S,
            retry_in_seconds=300,
        )
    )
    register_kind(
        JobKind(
            name="jira-refresh",
            handler=_run_jira_refresh,
            lane=HEAVY_LANE,
            lease_seconds=_DEFAULT_JIRA_REFRESH_LEASE_S,
            retry_in_seconds=300,
        )
    )
    register_kind(
        JobKind(
            name="ducklake-maintenance",
            handler=_run_ducklake_maintenance,
            lane=LIGHT_LANE,
            lease_seconds=_DEFAULT_DUCKLAKE_MAINTENANCE_LEASE_S,
            retry_in_seconds=300,
        )
    )
    register_kind(
        JobKind(
            name="analytics-migrate",
            handler=_run_analytics_migrate,
            lane=HEAVY_LANE,
            # Same cost class as data-refresh (a full extracts-tree rebuild,
            # just into a different target backend) — reuse the same
            # generous lease default/override knob rather than inventing a
            # second one.
            lease_seconds=_data_refresh_lease_seconds(),
            retry_in_seconds=300,
        )
    )
    register_kind(
        JobKind(
            name="distribution-mirror",
            handler=_run_distribution_mirror,
            lane=LIGHT_LANE,
            # Same LIGHT-lane default as marketplaces-sync/session-collector/
            # corporate-memory: bounded by the number of tables + their
            # individual upload times, not multi-minute by design. No
            # dedicated env override — a mirror run is bounded work, unlike
            # the ducklake catalog operations that justified their own knob.
            lease_seconds=_DEFAULT_LIGHT_LEASE_S,
            retry_in_seconds=300,
        )
    )
    register_kind(
        JobKind(
            name="analytics-rebuild",
            handler=_run_analytics_rebuild,
            lane=HEAVY_LANE,
            # Same cost class as data-refresh (BQ extract rebuild + master
            # views) — reuse its lease knob.
            lease_seconds=_data_refresh_lease_seconds(),
            retry_in_seconds=300,
        )
    )
    register_kind(
        JobKind(
            name="collections-purge",
            handler=_run_collections_purge,
            lane=HEAVY_LANE,
            # extract.duckdb surgery + rebuild_source — same serialization
            # class as the other analytics writers, so HEAVY (concurrency 1).
            lease_seconds=_DEFAULT_LIGHT_LEASE_S,
            retry_in_seconds=300,
        )
    )
