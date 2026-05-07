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

The previous spec proposed `_invalidate_metadata_cache(table_id)` on register/update. **That alone is insufficient.** `app/api/v2_catalog.py:25` already runs a 5-minute `_table_rows_cache` over the registry rows themselves (the table list, before per-table metadata enrichment). On current main, that cache is **not** invalidated by `register_table` / `update_table` (verified: `admin.py:1037,1110,2771` only call `app.instance_config.reset_cache()`). An admin who registers a remote table immediately runs `agnes catalog` and sees no row at all for up to 5 minutes — not just missing size, missing the whole row. This is a pre-existing bug the new metadata cache would otherwise inherit.

Fix in this PR by introducing a single helper that owns both caches:

```python
# app/api/v2_catalog.py (addition)

def invalidate_for_table(table_id: str) -> None:
    """Drop both the registry-rows cache and the per-table metadata cache
    so the next catalog request reflects the just-registered / updated /
    unregistered row immediately. The catalog module owns this so admin.py
    doesn't need to know which caches exist.
    """
    _table_rows_cache.invalidate_all()  # whole-list cache; can't precisely invalidate one row
    _metadata_cache.invalidate(table_id)
```

Wire it into `app/api/admin.py`:

- `POST /api/admin/register-table` — call after the registry write succeeds, before returning.
- `PUT /api/admin/registry/{id}` — call after the row update.
- `DELETE /api/admin/registry/{id}` — call after unregister (otherwise an unregistered row keeps appearing in `agnes catalog` for up to 5 minutes; same UX bug, opposite direction).

Three call sites, one shared helper. Keeps cache knowledge in `v2_catalog.py` and out of `admin.py`.

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

### Admin UI — minimal change

`app/web/templates/admin_tables.html` — add a `?` icon next to the `query_mode` selector linking to the new doc page. **No** redesign of the form, **no** inline guidance panel — adding a few hundred lines of UI copy here would bloat a 132-KB template that's already overdue for a structural pass. Tooltip + doc link is the right scope today. The form itself already has enough validation (#177's PUT/DELETE work, the materialised-mode source_query check, the BQ shape validator in `_validate_bigquery_register_payload`) that an admin who reads the doc can drive it.

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

- `connectors/bigquery/metadata.py` — `fetch(row)` returning `TableMetadata | None`.
- `connectors/keboola/metadata.py` — same shape, Storage API path.
- `app/api/_metadata_models.py` — single shared module for `MetadataRequest` + `TableMetadata`. Lives under `app/api/` (not `connectors/`) because the primary consumer is `app/api/v2_catalog.py` and the providers (which sit one layer down in `connectors/`) import upward into the API layer rather than the reverse. This avoids the layering inversion of having `app/api/v2_catalog.py` import from `connectors/__init__.py`.
- `tests/test_connectors_bigquery_metadata.py` — unit tests with mocked `bq.duckdb_session`.
- `tests/test_connectors_keboola_metadata.py` — unit tests with mocked `KeboolaStorageClient`.
- `tests/test_v2_catalog_remote_metadata.py` — integration test against the catalog endpoint with a registered remote row, mocked provider returning a `TableMetadata`. Verifies the response shape and the cache-bust path.

### Edited files

- `app/api/v2_catalog.py` — rename `_materialized_size_hint` → `_size_hint_for_row`, add provider dispatch, add `_metadata_cache` (TTLCache, 15 min), extend response shape with the new fields. ~50 LOC delta.
- `app/api/admin.py` — wire `v2_catalog.invalidate_for_table(table_id)` into the success path of `register_table`, `update_table`, and `unregister_table`. ~6 LOC.
- `cli/commands/admin.py` — extend the post-register hint as above. ~5 LOC.
- `app/web/templates/admin_tables.html` — `?` icon + doc link next to `query_mode`. ~10 LOC.

### Schema / DB / config

**No schema migration.** All metadata is computed on demand from BigQuery / Keboola Storage API. We deliberately don't persist it (would add a bookkeeping problem — staleness, invalidation, schema bumps).

**No new env vars.** All the required config (`data_source.bigquery.*`, `data_source.keboola.storage_*`) already exists for the connectors.

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
| Cache-bust | `register_table` flushes both `_table_rows_cache` and `_metadata_cache`; `update_table` same; `unregister_table` same. After bust, next catalog request reflects the new state immediately. |
| `agnes catalog` CLI | smoke test that the new fields surface in `--json` output and don't break the text-mode renderer |
| Sample endpoint | smoke test against a registered remote BQ row; verify it returns sample rows. If broken, separate fix path; not bundled in this PR's scope |

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

0. ~~**Live BigQuery verification.**~~ ✅ Done 2026-05-07. Verified on `prj-grp-foundryai-dev-7c37` (us-central1) against `audrius_test.product_inventory`. Outcome locked in Open Question §1 + §1a + §1b. Chosen path: `<project>.region-<location>.INFORMATION_SCHEMA.TABLE_STORAGE`, region from `data_source.bigquery.location` with two fallback layers. Partition/cluster path inherits v2_schema's existing tested behavior (§2).
1. **Shared models** — `app/api/_metadata_models.py` with `MetadataRequest` + `TableMetadata`. Pure dataclass module, no behavior. One commit, no tests beyond construction.
2. **`KeboolaStorageClient.get_table_info` thin wrapper** — single function added to existing module + a unit test mocking the underlying `_get`. One commit.
3. **Provider scaffold + dispatcher** — `app/api/v2_catalog.py:_metadata_provider_for` + `_build_metadata_request`. Stub providers in `connectors/<source>/metadata.py` returning `None`. Tests verify: dispatch returns the right callable per source_type; `_build_metadata_request` rejects bad identifiers; unknown source_type returns `None` callable.
4. **Keboola provider** — real implementation backed by `KeboolaStorageClient.get_table_info`. Tests with mocked client returning canned `rowsCount`/`dataSizeBytes`; failure path returns `None`; unconfigured returns `None`.
5. **BQ provider** — real implementation using the locked-in INFORMATION_SCHEMA view from step 0. Tests with mocked `bq.duckdb_session().execute().fetchall()`; partition + cluster path, rows + size path, sentinel early-return, error path, ingestion-time pseudo-column case (per step 0 verification).
6. **v2_catalog wiring** — `_size_hint_for_row` rename, dispatch on `query_mode='remote'`, response shape extension (rows/size_bytes/partition_by/clustered_by fields), 15-min `_metadata_cache`. Tests verify the catalog response includes the new fields; cache hits don't re-call provider; cache miss after 15 min does.
7. **Unified cache invalidation** — `v2_catalog.invalidate_for_table` helper + wiring into `admin.py:register_table` / `update_table` / `unregister_table`. Tests verify register/update/unregister all flush both caches; next catalog request shows the new state.
8. **CLI post-register hint** — `cli/commands/admin.py:register_table` adds the third hint when `query_mode=remote`. CLI test asserts the line appears.
9. **`docs/admin/query-modes.md`** — written end-to-end per the doc outline. Cross-references checked (RBAC.md, instance.yaml.example, BQ skill).
10. **Admin UI tooltip** — `?` icon next to query_mode selector in `admin_tables.html`, links to the new doc page. ~10 LOC.
11. **CHANGELOG + version bump** — `## [0.46.0] — YYYY-MM-DD`. Sections: Added (catalog response fields, query-modes doc), Changed (cache-invalidation behavior on register/update/unregister), Internal. Bump `pyproject.toml` to `0.46.0`. Minor (not patch) — new public catalog fields + new doc page.

Each step lands as one commit on the same branch. Reviewer can stop at any boundary if scope drifts.
