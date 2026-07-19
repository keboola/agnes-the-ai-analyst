"""Real job kinds for the worker runtime (wave-2B, spec §3.3 — Task 4;
``ducklake-maintenance`` added in wave-2G Task 5).

``register_all_kinds()`` registers the six kinds the scheduler's
current HTTP-driven jobs map onto:

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


def register_all_kinds() -> None:
    """Register the six real job kinds. Idempotent — safe to call more
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
