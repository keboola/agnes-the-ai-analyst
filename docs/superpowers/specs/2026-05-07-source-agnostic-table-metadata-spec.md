# Source-Agnostic Table Metadata for `agnes catalog`

> **Status:** spec / design. Convert to an implementation plan in `docs/superpowers/plans/` once reviewed. Closes #155 + #156.

**Goal:** Surface cost-relevant metadata for *every* registered table — regardless of `source_type` or `query_mode` — through `agnes catalog` and `agnes describe`. Right now the catalog response sets `rough_size_hint = None` for any non-local row, which means the analyst Claude has no guard against issuing a remote query against a 200-GB table. Plus give admins one canonical doc that explains when to register a table in each mode (BigQuery and Keboola today, future connectors tomorrow) so the option doesn't go undiscovered.

**Why now:** the v0.45.0 easy-wins bundle left analyst-side cost discipline in good shape (BQ rewriter + cap-guard + `--remote` for views), and the v0.44.x bootstrap rework consolidated the analyst entrypoint on `agnes catalog` JSON. The remaining gap is on the *server*: catalog rows for remote tables still ship without size info, and there's no single connector-agnostic seam to add it. Issues #155 and #156 were filed against an older `data_description.md` / `schema.json` artifact pair that no longer exists; the same demand surfaces today against `agnes catalog`.

**Non-goals:**

- Profiling / column histograms for remote tables. That's a separate, much bigger piece of work (the original #155 third bullet) — `src/profiler.py` runs against a local parquet today, and lifting it to read from BigQuery is its own design conversation.
- Dimension cardinality / `query_result_estimates`. Same reason — needs a profiler redesign.
- Onboarding nudge ("hey, you have N tables, consider registering BQ remote ones"). Worth doing, but a separate UX call (admin dashboard empty-state, `agnes init` summary, or both) — out of scope here.
- Generalising beyond BigQuery + Keboola. Jira / future connectors get a stub provider that returns `None`; not a polished surface yet.

---

## What already exists

The pieces are 80% in place; this spec wires them up cleanly.

### Catalog response (`/api/v2/catalog`)

`app/api/v2_catalog.py:_materialized_size_hint` already sizes any table whose data is on the server's local filesystem (the `local` and `materialized` modes). For `remote`, it explicitly returns `None` with a TODO comment: *"size requires a BQ INFORMATION_SCHEMA round-trip; tracked separately"*. That's the gap.

The function is also misnamed — it sizes more than just materialized rows. Will rename to `_size_hint_for_row` when restructuring.

### Schema endpoint (`/api/v2/schema/{id}`)

`app/api/v2_schema.py:_fetch_bq_table_options` (lines 85-140) already does a BQ INFORMATION_SCHEMA round-trip for partition + cluster info on a single table. The relevant body:

```python
# v2_schema.py:115-126 — DO NOT diverge from this shape; it's the template.
with bq.duckdb_session() as conn:
    bq_sql = (
        f"SELECT column_name, is_partitioning_column, clustering_ordinal_position "
        f"FROM `{bq.projects.data}.{dataset}.INFORMATION_SCHEMA.COLUMNS` "
        f"WHERE table_name = ? "
        f"ORDER BY clustering_ordinal_position NULLS LAST"
    )
    rows = conn.execute(
        "SELECT * FROM bigquery_query(?, ?, ?)",
        [bq.projects.billing, bq_sql, table],
    ).fetchall()
```

Returns `{"partition_by": str | None, "clustered_by": list[str]}` or `{}`. Best-effort: errors degrade to empty dict, schema endpoint stays 200. The **load-bearing patterns** the new providers MUST mirror:

1. **Sentinel-config early-return** — `if not bq.projects.data: return {}` on line 107, before any query construction. Keeps a Keboola-only deployment from blowing up on the first catalog call. Reasoning at `v2_schema.py:103-108`.
2. **`validate_quoted_identifier` discipline** — every interpolated identifier (`bq.projects.data`, `dataset`, `table`) goes through `src.identifier_validation.validate_quoted_identifier` before f-stringing into the SQL (lines 110-113). Refuses unsafe identifiers by returning `{}`.
3. **Positional `?` placeholders only** — `bigquery_query(?, ?, ?)` with 3 positional args: `[billing_project, inner_sql, *predicate_params]`. Inner BQ SQL uses `?` for predicates. **No `@named`-parameter syntax** — every existing call site (`extractor.py:204`, `v2_sample.py:52`, `v2_schema.py:124`) uses positional `?`; the BQ extension's named-param path is unverified in this codebase.
4. **`try/except Exception → return {}` outer guard** — load-bearing per the function docstring (lines 93-99). The /schema endpoint must keep returning 200. Same applies to providers — never escalate to the catalog endpoint.

This pattern is the prior-art template the new `metadata.py` providers replicate.

### Sample endpoint (`/api/v2/sample/{id}`)

`app/api/v2_sample.py` has a `bigquery` branch (line 86) that uses `bigquery_query` to fetch sample rows for remote BQ tables. **Already works**, in other words. Will verify with a smoke test in the implementation plan; if it works, no code change. (Issue #155's "agnes describe doesn't work on remote" claim is from May 1 — predates the rewriter / sample-endpoint work.)

### Keboola Storage API wrapper

`connectors/keboola/storage_api.py:KeboolaStorageClient` (landed in #190 today) exposes a generic `_get(path)` against `/v2/storage`. The Storage API's `GET /v2/storage/tables/{table_id}` returns `{rowsCount, dataSizeBytes, columns, primaryKey, ...}` — everything we need for a Keboola provider, no new HTTP plumbing required.

Keboola tables are universally `query_mode='local'` in current deployments (a sync downloads the parquet), so the Keboola provider is mostly forward-looking. But the `_remote_attach` mechanism (`keboola.bucket.table` paths via the Keboola DuckDB extension) is architecturally supported and the docs page must reflect that.

### BigQuery access

`connectors/bigquery/access.py:get_bq_access()` returns a `BqAccess` with `duckdb_session()` — a DuckDB conn with the BigQuery extension preloaded. Same path `v2_schema._fetch_bq_table_options` already uses for INFORMATION_SCHEMA.

### Caching infrastructure

`app/api/v2_cache.py:TTLCache` is the existing TTL cache, already used by v2_catalog (`_table_rows_cache`, 5-min TTL). The new metadata cache plugs into the same primitive.

---

## Design

### Provider pattern (source-agnostic seam)

```
connectors/
  bigquery/
    metadata.py      # NEW — INFORMATION_SCHEMA round-trip for BQ rows
  keboola/
    metadata.py      # NEW — GET /v2/storage/tables/{id} via storage_api
  jira/
    # no metadata.py — Jira tables are always query_mode='local',
    # parquet stat path covers them.
```

Each provider exposes a single function. The contract is **narrow**: callers pass only the values the provider needs, never the whole registry row. This both stops the provider from accidentally reading fields the catalog doesn't intend it to, and gives the dispatcher one place to validate identifiers before calling.

```python
# app/api/_metadata_models.py — new shared module

from dataclasses import dataclass

@dataclass(frozen=True)
class MetadataRequest:
    """Narrow input — the fields a metadata provider actually needs.

    `bucket` and `source_table` are pre-validated by the dispatcher
    (`validate_quoted_identifier`) before construction; the provider
    can interpolate them into SQL/URL paths without re-checking.
    """
    table_id: str
    bucket: str
    source_table: str

@dataclass
class TableMetadata:
    """Source-agnostic metadata bundle. Every field optional — providers
    fill what they can cheaply get, callers tolerate Nones."""

    rows: int | None = None
    size_bytes: int | None = None
    partition_by: str | None = None
    clustered_by: list[str] | None = None
    # Forward slots — populated when the provider grows. New fields here
    # are non-breaking on existing CLI consumers (which today don't even
    # render `rough_size_hint` — `grep -rn rough_size_hint cli/` is empty,
    # confirming the additive-field claim).
```

```python
# connectors/<source>/metadata.py

def fetch(req: MetadataRequest) -> TableMetadata | None:
    """Return metadata for a registered table. None on any failure
    (network, permissions, sentinel-unconfigured connector); the caller
    falls back to rough estimates or omits the field. Never raises."""
```

Dispatch from `app/api/v2_catalog.py` via a small registry:

```python
# app/api/v2_catalog.py (new helpers)

from src.identifier_validation import validate_quoted_identifier

def _metadata_provider_for(source_type: str):
    """Lazy import — connector modules are heavy (import duckdb extensions,
    google-cloud client, etc.). Loading them at request time keeps a
    keboola-only deployment from paying the BQ import cost.
    """
    if source_type == "bigquery":
        from connectors.bigquery import metadata as m
        return m.fetch
    if source_type == "keboola":
        from connectors.keboola import metadata as m
        return m.fetch
    return None  # jira et al — no remote provider, fall through to parquet stat


def _build_metadata_request(row: dict) -> MetadataRequest | None:
    """Construct a validated MetadataRequest from a registry row. Returns
    None when the row's identifiers don't pass validation — provider is
    not dispatched. Mirrors the gate in v2_schema._fetch_bq_table_options:113."""
    bucket = row.get("bucket") or ""
    source_table = row.get("source_table") or row["id"]
    if not (validate_quoted_identifier(bucket, "bucket")
            and validate_quoted_identifier(source_table, "source_table")):
        return None
    return MetadataRequest(
        table_id=row["id"], bucket=bucket, source_table=source_table,
    )
```

The dispatch table is **two lines per connector**. Adding a future source (e.g. Snowflake) is a one-line registration plus a new `metadata.py`. Pre-validation means **identifier-injection guards live in one place** rather than being duplicated per provider.

### When to call the provider

`_size_hint_for_row(row)` (renamed from `_materialized_size_hint` — the rename is itself a fix; the existing function already handles `local` and `materialized`, the "materialized" in the name was misleading) becomes:

1. If `query_mode in {"local", "materialized"}` → existing parquet-stat path on the data volume. Cheap.
2. If `query_mode == "remote"` → call `_build_metadata_request(row)` (validates identifiers, returns None on bad shape) → dispatch to the provider → cache result by `(source_type, table_id)` for 15 minutes.
3. Provider returns `None` or fails → return `None`, no escalation. The catalog response stays 200; the analyst Claude reads `null` and treats the size as unknown per existing CLAUDE.md guidance.

The 15-minute TTL is a deliberate compromise:

| TTL | Pro | Con |
|---|---|---|
| Per-request (no cache) | Always fresh | One INFORMATION_SCHEMA query per visible table per `agnes catalog` call. With 50 tables and 10 analysts hitting the dashboard, BQ quota burn adds up. |
| 5 min (matches `_table_rows_cache`) | Already a configurable knob | Too short for a metric that barely changes hour-to-hour. |
| **15 min** | Fresh enough for an analyst session, low enough that newly-registered tables show metrics within one coffee break | Slight lag for operators verifying registration. Mitigated by the unified cache-bust below. |
| 1 hour | Less BQ traffic | Operators verifying `--query-mode remote` registration would see "unknown size" for too long. |

**Negative-cache: NO.** Don't store a sentinel for failed lookups. The previous spec proposed a 60-second negative-cache TTL; reviewer correctly flagged the asymmetry as adding complexity without paying for itself. A failed BQ INFORMATION_SCHEMA call is cheap (one round-trip, metadata-only); a failed Keboola Storage API call is one HTTP GET. Worth re-trying on the next catalog request rather than building a parallel TTL system. If telemetry later shows a hot-loop (e.g. an instance permanently misconfigured but with admin watching the dashboard), revisit — until then, no negative cache.

### Unified cache invalidation

The previous spec proposed `_invalidate_metadata_cache(table_id)` on register/update. **That alone is insufficient.** Verified state on current main:

| Cache | TTL | Cleared on registry change today? |
|---|---|---|
| `_table_rows_cache` (`v2_catalog.py:25`) | 300 s | ❌ no |
| `_schema_cache` (`v2_schema.py:17`) | 3600 s (1 h) | ❌ no |
| `_sample_cache` (`v2_sample.py:17`) | 3600 s (1 h) | ❌ no |

`admin.py:1037,1110,2771` (the registry write paths) call only `app.instance_config.reset_cache()`. None of the four catalog/schema/sample/metadata caches are touched. The user-visible failures of this gap:

- Admin registers a remote table → `agnes catalog` doesn't show the new row for up to 5 minutes.
- Admin updates a row's `bucket` → `agnes schema <id>` returns the OLD column list for up to 1 hour.
- Admin unregisters a table → `agnes describe <id>` keeps returning the OLD sample rows for up to 1 hour.

Fix in this PR by introducing a single helper that owns all four caches:

```python
# app/api/v2_catalog.py (addition)

def invalidate_for_table(table_id: str) -> None:
    """Drop every per-table cache so the next /api/v2/* request reflects
    the just-registered / updated / unregistered row immediately. Owned by
    the catalog module so admin.py doesn't need to know which caches exist.

    Imports v2_schema and v2_sample lazily — keeps catalog tests from
    pulling in BQ-extension imports they don't need.
    """
    from app.api import v2_schema, v2_sample

    _table_rows_cache.clear()  # whole-list cache; no per-row precision
    _metadata_cache.invalidate(table_id)
    v2_schema._schema_cache.invalidate(table_id)
    # Sample cache key is `f"{table_id}|{n}"`; clearing the whole sample
    # cache is heavier than precise invalidation, but registry-change
    # frequency (handful per day on a typical instance) doesn't justify
    # adding a prefix-invalidation primitive to TTLCache. Acceptable.
    v2_sample._sample_cache.clear()
```

Wire it into `app/api/admin.py`:

- `POST /api/admin/register-table` — call after the registry write succeeds, before returning.
- `PUT /api/admin/registry/{id}` — call after the row update.
- `DELETE /api/admin/registry/{id}` — call after unregister (otherwise an unregistered row keeps appearing in `agnes catalog` and serving stale schema for up to 1 hour; same UX bug, opposite direction).

Three call sites, one shared helper. Keeps cache knowledge in `v2_catalog.py` and out of `admin.py`. The TTL values themselves are unchanged (1 h is fine when staleness is bounded by an explicit flush).

### BQ COLUMNS query consolidation

`v2_schema.py:_fetch_bq_schema` and `v2_schema.py:_fetch_bq_table_options` both query the same `INFORMATION_SCHEMA.COLUMNS` view with the same `WHERE table_name = ?` predicate; only the SELECT list differs. On a `_schema_cache` miss, that's **two BQ jobs back-to-back** for one logical request — wasteful on on-demand pricing where every job is billed.

Consolidate into a single helper that returns one resultset; both consumers (the v2_schema endpoint AND the new BQ metadata provider's `_fetch_partition_cluster` path) call it:

```python
# connectors/bigquery/access.py (or a sibling module — see below)

def fetch_bq_columns_full(
    bq: BqAccess, dataset: str, table: str,
) -> list[dict] | None:
    """Single round-trip to INFORMATION_SCHEMA.COLUMNS pulling everything
    both v2_schema and the metadata provider need. Returns one dict per
    column; consumers project the fields they care about.

    Best-effort: returns None on any failure. Sentinel-config early-return
    on `not bq.projects.data`. Mirrors the validation discipline of the
    individual functions it replaces.
    """
    if not bq.projects.data:
        return None

    if not (validate_quoted_identifier(bq.projects.data, "BQ project")
            and validate_quoted_identifier(dataset, "BQ dataset")
            and validate_quoted_identifier(table, "BQ source_table")):
        return None

    bq_sql = (
        f"SELECT column_name, data_type, is_nullable, "
        f"       is_partitioning_column, clustering_ordinal_position "
        f"FROM `{bq.projects.data}.{dataset}.INFORMATION_SCHEMA.COLUMNS` "
        f"WHERE table_name = ? ORDER BY ordinal_position"
    )
    try:
        with bq.duckdb_session() as conn:
            rows = conn.execute(
                "SELECT * FROM bigquery_query(?, ?, ?)",
                [bq.projects.billing, bq_sql, table],
            ).fetchall()
    except Exception as e:
        logger.warning(
            "BQ COLUMNS fetch failed for %s.%s.%s: %s",
            bq.projects.data, dataset, table, e,
        )
        return None

    return [
        {
            "name": r[0],
            "type": r[1],
            "nullable": (r[2] or "").upper() == "YES",
            "is_partitioning_column": (r[3] or "").upper() == "YES",
            "clustering_ordinal_position": r[4],
        }
        for r in rows
    ]
```

Touchpoints on existing code:

- **`v2_schema.py:_fetch_bq_schema`** — replaced by `[{"name", "type", "nullable", "description":""} for c in fetch_bq_columns_full(...)]`.
- **`v2_schema.py:_fetch_bq_table_options`** — replaced by deriving `partition_by` (first row with `is_partitioning_column == True`) and `clustered_by` (rows with non-null `clustering_ordinal_position`, ordered by that position) from the same list.
- **`connectors/bigquery/metadata.py:_fetch_partition_cluster`** (new) — same two derivations.
- Net effect on `/api/v2/schema/{id}` cache miss: **2 BQ jobs → 1 BQ job**. ~50 % BQ-job reduction.

Helper location: `connectors/bigquery/access.py` already exposes `BqAccess` to both consumers; appending the helper there avoids creating yet another module and keeps BQ specifics in the BQ connector. (Earlier draft proposed `app/api/_bq_helpers.py` but that's a worse fit — the function is connector-bound, not API-bound.)

The consolidation is **independent of the metadata feature** in spirit but lands in the same PR because (a) the new metadata provider would otherwise add a third copy of the same SQL pattern, (b) the cache invalidation work touches the same `_schema_cache` the consolidation benefits from, and (c) splitting it would cost one extra round of CI + review.

### Server-side automatic cache warmup

In-process caches (the four flushed by `invalidate_for_table`) are empty after every container restart — a deploy, a rolling update, an OOM kill. The first analyst to call `agnes catalog` or `agnes schema <id>` after restart pays a cold-cache penalty: 1 BQ job per remote table for the catalog enrichment, plus 1 BQ job per `agnes schema` call. On a 30-table instance that's **30+ BQ jobs in the first analyst's first session, in burst**. Cost-wise it's negligible (INFORMATION_SCHEMA queries are <1 MB, $0.005/MB on-demand → $0.00015 for the whole burst). UX-wise it's a 2–6 second hiccup on the first catalog load. Operationally it's noise that confuses "is the new deploy slow?" with "is BQ slow?".

The fix: warm the caches automatically at process startup, in the background, with bounded concurrency. The first analyst hits warm caches; the BQ burst is spread across the readiness-up-to-fully-warm window, not a single user's request.

```python
# app/main.py — addition to startup events

@app.on_event("startup")
async def warm_catalog_caches():
    """Schedule a background warmup of the v2 catalog/schema/metadata caches.

    Fire-and-forget — readiness is not blocked. Operators can disable via
    `AGNES_SKIP_CACHE_WARMUP=1` in test/dev contexts. Failures inside the
    background task are logged + swallowed; never escalate to startup
    failure (a transient BQ outage at deploy time should not keep the
    server from coming up at all).
    """
    if os.environ.get("AGNES_SKIP_CACHE_WARMUP") == "1":
        return
    asyncio.create_task(_warm_catalog_caches_bg())
```

```python
# app/api/cache_warmup.py — new module

@dataclass
class WarmupRowState:
    table_id: str
    status: Literal["pending", "warming", "fresh", "error"]
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int | None = None
    error: str | None = None
    last_warmed_at: datetime | None = None  # carries across runs


@dataclass
class WarmupRunState:
    run_id: str
    trigger: Literal["startup", "manual", "registry_change"]
    started_at: datetime
    completed_at: datetime | None = None
    total: int = 0
    completed: int = 0
    failed: int = 0
    rows: dict[str, WarmupRowState] = field(default_factory=dict)
    # SSE subscribers attach to this; appended events are broadcast.
    _subscribers: list[asyncio.Queue] = field(default_factory=list, repr=False)


# Module-level singleton — survives across runs, holds the latest state.
WARMUP_STATE: WarmupRunState | None = None


async def _warm_catalog_caches_bg(trigger: str = "startup") -> None:
    """Walk the registry, warm metadata + schema caches for every BQ remote
    row with bounded concurrency. Errors are recorded per-row but never
    propagate. Emits SSE events as rows complete.
    """
    global WARMUP_STATE
    run_id = uuid4().hex[:8]
    state = WarmupRunState(run_id=run_id, trigger=trigger, started_at=now())

    # Snapshot registry — registry write during warmup is not coordinated;
    # stale snapshot is fine because the cache-bust path will refresh
    # whatever the warmup populated.
    conn = get_system_db()
    rows = TableRegistryRepository(conn).list_all()
    remote = [
        r for r in rows
        if r.get("query_mode") == "remote" and r.get("source_type") == "bigquery"
    ]
    state.total = len(remote)
    for r in remote:
        state.rows[r["id"]] = WarmupRowState(table_id=r["id"], status="pending")
    WARMUP_STATE = state
    _broadcast(state, {"event": "start", "data": {
        "run_id": run_id, "trigger": trigger, "total": state.total,
    }})

    sem = asyncio.Semaphore(int(os.environ.get("AGNES_WARMUP_CONCURRENCY", "4")))
    await asyncio.gather(*(_warm_one(r, state, sem) for r in remote))

    state.completed_at = now()
    _broadcast(state, {"event": "complete", "data": {
        "run_id": run_id, "total": state.total,
        "completed": state.completed, "failed": state.failed,
    }})
    logger.info(
        "cache warmup complete: run_id=%s total=%d ok=%d fail=%d",
        run_id, state.total, state.completed, state.failed,
    )


async def _warm_one(row: dict, state: WarmupRunState, sem: asyncio.Semaphore) -> None:
    async with sem:
        rs = state.rows[row["id"]]
        rs.status = "warming"
        rs.started_at = now()
        _broadcast(state, {"event": "row", "data": {**asdict(rs)}})
        t0 = time.monotonic()
        try:
            # Warm metadata cache via the same path live requests use.
            # _size_hint_for_row populates _metadata_cache as a side effect.
            await asyncio.to_thread(_warm_metadata, row)
            # Warm schema cache via the new RBAC-naive helper.
            await asyncio.to_thread(_warm_schema, row)
            rs.status = "fresh"
            rs.last_warmed_at = now()
            state.completed += 1
        except Exception as e:
            rs.status = "error"
            rs.error = str(e)
            state.failed += 1
            logger.warning("cache warmup row=%s failed: %s", row["id"], e)
        finally:
            rs.completed_at = now()
            rs.duration_ms = int((time.monotonic() - t0) * 1000)
            _broadcast(state, {"event": "row", "data": {**asdict(rs)}})
```

The `build_schema` function in `v2_schema.py` currently mixes RBAC + cache + BQ work. Refactor splits it:

- **`build_schema(conn, user, table_id, *, bq)`** — keeps RBAC + cache check at the top, then delegates to:
- **`build_schema_uncached(conn, table_id, *, bq)`** — does the BQ work + cache write only. Warmup calls this directly with no user context. ~10-LOC extraction.

### Status + control endpoints

```python
# app/api/cache_warmup.py — endpoints

@router.get("/api/admin/cache-warmup/status")
async def warmup_status(user: dict = Depends(require_admin)):
    """Return the latest warmup state as JSON. For polling fallback when
    SSE isn't available (e.g. behind a proxy that buffers)."""
    if WARMUP_STATE is None:
        return {"state": "never_run"}
    return _serialize_state(WARMUP_STATE)


@router.post("/api/admin/cache-warmup/run")
async def warmup_run(user: dict = Depends(require_admin)):
    """Manually trigger a warmup. Returns the new run_id immediately;
    the run executes in the background. Idempotent: if a warmup is
    already in progress, returns its run_id without starting another."""
    if WARMUP_STATE and WARMUP_STATE.completed_at is None:
        return {"run_id": WARMUP_STATE.run_id, "status": "already_running"}
    asyncio.create_task(_warm_catalog_caches_bg(trigger="manual"))
    return {"status": "started"}


@router.get("/api/admin/cache-warmup/stream")
async def warmup_stream(user: dict = Depends(require_admin)):
    """Server-Sent Events stream of warmup events. UI consumes this for
    realtime progress. Connection stays open for the lifetime of the
    current run + 5 s grace, then closes; client reconnects on next run.

    Event types: 'start', 'row', 'complete'. Each event is JSON.
    """
    return EventSourceResponse(_warmup_event_generator())
```

Three endpoints, all `require_admin`. `EventSourceResponse` is from `sse-starlette` (already a transitive dep; if not, ~3 KB additional install).

### Cache-bust now also re-warms

`invalidate_for_table` (defined above) flushes caches. After flushing, immediately enqueue a **single-row warmup** for the affected `table_id` so admins editing a registry row see fresh data within a couple of seconds rather than waiting for the next analyst to trigger a miss:

```python
def invalidate_for_table(table_id: str) -> None:
    """... (existing flush logic) ..."""
    # ... existing cache.clear() / .invalidate() calls ...

    # Schedule a single-row re-warm in the background. Doesn't block the
    # admin's HTTP response. Fire-and-forget; failures log + skip.
    asyncio.create_task(_rewarm_one_row(table_id))
```

Effect: admin clicks "Save" in the edit modal → response returns in ~50 ms → 1-2 s later the warmup task has populated fresh metadata + schema caches → next `agnes catalog` request is warm. The admin doesn't see a "warming…" state because their edit doesn't call catalog/schema.

### Operations env vars

| Var | Default | Effect |
|---|---|---|
| `AGNES_SKIP_CACHE_WARMUP` | `0` | If `1`, the startup hook is a no-op. For dev / test instances. |
| `AGNES_WARMUP_CONCURRENCY` | `4` | How many BQ INFORMATION_SCHEMA jobs to run in parallel. Bounded; raising this beyond 8 risks tripping BQ's 100-concurrent-job project quota on instances with 100+ tables. |

No new instance.yaml knobs; warmup is unconditional in production, opt-out only.

### BQ provider implementation sketch

Two separate `bigquery_query()` calls, mirroring `v2_schema._fetch_bq_table_options` line-for-line. Same positional `?` binding, same identifier-validation discipline already enforced by the dispatcher, same sentinel-config early-return, same `try/except → None`. Combining them into one CTE was the previous spec's mistake — the codebase has zero precedent for multi-CTE BQ queries through `bigquery_query()`, the `LEFT JOIN ... ON TRUE` pattern made the empty-`cols` case yield `[NULL]` rather than `[]` (relied on coincidence to unwrap), and one extra round-trip on a 15-min-cached call site is not worth the risk.

```python
# connectors/bigquery/metadata.py

import logging

from app.api._metadata_models import MetadataRequest, TableMetadata
from connectors.bigquery.access import BqAccessError, get_bq_access

logger = logging.getLogger(__name__)


def fetch(req: MetadataRequest) -> TableMetadata | None:
    try:
        bq = get_bq_access()
    except BqAccessError:
        return None

    # Sentinel-config early-return — mirror v2_schema._fetch_bq_table_options:107.
    # On a Keboola-only deployment, BqAccess is the sentinel and projects.data
    # is empty. Returning None here keeps the catalog response clean (no
    # mystery "size unknown" entries) and means the lazy-import rationale
    # actually pays off — we don't run a query, we don't even build SQL.
    if not bq.projects.data:
        return None

    # Identifier validation already done in the dispatcher
    # (_build_metadata_request); req.bucket / req.source_table are safe
    # to interpolate.
    rows_size = _fetch_rows_and_size(bq, req)
    part_clust = _fetch_partition_cluster(bq, req)
    if rows_size is None and part_clust is None:
        # Both queries failed — likely permissions or BQ down.
        # Caller treats None as "unknown" and falls through to the existing
        # null-size-hint contract.
        return None

    return TableMetadata(
        rows=(rows_size or {}).get("rows"),
        size_bytes=(rows_size or {}).get("size_bytes"),
        partition_by=(part_clust or {}).get("partition_by"),
        clustered_by=(part_clust or {}).get("clustered_by"),
    )


def _fetch_partition_cluster(bq, req: MetadataRequest) -> dict | None:
    """Reuse the EXACT shape from v2_schema._fetch_bq_table_options:115-126.

    We don't import the v2_schema helper directly because:
    - It's marked private (leading underscore).
    - Coupling the catalog provider to a sibling endpoint's internals
      makes future refactors (e.g. v2_schema rewrite) ripple here.
    The right move is one shared helper after a third caller appears;
    until then, two co-located copies with this comment is cleaner than
    a premature abstraction. (Tracked in "Out of scope".)
    """
    try:
        bq_sql = (
            f"SELECT column_name, is_partitioning_column, clustering_ordinal_position "
            f"FROM `{bq.projects.data}.{req.bucket}.INFORMATION_SCHEMA.COLUMNS` "
            f"WHERE table_name = ? "
            f"ORDER BY clustering_ordinal_position NULLS LAST"
        )
        with bq.duckdb_session() as conn:
            rows = conn.execute(
                "SELECT * FROM bigquery_query(?, ?, ?)",
                [bq.projects.billing, bq_sql, req.source_table],
            ).fetchall()
    except Exception as e:
        logger.warning(
            "BQ metadata partition/cluster fetch failed for %s.%s.%s: %s",
            bq.projects.data, req.bucket, req.source_table, e,
        )
        return None
    partition_by = next(
        (r[0] for r in rows if (r[1] or "").upper() == "YES"),
        None,
    )
    clustered_by = [r[0] for r in rows if r[2] is not None]
    return {"partition_by": partition_by, "clustered_by": clustered_by}


def _fetch_rows_and_size(bq, req: MetadataRequest) -> dict | None:
    """Return {rows, size_bytes} for a BQ table, or None on failure.

    Uses INFORMATION_SCHEMA.TABLE_STORAGE at REGION scope (the only
    valid scope per live verification 2026-05-07 — see Open Question §1).
    Falls through to legacy __TABLES__ on TABLE_STORAGE failure (e.g.
    operator typo'd the location config, region mismatch, IAM gap).

    For VIEW-backed entries both views return no rows; caller gets
    None which is the correct answer (a view has no inherent scan size).
    """
    location = _resolve_bq_location(bq, req)
    if location:
        result = _fetch_via_table_storage(bq, req, location)
        if result is not None:
            return result
        # TABLE_STORAGE failed despite a configured location. Could be
        # a typo (`us-central` vs `us-central1`), a multi-region dataset
        # the operator misclassified, or a transient permission gap.
        # Try __TABLES__ before giving up — same numbers, different
        # IAM surface.
    return _fetch_via_legacy_tables(bq, req)


def _resolve_bq_location(bq, req: MetadataRequest) -> str | None:
    """Return the BQ region (e.g. "us-central1") for the dataset, or None.

    Resolution order:
      1. instance.yaml `data_source.bigquery.location` (the common case;
         operators with a single-region BQ deployment set this once).
      2. google-cloud-bigquery REST: `client.get_dataset(dataset_id).location`.
         Cached at the dispatcher (TBD — likely a small TTL dict on
         `(project, dataset) → location`).
      3. None → caller falls back to legacy __TABLES__.
    """
    # Implementation detail; see app.instance_config.get_value lookup.
    from app.instance_config import get_value
    cfg_location = (get_value("data_source.bigquery.location") or "").strip()
    if cfg_location:
        return cfg_location
    try:
        ds = bq.bigquery_client().get_dataset(
            f"{bq.projects.data}.{req.bucket}"
        )
        return ds.location
    except Exception as e:
        logger.warning(
            "BQ dataset.get failed for %s.%s — falling back to __TABLES__: %s",
            bq.projects.data, req.bucket, e,
        )
        return None


def _fetch_via_table_storage(bq, req: MetadataRequest, location: str) -> dict | None:
    """Region-scoped INFORMATION_SCHEMA.TABLE_STORAGE — preferred path.

    `validate_quoted_identifier` accepts `us-central1`, `europe-west1`,
    `EU`, `us` etc. (regex `^[a-zA-Z0-9_][a-zA-Z0-9_.\\-]{0,127}$` —
    verified 2026-05-07). Refuses anything that could break out of the
    backtick-quoted path.

    The size_bytes reported is `active + long_term` logical bytes —
    a full BQ scan reads both, so reporting only `active` undercounts
    aged partitioned tables. See spec Open Question §1 for rationale.
    """
    from src.identifier_validation import validate_quoted_identifier
    if not validate_quoted_identifier(location, "BQ region"):
        return None
    try:
        bq_sql = (
            f"SELECT total_rows, "
            f"IFNULL(active_logical_bytes, 0) + IFNULL(long_term_logical_bytes, 0) "
            f"FROM `{bq.projects.data}.region-{location}.INFORMATION_SCHEMA.TABLE_STORAGE` "
            f"WHERE table_schema = ? AND table_name = ?"
        )
        with bq.duckdb_session() as conn:
            row = conn.execute(
                "SELECT * FROM bigquery_query(?, ?, ?, ?)",
                [bq.projects.billing, bq_sql, req.bucket, req.source_table],
            ).fetchone()
    except Exception as e:
        logger.warning(
            "BQ TABLE_STORAGE fetch failed for %s.%s.%s: %s",
            bq.projects.data, req.bucket, req.source_table, e,
        )
        return None
    if row is None:
        return None  # row absent ⇒ entry is a VIEW, or table lives in
                     # a different region than the configured one.
                     # Caller falls through to __TABLES__.
    rows_, size_bytes = row
    return {
        "rows": int(rows_) if rows_ is not None else None,
        "size_bytes": int(size_bytes) if size_bytes is not None else None,
    }


def _fetch_via_legacy_tables(bq, req: MetadataRequest) -> dict | None:
    """Last-resort dataset-scoped __TABLES__ — works without region."""
    try:
        bq_sql = (
            f"SELECT row_count, size_bytes "
            f"FROM `{bq.projects.data}.{req.bucket}.__TABLES__` "
            f"WHERE table_id = ?"
        )
        with bq.duckdb_session() as conn:
            row = conn.execute(
                "SELECT * FROM bigquery_query(?, ?, ?)",
                [bq.projects.billing, bq_sql, req.source_table],
            ).fetchone()
    except Exception as e:
        logger.warning(
            "BQ __TABLES__ fetch failed for %s.%s.%s: %s",
            bq.projects.data, req.bucket, req.source_table, e,
        )
        return None
    if row is None:
        return None
    rows_, size_bytes = row
    return {
        "rows": int(rows_) if rows_ is not None else None,
        "size_bytes": int(size_bytes) if size_bytes is not None else None,
    }
```

Notes:

- **Two queries, not one CTE.** Forced by BQ schema: TABLE_STORAGE is region-scoped, COLUMNS is dataset-scoped, they live at different fully-qualified paths and cannot share a query. Live-verified 2026-05-07 (Open Question §1).
- **`bq.projects.billing` first arg, `bq.projects.data` in the SQL path.** Same as v2_schema. The billing project is who-pays-for-the-query; the data project is whose-tables-we-read.
- **Partition/cluster path is verbatim copy of `_fetch_bq_table_options`:115-126.** If a follow-up PR consolidates the duplication into `app/api/_bq_helpers.py`, the consolidation can drop in without touching the provider's contract.
- **Region resolution prefers config over discovery.** `instance.yaml.data_source.bigquery.location` is already a documented knob; reading it from `app.instance_config.get_value` avoids a per-dataset round-trip in the common case (single-region deployments). The `bq_client.get_dataset(...)` fallback handles the rare multi-region or unset-config case; the `__TABLES__` fallback handles the rarer SA-can-query-but-not-`bigquery.datasets.get` case.

### Keboola provider implementation sketch

```python
# connectors/keboola/metadata.py

import logging

from app.api._metadata_models import MetadataRequest, TableMetadata
from connectors.keboola.client import KeboolaClient
from connectors.keboola.storage_api import (
    KeboolaStorageClient, StorageApiError,
)

logger = logging.getLogger(__name__)


def fetch(req: MetadataRequest) -> TableMetadata | None:
    # Reuse KeboolaClient's existing env-fallback path (KEBOOLA_STACK_URL
    # + KEBOOLA_STORAGE_TOKEN env vars, mirrors instance.yaml token_env
    # convention). We construct it just to read `.token` and `.url` —
    # this is intentional; KeboolaClient.__init__ has no side effects
    # beyond setting those two attributes (verified
    # connectors/keboola/client.py:90-99). When a future refactor extracts
    # `_resolve_keboola_credentials()` as a standalone helper, switch the
    # provider to call that directly.
    creds = KeboolaClient(token=None, url=None)
    if not creds.url or not creds.token:
        return None  # not configured — same posture as BQ sentinel

    table_id = f"{req.bucket}.{req.source_table}"
    try:
        storage = KeboolaStorageClient(url=creds.url, token=creds.token)
        info = storage.get_table_info(table_id)  # NEW thin wrapper — see below
    except (StorageApiError, ValueError) as e:
        logger.warning("Keboola metadata fetch failed for %s: %s", table_id, e)
        return None

    return TableMetadata(
        rows=info.get("rowsCount"),
        size_bytes=info.get("dataSizeBytes"),
        # Keboola has no BQ-style partition/cluster concept; primaryKey is
        # conceptually different (uniqueness, not physical layout). Leave
        # partition_by / clustered_by as None.
    )
```

**Token resolution: reuse `KeboolaClient.__init__`'s existing env-fallback.** Verified at `connectors/keboola/client.py:90-99`:

```python
def __init__(self, token: Optional[str] = None, url: Optional[str] = None):
    ...
    self.token = token or os.environ.get("KEBOOLA_STORAGE_TOKEN", "")
    self.url = url or os.environ.get("KEBOOLA_STACK_URL", "")
```

Constructing `KeboolaClient(token=None, url=None)` is a zero-side-effect way to inherit the same env-var hierarchy the rest of the codebase uses. **No third token-lookup path is invented.** A small future refactor could extract a standalone `_resolve_keboola_credentials()` helper that both `KeboolaClient.__init__` and this provider call directly; tracked as a low-priority follow-up nit, not a blocker.

**`get_table_info(table_id)` — thin wrapper added to `KeboolaStorageClient` in this PR.** The previous spec called `client._get(f"/tables/{table_id}")` directly; that bleeds a `_`-private method out of the module and reviewers will (rightly) push back. One-line wrapper:

```python
# connectors/keboola/storage_api.py — addition

def get_table_info(self, table_id: str) -> dict:
    """GET /v2/storage/tables/{table_id} — full table metadata.

    Storage API guarantees `rowsCount` + `dataSizeBytes` on success.
    Other fields (`columns`, `primaryKey`, ...) are present but not
    consumed today. Raises `StorageApiError` on 4xx/5xx.
    """
    return self._get(f"/tables/{table_id}")
```

Confirmed against existing call sites: `connectors/keboola/client.py:211-212,801-802` already destructure `rowsCount` and `dataSizeBytes` from the same endpoint. Test fixture `tests/test_admin_bq_register.py:1746` mocks the same shape. No surprises.

### Catalog response shape — no breaking change

Today's response per row:

```jsonc
{
  "id": "orders",
  "name": "orders",
  "description": "...",
  "source_type": "bigquery",
  "query_mode": "remote",
  "sql_flavor": "bigquery",
  "where_examples": ["..."],
  "fetch_via": "agnes snapshot create ...",
  "rough_size_hint": null  // ← now populated for remote rows
}
```

After:

```jsonc
{
  // ... (all of the above) ...
  "rough_size_hint": "large",            // size bucket — was null for remote
  "rows": 12345678,                       // NEW — exact when known, null when not
  "size_bytes": 4567890123,               // NEW — exact when known, null when not
  "partition_by": "event_date",           // NEW — only for BQ, null otherwise
  "clustered_by": ["country", "platform"] // NEW — only for BQ, null otherwise
}
```

`rough_size_hint` keeps the existing bucket vocabulary (`small` / `medium` / `large` / `very_large`); the new exact fields are additive. Existing CLI consumers that read only `rough_size_hint` keep working unchanged.

### `agnes describe` — verify, don't fix

`/api/v2/sample/{id}` already has a BigQuery branch. Implementation plan includes a smoke test against a live BQ remote table; if it returns rows, **no code change**. If it doesn't, the fix is a 5-line `bigquery_query("SELECT * FROM ... LIMIT N")` along the same path as `v2_schema`. Don't pre-emptively scope-creep.

CLI side (`agnes describe`) calls the v2 sample endpoint and renders rows — no per-source-type branching client-side. Nothing to change there.

---

## Documentation surface

Single doc, one table-mode reference, future-proofs for new connectors.

### `docs/admin/query-modes.md` (new)

Outline:

1. **Why three modes** — table comparing `local` vs `remote` vs `materialized` on (storage location, query path, cost model, freshness, scan limits).
2. **Decision tree** — flowchart prose:
   - Table updates daily and fits on a laptop (≤ 1 GB) → `local`
   - Table updates frequently / live (intraday) → `remote`
   - Table is the *result* of a daily SQL aggregate → `materialized`
   - Table is too big to sync but rarely-queried (compliance/residency) → `remote`
3. **Per-source-type reference**:
   - **BigQuery** — IAM (`bigquery.dataViewer` + `bigquery.jobUser`), `billing_project` vs `project` distinction (cross-link to the `bq_config` info-tier health check from #178), `bq_max_scan_bytes` cost gate, registration via `agnes admin register-table --source-type bigquery --query-mode remote --bucket <dataset> --source-table <table>`, registration via UI.
   - **Keboola** — Storage API token requirements, `local` is the path in production today, `remote` is architecturally supported via the Keboola DuckDB extension's `_remote_attach` mechanism but not in active deployment use. Includes a forward-looking note: *"If you have an analyst workflow against a Keboola table that's too big to sync, file an issue — the architecture is in place but the registration UX hasn't been polished."*
   - **Jira** — event-driven ingestion, always `local`. Webhook setup pointer.
4. **Three worked examples** (one per source type) — copy-paste CLI invocations.
5. **Cross-references** — to `RBAC.md` (grants), to `instance.yaml.example` (config knobs), to the BQ skill in `docs/skills/`.

The doc is the single landing place for the question *"can / how do I register a $X table for $Y mode?"* — replaces the absent breadcrumb #156 calls out.

### Admin UI integration — `/admin/tables` only

All visibility lives on the existing `/admin/tables` page. **No new admin pages.** The page already lists every registered table grouped by `source_type` (`bqTableListing` / `kbTableListing` / `jiraTableListing`) and renders rows via `renderRegistryListing(target, tables)`. The row markup already reserves an empty `<th class="col-status"></th>` column at the end — perfect slot for a cache-freshness badge with no schema-of-rendered-table change.

Three additions to the page:

**1. Cache toolbar** — a single card above the per-source-type listings, visible only when at least one BQ remote table is registered:

```
┌─────────────────────────────────────────────────────────────┐
│  Cache freshness                            [Re-warm all]   │
│                                                              │
│  ●●●●●●●●●●○○○○○  21 / 30 fresh                              │
│  Last completed run: 4 minutes ago (28 ok, 2 errors)         │
│                                                              │
│  [▾ Show log]                                                │
└─────────────────────────────────────────────────────────────┘
```

When a run is in progress, the bar animates and `[Re-warm all]` is disabled. The "Show log" expand reveals a terminal-style scrolling area:

```
┌─ Warmup log — run f4d2bcae ───────────────────────────────┐
│ 14:32:01  start   trigger=startup total=30                │
│ 14:32:01  warming events_2024                             │
│ 14:32:01  warming users_2024                              │
│ 14:32:01  warming orders_2024                             │
│ 14:32:01  warming sessions_2024                           │
│ 14:32:02  fresh   events_2024  (1.2 s)                    │
│ 14:32:02  warming products_2024                           │
│ 14:32:02  fresh   users_2024   (1.4 s)                    │
│ ...                                                       │
│ 14:32:14  error   stale_table_v1  permission denied       │
│ 14:32:18  complete  total=30 ok=28 fail=2                 │
└───────────────────────────────────────────────────────────┘
```

The log is the SSE event stream rendered in chronological order. Auto-scrolls to bottom while a run is active; freezes when the run completes so the admin can scroll back.

**2. Per-row cache badge in `col-status`** — populated from the WARMUP_STATE snapshot on page load and updated live from SSE:

| Status | Badge |
|---|---|
| `fresh` (warmed within TTL) | ● green "fresh 4m" (with relative-time tooltip) |
| `warming` (in current run) | ● blue spinner "warming…" |
| `pending` (queued, not started) | ○ grey "queued" |
| `error` (last run failed for this row) | ● red "error" (with tooltip showing `state.error`) |
| not-warmed-yet OR cache TTL expired without re-warm | (empty cell) |

For non-BQ-remote rows (Keboola local, Jira), the column stays empty — they don't go through the warmup path. This keeps the column visually quiet when there's nothing useful to say.

**3. `?` icon next to the `query_mode` field** in the Add/Edit modal, linking to `docs/admin/query-modes.md`. The original "minimal admin UI" change. Survives unchanged.

### Wiring details

- **Initial state on page load:** call `GET /api/admin/cache-warmup/status` once, populate the toolbar + per-row badges from the response.
- **Live updates:** open `EventSource("/api/admin/cache-warmup/stream")` after the initial render. Each event mutates the corresponding row badge + appends to the log. Reconnect logic is built into `EventSource` for free.
- **SSE failure fallback:** if `EventSource.onerror` fires repeatedly (browser, proxy, content-security), fall back to polling `/status` every 3 s. Same code path, reads the same JSON shape.
- **"Re-warm all" button:** `POST /api/admin/cache-warmup/run` — server schedules the run, response includes the new `run_id`. UI keeps watching the SSE stream; the new `start` event has the new `run_id` so the log section auto-clears the prior run's lines.
- **Edit-modal cache flush hint:** when the admin saves an edit (existing `saveTableEdit` flow), the server's `invalidate_for_table` already triggers a single-row re-warm in the background. The UI doesn't need new copy here; the badge will update via SSE within 1-2 s.

The toolbar + log fit in **one new `<section>` block** between the page header and the per-source-type table listings (`bqTableListing` etc.). Plus ~80 LOC of JS to render + bind. Plus the per-row badge addition in `renderRegistryListing` (~10 LOC).

### CLI hint at registration time

`agnes admin register-table` already prints two post-success hints (the `Next: run agnes setup first-sync` and the `register-table does not auto-grant` notes). Add a third when `query_mode=remote` is registered:

```
Note: this is a remote-query table. Verify the SA can read it:
  agnes query --remote "SELECT COUNT(*) FROM <id>"
If it 403s, see docs/admin/query-modes.md → "BigQuery → IAM".
```

One conditional, mirrors the existing pattern. No new flag.

---

## Server-side changes

### New files

- `app/api/_metadata_models.py` — `MetadataRequest` + `TableMetadata` dataclasses. Lives under `app/api/` (not `connectors/`) — primary consumer is `app/api/v2_catalog.py`; providers in `connectors/` import upward into the API layer. Avoids layering inversion of `app/api/v2_catalog.py` importing from `connectors/__init__.py`.
- `connectors/bigquery/metadata.py` — `fetch(req)` returning `TableMetadata | None`. Calls the new shared `fetch_bq_columns_full` helper for partition/cluster.
- `connectors/keboola/metadata.py` — same shape, Storage API path.
- `app/api/cache_warmup.py` — `WarmupRunState` + `WarmupRowState` dataclasses, `_warm_catalog_caches_bg`, `_warm_one`, `_rewarm_one_row`, SSE generator, the three `/api/admin/cache-warmup/*` endpoints.
- `tests/test_connectors_bigquery_metadata.py` — 5 unit cases (happy / sentinel / VIEW / region-typo / both-paths-fail).
- `tests/test_connectors_keboola_metadata.py` — 3 unit cases (happy / unconfigured / api-error).
- `tests/test_v2_catalog_remote_metadata.py` — integration test against the catalog endpoint; verifies response shape + cache hit/miss.
- `tests/test_v2_catalog_invalidation.py` — verifies `invalidate_for_table` flushes all four caches and triggers single-row re-warm.
- `tests/test_cache_warmup.py` — startup runs in background without blocking readiness; bounded concurrency; per-row failure isolated; SSE event stream shape; `/run` idempotency under concurrent invocation.
- `tests/test_admin_tables_warmup_ui.py` — smoke test that `/admin/tables` HTML contains the cache toolbar markup, the per-row `col-status` slot, and the `EventSource` wiring.

### Edited files

- `app/api/v2_catalog.py` — rename `_materialized_size_hint` → `_size_hint_for_row`, add provider dispatch (`_metadata_provider_for`, `_build_metadata_request`), add `_metadata_cache` (TTLCache, 15 min), extend response shape with the new fields, add `invalidate_for_table` helper. ~80 LOC delta.
- `app/api/v2_schema.py` — split `build_schema` into RBAC-checking outer + uncached inner (`build_schema_uncached`); replace `_fetch_bq_schema` + `_fetch_bq_table_options` with the shared `fetch_bq_columns_full` helper consumed by both schema response builder and the metadata provider's partition/cluster path. ~40 LOC delta (mostly refactor).
- `connectors/bigquery/access.py` — append the `fetch_bq_columns_full(bq, dataset, table)` helper (single combined `INFORMATION_SCHEMA.COLUMNS` query). ~50 LOC.
- `app/main.py` — register the `warm_catalog_caches` startup event hook. ~10 LOC.
- `app/api/admin.py` — wire `v2_catalog.invalidate_for_table(table_id)` into the success path of `register_table`, `update_table`, and `unregister_table`. ~6 LOC.
- `cli/commands/admin.py` — extend the post-register hint with the BQ-remote IAM smoke-check pointer. ~5 LOC.
- `app/web/templates/admin_tables.html` — new `<section id="cacheWarmupCard">` toolbar block, per-row badge in `renderRegistryListing`, `?` icon next to `query_mode` field in the edit modal, `EventSource` + polling-fallback JS. ~250 LOC delta in this template.

### Schema / DB / config

**No schema migration.** All metadata is computed on demand from BigQuery / Keboola Storage API. Deliberately not persisted — adds a bookkeeping problem (staleness, invalidation, schema bumps) we don't need.

**Two new env vars (both opt-out / tuning, no required setup change):**

| Var | Default | Effect |
|---|---|---|
| `AGNES_SKIP_CACHE_WARMUP` | unset | If `1`, the FastAPI startup warmup hook is a no-op. For dev / test instances. |
| `AGNES_WARMUP_CONCURRENCY` | `4` | How many BQ INFORMATION_SCHEMA jobs to run in parallel during a warmup run. Bounded; raising beyond 8 risks tripping BQ's 100-concurrent-job project quota on instances with 100+ tables. |

The connector configs (`data_source.bigquery.*`, `data_source.keboola.storage_*`) already exist in `instance.yaml` and are not touched here.

---

## Test plan

| Layer | Coverage |
|---|---|
| Provider (BQ) — happy path | mocked `bq.duckdb_session()` returns synthetic row → `fetch(req)` returns expected `TableMetadata` with `size_bytes = active + long_term` |
| Provider (BQ) — sentinel | `bq.projects.data == ""` → returns `None` before any query, never imports `validate_quoted_identifier` |
| Provider (BQ) — VIEW path | TABLE_STORAGE returns no rows, `__TABLES__` also returns no rows → `TableMetadata(rows=None, size_bytes=None, partition_by=<from COLUMNS>, clustered_by=<from COLUMNS>)`. Asserts the view-aware fall-through documented in §"View-backed remote tables" |
| Provider (BQ) — region typo | location set to `"us-central"` (invalid) → `_fetch_via_table_storage` raises BQ "not found", `_fetch_rows_and_size` falls through to `_fetch_via_legacy_tables` → still returns rows + size |
| Provider (BQ) — both paths fail | TABLE_STORAGE raises and `__TABLES__` raises → `_fetch_rows_and_size` returns `None`; `fetch()` still returns a `TableMetadata` with partition/cluster populated (only the size pieces are `None`) |
| Provider (Keboola) | mocked `KeboolaStorageClient.get_table_info` returns `{rowsCount, dataSizeBytes}` → `fetch(req)` returns expected metadata; `KeboolaClient(token=None, url=None)` with empty env → `None`; `StorageApiError` → `None` |
| Catalog endpoint | for a `query_mode='local'` row → existing parquet-stat path unchanged; for a `query_mode='remote'` BQ row → provider called, response has the new fields populated; cache hit returns cached metadata without re-calling provider |
| Cache-bust | `register_table` / `update_table` / `unregister_table` each flush all four caches (`_table_rows_cache`, `_metadata_cache`, `_schema_cache`, `_sample_cache`). After bust, next catalog/schema request reflects new state. Background re-warm task is scheduled for the affected `table_id` only. |
| Cache warmup — startup | `warm_catalog_caches` startup hook runs in background without blocking `/api/health` readiness; warmup completes within `total × 200ms / concurrency` budget for synthetic 30-row registry. |
| Cache warmup — failure isolation | one row's `_warm_one` raises; remaining rows still process; `WarmupRowState.error` is populated for the failed row only; final `state.failed == 1, state.completed == total - 1`. |
| Cache warmup — bounded concurrency | with `AGNES_WARMUP_CONCURRENCY=2` and 30 rows, at most 2 `_warm_one` invocations run concurrently (assert via mock semaphore-tracked counter). |
| Cache warmup — `/run` idempotency | calling `POST /api/admin/cache-warmup/run` twice in flight returns the same `run_id` on the second call without spawning a second background task. |
| Cache warmup — registry-change rewarm | `invalidate_for_table(id)` schedules a single-row re-warm task; `WARMUP_STATE` is updated with that one row's progress. |
| SSE stream | `GET /api/admin/cache-warmup/stream` yields `start` / `row` / `complete` events in JSON; events arrive within ~200 ms of state changes; client disconnect doesn't crash the producer. |
| Status endpoint | `GET /api/admin/cache-warmup/status` returns the latest state (or `{"state": "never_run"}` before any run); reflects per-row state including `last_warmed_at` carried across runs. |
| Admin UI smoke | `/admin/tables` HTML contains the cache toolbar `<section>`, the `EventSource` wiring, and the `col-status` per-row slot for BQ remote rows. (Doesn't run JS — just verifies the markup is present.) |
| `agnes catalog` CLI | smoke test that the new fields surface in `--json` output and don't break the text-mode renderer. |
| Sample endpoint | smoke test against a registered remote BQ row; verify it returns sample rows. If broken, separate fix path; not bundled in this PR's scope. |

The new tests sit alongside `test_v2_catalog.py` (existing), `test_diagnose_billing.py` (existing — uses the same `seeded_app` BQ-mocking fixture).

---

## Migration / compatibility

- **Wire-break: no.** Catalog response is additive. New fields default to `null` for sources without a provider; existing CLI consumers reading only `rough_size_hint` and `query_mode` are unaffected.
- **`MIN_COMPAT_CLI_VERSION`** stays at `0.0.0`.
- **BQ quota.** A typical instance with 30 remote tables sees one INFORMATION_SCHEMA query per table per 15-min window. INFORMATION_SCHEMA is metadata-only, doesn't bill against scan quota. Project-level concurrent-query quota is the only conceivable limit; with the 15-min cache it's not reachable.
- **Keboola Storage API.** One `GET /tables/{id}` call per remote Keboola table per 15 min. Storage API has no public rate limit on metadata reads. Negligible.
- **Performance.** First catalog call after a TTL expiry pays the round-trip cost (one BQ query + one Keboola GET). Subsequent calls within the window are sub-millisecond cache hits. Provider failures (network, permissions) are non-blocking — catalog response always returns within the existing latency budget.

---

## Out of scope (revisit later)

- **Profile / column histograms / cardinality for remote tables.** Big lift, separate issue.
- **`rough_size_hint` boundaries per source type.** A 5-GiB BQ table is "easy on remote" because of partition pruning; a 5-GiB Keboola table can't be remote at all. Bucket vocabulary is currently shared across sources; might want per-source thresholds eventually. Tracked as a follow-up nit.
- **Provider plug-in registration via entry-points.** Currently the dispatch table is a hardcoded if-tree in `_metadata_provider_for`. If a future plugin API ships (#8), this becomes one line of registry boilerplate. Not worth pre-emptively building.
- **Onboarding nudge** ("you have 0 remote tables, consider registering some BQ ones"). Worth doing — admin dashboard empty-state + `agnes init` summary footer line — but a UX call separate from this metadata work. Followup issue after this lands.

---

## Open questions

### 1. Which BigQuery view exposes row count + size? **RESOLVED — verified live on `prj-grp-foundryai-dev-7c37` 2026-05-07.**

Three candidates were surveyed and tested against `audrius_test.product_inventory` (25-row table in us-central1). Outcome:

| View | Status | Notes |
|---|---|---|
| **`<project>.region-<region>.INFORMATION_SCHEMA.TABLE_STORAGE`** | ✅ **chosen** | Returns `total_rows`, `active_logical_bytes`, `long_term_logical_bytes`, `active_physical_bytes`, `long_term_physical_bytes`. Filter via `WHERE table_schema='<dataset>' AND table_name='<table>'`. Confirmed `active_logical_bytes` matches legacy `__TABLES__.size_bytes` byte-for-byte (2407 == 2407). |
| `<project>.<dataset>.INFORMATION_SCHEMA.TABLE_STORAGE` | ❌ doesn't exist | `bq query` returns "Not found: Dataset prj-grp-foundryai-dev-7c37:audrius_test.INFORMATION_SCHEMA was not found in location us-central1". TABLE_STORAGE is region-scoped only. |
| `<project>.<dataset>.__TABLES__` (legacy) | ⚠️ fallback only | Works (`row_count=25, size_bytes=2407`), but per-dataset (no multi-region) and rumoured to be deprecated. Use only if region resolution fails. |
| `__TABLES_SUMMARY__` | n/a | Separate legacy view, distinct columns. **Not** an alias of `__TABLES__` (the original spec was wrong on this). Don't use. |

**Locked SQL for the BQ provider:**

```sql
SELECT
  total_rows,
  IFNULL(active_logical_bytes, 0) + IFNULL(long_term_logical_bytes, 0) AS total_logical_bytes
FROM `<project>.region-<location>.INFORMATION_SCHEMA.TABLE_STORAGE`
WHERE table_schema = ? AND table_name = ?
```

Mapped to `TableMetadata` as `rows = total_rows`, `size_bytes = total_logical_bytes` (active + long-term). **The sum is correct for the cost-warning use case** — a full BQ table scan reads both partitions; reporting only `active_logical_bytes` would undercount on partitioned tables that have aged into long-term storage (≥ 90 days untouched), and the analyst's mental model of "this is a 200-GB table" includes long-term. The `physical_bytes` variants are NOT exposed — they're compression-aware storage billing, not scan-cost.

**View-backed remote tables:** `INFORMATION_SCHEMA.TABLE_STORAGE` returns **no rows** for entries whose `table_type = 'VIEW'` (verified: TABLE_STORAGE only covers physical storage). For a `query_mode='remote'` row pointing at a VIEW, `_fetch_via_table_storage` returns `None`, and the legacy `__TABLES__` fallback also returns `None` for views. The final `TableMetadata` therefore has `rows=None, size_bytes=None` — which is **correct**: a view's scan cost depends on the underlying query, not on the view itself. The analyst Claude reads `null` and applies the existing CLAUDE.md guidance (*"treat as potentially large; use `agnes snapshot create --estimate` first"*). Partition + cluster metadata DOES surface for views via `INFORMATION_SCHEMA.COLUMNS` if the underlying tables are partitioned, so the response isn't entirely empty. Materialised views (`MATERIALIZED_VIEW`) DO appear in TABLE_STORAGE because they have stored bytes, so the path works for them out-of-the-box. Tested behavior, not theoretical: implementation plan includes a unit test that mocks TABLE_STORAGE returning empty for a view and asserts `TableMetadata(rows=None, size_bytes=None, partition_by=...)`.

### 1a. Where does `<region>` come from?

**Primary:** `data_source.bigquery.location` in `instance.yaml` (already a documented config knob — see `config/instance.yaml.example:116`). Operators with a single-region BQ deployment (the common case) set this once; provider reads it.

**Fallback:** if `location` is unset and the dataset's region can't be inferred, the provider tries `bq_client.get_dataset(dataset_id).location` via the existing google-cloud-bigquery REST client (one cached round-trip per dataset). If that also fails (e.g. the SA lacks `bigquery.datasets.get`), the provider falls back to legacy `__TABLES__` which is dataset-scoped and doesn't need region knowledge — at the cost of losing the region-portable property.

The dispatch order is: **`instance.yaml.location` → `bq_client.get_dataset` → legacy `__TABLES__`**. Most deployments hit the first; the rest have a graceful path.

### 1b. Why two queries, not one CTE

The original spec proposed a single combined CTE. After live verification this is **architecturally impossible**: TABLE_STORAGE lives at *region* scope (`<project>.region-<region>.INFORMATION_SCHEMA.TABLE_STORAGE`); COLUMNS lives at *dataset* scope (`<project>.<dataset>.INFORMATION_SCHEMA.COLUMNS`). They cannot be joined inside a single `bigquery_query()` call — different fully-qualified paths require separate queries. Two round-trips is forced, not a preference.

### 2. Ingestion-time partitioning pseudo-columns

**RESOLVED — defer to existing v2_schema behavior, no new code.**

The original concern: for tables partitioned by ingestion time (BQ's `_PARTITIONTIME` / `_PARTITIONDATE` pseudo-columns), `INFORMATION_SCHEMA.COLUMNS` may or may not surface them as `is_partitioning_column='YES'`. Live verification could not be completed — the SA on `prj-grp-foundryai-dev-7c37` doesn't have visibility into a partitioned table that's also reachable for testing. But this is **not a blocker** because:

1. The new BQ provider's partition/cluster path is a **verbatim copy** of `v2_schema._fetch_bq_table_options:115-126`, which has been running in production for months. Whatever its behavior is on ingestion-time-partitioned tables, the metadata provider will produce identical output — and the `/api/v2/schema` endpoint already serves that output to analysts today without complaints.
2. The fallback contract is well-defined: provider returns `partition_by=None` if no row matches `is_partitioning_column='YES'`. Analyst Claude treats `null` as "no usable partition pruning" and falls back to the BQ cap-guard. No corruption mode.

If a follow-up issue surfaces with ingestion-time partitioning specifically, the fix is one-line in v2_schema and the metadata provider inherits it.

### 3. Cache key shape

`(source_type, table_id)` vs `(source_type, bucket, source_table)`. Today `table_id` is unique within a registry, so they're equivalent. If two registry rows ever pointed at the same upstream table (local-mode for sync + remote-mode for ad-hoc), keying by tuple would dedupe the BQ call. **Provisional answer: `table_id`.** Duplicate-target case is hypothetical; KISS until somebody registers it.

### 4. `fetch_via` hint differentiation

Currently catalog says `agnes snapshot create <id>` for any non-local row. With the new size hint, the catalog could differentiate per bucket: `small`/`medium` → `agnes query --remote "..."`; `large`/`very_large` → `agnes snapshot create <id> --where '<predicate>'`. **Lean yes** — one-line conditional, surfaces actionable advice the analyst Claude already follows manually. Codify in implementation plan.

### 5. `--no-metadata` flag on `agnes catalog`?

**No** — the cache amortises the work, an opt-out is more knob than the operator needs. Reconsider only if telemetry shows real load.

### 6. `bq_config` health-check coordination

Reviewer flagged: when `bq_config` info-tier reports "BigQuery project not configured" (`app/api/health.py:64-66`), the metadata provider currently silently returns `None` rather than agreeing with the health check. Both signals exist; they should be consistent. **Resolved in design above** — provider's sentinel-config early-return (`if not bq.projects.data: return None`) reads the same `BqAccess.projects.data` truthy check that drives the health entry. They can't disagree because they share state. No code coordination needed.

---

## Implementation order

When this spec converts to a plan in `docs/superpowers/plans/`:

0. ~~**Live BigQuery verification.**~~ ✅ Done 2026-05-07. Outcome locked in Open Question §1 + §1a + §1b.
1. **Shared models** — `app/api/_metadata_models.py` with `MetadataRequest` + `TableMetadata`. Pure dataclass module. One commit.
2. **`KeboolaStorageClient.get_table_info` thin wrapper** — single function added + unit test mocking `_get`. One commit.
3. **Combined COLUMNS helper** — `connectors/bigquery/access.py:fetch_bq_columns_full` (single query for column list + partition + cluster). Refactor `v2_schema._fetch_bq_schema` + `_fetch_bq_table_options` to call it; no behavior change for `/api/v2/schema/{id}` consumers. Existing schema-endpoint tests pass unchanged; new test asserts only one BQ job per cache miss (count `bigquery_query` invocations on the mocked session).
4. **`build_schema` RBAC/cache split** — extract `build_schema_uncached(conn, table_id, *, bq)` containing the BQ work + cache write. `build_schema(...)` keeps the RBAC + cache-check at the top, then delegates. Existing endpoint behavior unchanged; new entry point is what warmup will call.
5. **Provider scaffold + dispatcher** — `app/api/v2_catalog.py:_metadata_provider_for` + `_build_metadata_request`. Stub providers in `connectors/<source>/metadata.py` returning `None`. Tests verify dispatch + identifier rejection + unknown-source fall-through.
6. **Keboola provider** — real `connectors/keboola/metadata.py:fetch` using `KeboolaStorageClient.get_table_info` + `KeboolaClient(token=None, url=None)` env-fallback. Tests cover happy / unconfigured / `StorageApiError`.
7. **BQ provider** — real `connectors/bigquery/metadata.py:fetch` using `fetch_bq_columns_full` (step 3) for partition/cluster + `_fetch_via_table_storage` / `_fetch_via_legacy_tables` for rows+size + `_resolve_bq_location`. Tests cover the 5 cases from Test plan (happy / sentinel / VIEW / region-typo / both-paths-fail).
8. **v2_catalog wiring** — `_size_hint_for_row` rename, dispatch on `query_mode='remote'`, response shape extension, 15-min `_metadata_cache`. Tests verify catalog response includes the new fields; cache hit/miss behavior; provider not dispatched for non-remote rows.
9. **Unified cache invalidation** — `v2_catalog.invalidate_for_table` helper that flushes all four caches and schedules a single-row re-warm. Wired into `admin.py:register_table` / `update_table` / `unregister_table`. Tests verify all flushes + that the re-warm task is scheduled.
10. **Cache warmup framework** — `app/api/cache_warmup.py` with `WarmupRunState` / `WarmupRowState` / `_warm_catalog_caches_bg` / `_warm_one`. The three `/api/admin/cache-warmup/{status,run,stream}` endpoints. SSE generator. Tests cover startup hook, bounded concurrency, failure isolation, idempotent `/run`, registry-change rewarm.
11. **`app/main.py` startup hook** — register `warm_catalog_caches` event handler. Test verifies readiness is not blocked + warmup runs to completion in background. Honors `AGNES_SKIP_CACHE_WARMUP=1`.
12. **CLI post-register hint** — `cli/commands/admin.py:register_table` adds the third hint when `query_mode=remote`. CLI test asserts the line appears.
13. **`docs/admin/query-modes.md`** — written end-to-end per the doc outline. Cross-references checked (RBAC.md, instance.yaml.example, BQ skill).
14. **Admin UI integration** — `admin_tables.html` cache toolbar `<section>`, per-row `col-status` badge, `EventSource` wiring + polling fallback, `?` icon on query_mode field. Smoke test asserts the markup is present.
15. **CHANGELOG + version bump** — `## [0.46.0] — YYYY-MM-DD`. Sections: Added (catalog response fields, /api/admin/cache-warmup/*, automatic startup warmup, admin UI cache panel, query-modes doc), Changed (cache-invalidation on register/update/unregister; BQ schema endpoint now does 1 BQ job per cache miss instead of 2), Internal. Bump `pyproject.toml` to `0.46.0`. Minor — new public catalog fields, new admin endpoints, new doc page.

Each step lands as one commit on the same branch. Reviewer can stop at any boundary if scope drifts. Steps 1-2 are pure scaffolding; steps 3-4 are independent refactors that ship value on their own (50% BQ-job reduction); steps 5-9 are the metadata feature core; steps 10-11 are warmup infrastructure; step 14 is the operator-visible UI surface.
