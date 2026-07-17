"""Real job kinds for the worker runtime (wave-2B, spec §3.3 — Task 4).

``register_all_kinds()`` registers the five kinds the scheduler's
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

import os

from app.worker.registry import HEAVY_LANE, LIGHT_LANE, JobKind, register_kind

_DEFAULT_DATA_REFRESH_LEASE_S = 900
_DEFAULT_JIRA_REFRESH_LEASE_S = 300
_DEFAULT_LIGHT_LEASE_S = 300


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
    """
    from app.api.sync import _run_sync

    _run_sync(payload.get("tables"), payload.get("source"))


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


def register_all_kinds() -> None:
    """Register the five real job kinds. Idempotent — safe to call more
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
