# Claude-Driven Fetch Primitives + Discovery + Agent Rails — Design

> **Goal:** Replace the broken "wrap a BQ view in a DuckDB master view" approach (issue #101) with a clean primitives-based model where Claude (the LLM agent) plans the work, and Agnes provides discovery + scoped fetch + local query primitives. No client-side SQL parsing. No GCP creds on the analyst laptop.

**Status:** Design — awaiting code review and implementation plan.

**Author:** ZS (with the in-house Claude agent)

**Related issues:** #101 (BQ view-wrapping doesn't push down outer queries), #91 (admin server-config), #96 (project_id validation, already shipped), #98 (token cache, already shipped)

---

## 1. Motivation

The current BigQuery view pipeline (shipped in branch `zs/test-bq-e2e`, PR #102) wraps each registered BQ view as:

```sql
CREATE VIEW "S1_session_landings" AS
  SELECT * FROM bigquery_query('proj', 'SELECT * FROM `proj.ds.S1_session_landings`')
```

This is correct in principle, but **fails at query time** for any non-trivial view:

```sql
SELECT COUNT(*) FROM S1_session_landings
-- DuckDB rewrites to:
-- SELECT COUNT(*) FROM (SELECT * FROM bigquery_query(...))
-- BigQuery sees the inner SELECT * as the literal job and tries to materialize 225M rows.
-- → "Response too large to return"
```

DuckDB's optimizer cannot push the outer `COUNT(*)` / `WHERE` / `LIMIT` into the opaque `bigquery_query()` table function. The wrap is therefore a near-zero-utility abstraction for any BQ view of meaningful size.

We considered four mitigations (issue #101 lists them: detect-attach for views, predicate templates, pre-materialize to BQ tables, drop-the-wrap). None of them is fully satisfying as a closed system, because **the agent (Claude) is already the smart planner in the loop**. The right answer is to expose primitive operations Claude can compose, with strong railsy in CLAUDE.md, instead of trying to make DuckDB look transparent through the wrong abstraction.

## 2. Architecture

### 2.1 Two-tier query model (unchanged)

```
┌─ analyst laptop ─────────────────┐    ┌─ Agnes server ───────────┐    ┌─ BigQuery
│                                  │    │                          │    │
│  Claude (agent) ── da CLI ──┐    │    │  FastAPI                 │    │
│                              │   │    │   ├─ /api/v2/catalog     │    │
│                              │   │    │   ├─ /api/v2/schema      │    │
│              ┌───────────────┴─┐ │    │   ├─ /api/v2/sample      │    │
│              ▼                 │ │    │   ├─ /api/v2/scan        │ ──►│
│       local DuckDB             │ │    │   └─ /api/v2/scan/estimate│   │
│       ~/agnes-data/.../        │ │    │                          │    │
│       user/duckdb/             │ │    │  server DuckDB           │    │
│       analytics.duckdb         │ │    │   + BQ secret            │    │
│         + parquet views        │ │    │   + RBAC                 │    │
│         + snapshot views ◄─────┼─┘    │   + safelist             │    │
│                                  │    │                          │    │
└──────────────────────────────────┘    └──────────────────────────┘    └─
```

Local DuckDB stays the analyst's interactive SQL surface. Server-side DuckDB is the BQ entrypoint — secrets stay there. The two are joined by **fetch operations** that materialize filtered subsets onto the laptop as DuckDB views over local parquet snapshots.

### 2.2 What changes vs today

- **Drop the `bigquery_query()` wrap view** in `connectors/bigquery/extractor.py`. BQ views still get registered in `_meta` for catalog purposes, but no master view is created in `analytics.duckdb`.
- **Add server endpoints** for catalog / schema / sample / scan / scan-estimate.
- **Add CLI primitives** for fetch + snapshot management + discovery.
- **Add CLAUDE.md instructions** that teach the agent the workflow.
- **Add a standalone skill** so the agent rails load automatically when working with Agnes.

`/api/query` and `/api/query/hybrid` stay; they remain useful for one-shot server-side aggregations and existing `da query --remote` flows.

## 3. Server endpoints

### 3.1 `GET /api/v2/catalog`

Returns the user-visible table catalog. Filtered by RBAC (`can_access_table`).

Response shape:

```json
{
  "tables": [
    {
      "id": "s1_session_landings",
      "name": "S1_session_landings",
      "description": "Session landings event view",
      "source_type": "bigquery",
      "query_mode": "remote",
      "sql_flavor": "bigquery",
      "where_examples": [
        "event_date > DATE '2026-01-01'",
        "country_code = 'CZ' AND platform = 'web'"
      ],
      "fetch_via": "da fetch s1_session_landings --select <cols> --where '<BQ predicate>' --limit <N>",
      "rough_size_hint": null
    },
    {
      "id": "orders",
      "name": "orders",
      "source_type": "keboola",
      "query_mode": "local",
      "sql_flavor": "duckdb",
      "fetch_via": "already local — query directly via `da query`",
      "rough_size_hint": "1.2k rows / 180 KB"
    }
  ],
  "server_time": "2026-04-27T17:30:00Z"
}
```

Cached server-side per user (TTL 5 min) since the catalog rarely changes mid-session.

### 3.2 `GET /api/v2/schema/{table_id}`

Returns column metadata + BQ flavor hints (when applicable).

Response shape:

```json
{
  "table_id": "s1_session_landings",
  "source_type": "bigquery",
  "sql_flavor": "bigquery",
  "columns": [
    {"name": "event_date", "type": "DATE", "nullable": false, "description": "partition column"},
    {"name": "session_id", "type": "STRING", "nullable": false},
    {"name": "country_code", "type": "STRING", "nullable": true, "description": "ISO 3166-1 alpha-2"}
  ],
  "partition_by": "event_date",
  "clustered_by": ["country_code"],
  "where_dialect_hints": {
    "date_literal": "DATE '2026-01-01'",
    "timestamp_literal": "TIMESTAMP '2026-01-01 00:00:00 UTC'",
    "interval_subtract": "DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)",
    "regex": "REGEXP_CONTAINS(field, r'pattern')",
    "cast": "CAST(x AS INT64)"
  }
}
```

Source for BQ tables: `bigquery_query()` against `INFORMATION_SCHEMA.COLUMNS` + `INFORMATION_SCHEMA.TABLE_OPTIONS` + dataset query. No data scan, sub-second.

Cached server-side per `table_id` (TTL 1 h, manual invalidate via `da catalog --refresh`).

### 3.3 `GET /api/v2/sample/{table_id}?n=5`

Returns N sample rows (default 5, max 100). For BQ: `bigquery_query('proj', 'SELECT * FROM ds.t LIMIT N')`. For local: read from parquet directly.

Response shape:

```json
{
  "table_id": "s1_session_landings",
  "rows": [
    {"event_date": "2026-04-27", "session_id": "...", "country_code": "CZ"},
    ...
  ],
  "source": "bigquery"
}
```

Cached server-side TTL 1 h, invalidated on table re-extract or admin force-refresh.

### 3.4 `POST /api/v2/scan`

The work primitive. Takes a single-table filtered fetch request, returns Arrow IPC stream.

Request shape:

```json
{
  "table_id": "s1_session_landings",
  "select": ["event_date", "country_code", "session_id"],
  "where": "event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) AND country_code = 'CZ'",
  "limit": 1000000,
  "order_by": ["event_date DESC"]
}
```

Response: Arrow IPC stream (HTTP body), schema in headers.

Server-side flow:

1. Auth + RBAC: user can read this table?
2. Validate `where` with `sqlglot` — single-table predicate, no DDL/DML, no semicolons, only allow-listed function names. Reject malformed, log + 400.
3. Validate `select` columns: each must exist in the table's schema (cross-checked with cached schema endpoint result).
4. Validate `limit` (max 10M rows hard cap, or per-instance config).
5. Build target SQL:
   - For `source_type=bigquery`: `SELECT [select] FROM \`{project}.{dataset}.{source_table}\` WHERE [where] ORDER BY [order_by] LIMIT [limit]`. Pass to `bigquery_query()` with the metadata token (#98 cache helps).
   - For `source_type=keboola` / `source_type=jira`: query the local parquet via DuckDB.
6. Stream Arrow IPC back. No materialization on server beyond the BQ jobs API result buffer.

Quotas:
- Per-user concurrent scan count (default 5 simultaneous).
- Per-user daily byte cap (configurable; typical: 50 GB).
- Tracked in `audit_log` per request.

### 3.5 `POST /api/v2/scan/estimate`

Same request shape as `/api/v2/scan`, but doesn't actually run the query. Uses BQ's `dryRun: true` flag to get scan size without paying for it.

Response shape:

```json
{
  "table_id": "s1_session_landings",
  "estimated_scan_bytes": 4400000000,
  "estimated_result_rows": 245000,
  "estimated_result_bytes": 12000000,
  "bq_cost_estimate_usd": 0.022
}
```

`estimated_scan_bytes` comes directly from BQ dry-run. `estimated_result_rows` is rough — BQ doesn't provide it on dry runs, so we estimate from `bytes_processed × selectivity_factor`. `estimated_result_bytes` derives from `result_rows × avg_row_bytes_from_schema`.

For `source_type` other than BQ, return zero/unknown for cost fields.

### 3.6 Caching layer

Server uses an in-process LRU + TTL cache for catalog/schema/sample. Cache invalidation:
- `POST /api/admin/catalog/invalidate` — admin force-refresh
- Auto-invalidate on `table_registry` mutations (after `register-table` / `unregister-table`)
- TTL: catalog 5 min, schema 1 h, sample 1 h

### 3.7 Server-side WHERE validator (sqlglot)

A focused module: `app/api/where_validator.py`. Surface ~80 LOC.

Rules:
- Parse with `sqlglot.parse_one(predicate, into=exp.Where, dialect="bigquery")`.
- Walk AST. Reject if any of:
  - `exp.Subquery`, `exp.Select` (no nested SELECTs)
  - `exp.SemicolonSegment` (no statement chaining)
  - `exp.Insert / Update / Delete / Drop / Truncate / Alter / Create / Copy` (DDL/DML)
  - References to tables other than the target (no JOINs)
  - Function calls outside an allow-list (date/time/string/math/comparison; no `BIGQUERY()`, `EXEC`, etc.)
- Pass-through fragments that match these constraints.

This is the only place sqlglot lives in the codebase. Constrained, testable, single responsibility.

## 4. CLI commands

### 4.1 Discovery

```
da catalog [--json] [--refresh]
da schema <table_id> [--json]
da describe <table_id> [-n N] [--json]
```

`da catalog` lists tables in a human-readable table by default. With `--json`, emits the API response verbatim — Claude reads this to understand what's available.

`da schema` shows columns + types + BQ flavor hints (when applicable).

`da describe` = schema + sample rows in one shot.

Client-side cache at `~/agnes-data/user/cache/`:
- `catalog.json` (5 min TTL, invalidated on `da sync` and `--refresh`)
- `schema/<table_id>.json` (1 h TTL)
- `samples/<table_id>.json` (1 h TTL)

### 4.2 Fetch + snapshot management

```
da fetch <table> \
    [--select <cols>] \
    [--where <predicate>] \
    [--limit <N>] \
    [--order-by <cols>] \
    [--as <name>] \
    [--estimate] \
    [--force]
```

Materializes a filtered subset locally as `~/agnes-data/user/snapshots/<name>.parquet`, registers `<name>` as a DuckDB view in `analytics.duckdb`, writes metadata to `~/agnes-data/user/snapshots/<name>.meta.json`.

Default `<name>` is `<table>` (overwrites previous snapshot of that table unless `--force` not given and snapshot exists — then prompt or error).

`--estimate` runs only the dry-run estimate, doesn't fetch. Prints scan bytes + result row/byte estimate + cost. Always shown before fetch unless `--no-estimate` is set.

```
da snapshot list [--json]            # name | rows | size | age | table_id | where
da snapshot refresh <name> [--where <new>]  # re-fetch with stored params
da snapshot drop <name>
da snapshot prune [--older-than 7d] [--larger-than 1g]
```

The metadata sidecar (`<name>.meta.json`) is the source of truth for `refresh`:

```json
{
  "name": "cz_recent",
  "table_id": "s1_session_landings",
  "select": ["event_date", "country_code", "session_id"],
  "where": "event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) AND country_code = 'CZ'",
  "limit": 1000000,
  "order_by": null,
  "fetched_at": "2026-04-27T17:30:00Z",
  "rows": 245832,
  "bytes_local": 8400000,
  "estimated_scan_bytes_at_fetch": 4400000000
}
```

### 4.3 Disk awareness

```
da disk-info [--json]
```

Output:

```
Snapshots dir:    ~/agnes-data/user/snapshots/
Used by Agnes:    2.4 GB across 7 snapshots
Free disk:        38.2 GB
Configured cap:   10 GB (~/.agnes/config: snapshot_quota_gb)
```

`snapshot_quota_gb` is a soft cap — `da fetch` warns if exceeded but doesn't hard-fail (analyst can override). `da snapshot prune --auto` honors the cap.

### 4.4 Existing commands stay

- `da query "..."` — local DuckDB query, fast, offline-capable. Works on local-mode tables and snapshots.
- `da query --remote "..."` — passthrough to `/api/query`. For one-shot aggregates, ad-hoc raw BQ-flavor SQL. (Will evolve into `da query-remote` for clarity.)
- `da sync` — refreshes local-mode parquets. Snapshot files don't get touched.

## 5. Claude rails (CLAUDE.md + skill)

### 5.1 CLAUDE.md addendum

A new section in the repo's CLAUDE.md:

```markdown
## Querying Agnes data — agent rails

When asked about ANY data in Agnes, follow this protocol.

### Discovery first

Before writing ANY query against a table, run:

    da catalog --json | jq <filter>     # know what's available
    da schema <table>                   # learn columns + types
    da describe <table> -n 5            # see real values for shape

NEVER write `SELECT * FROM <table>` blindly. For local-mode tables it's
wasteful; for remote-mode tables it can blow up at 225M rows.

### Choose the right tool

Tables in `da catalog` have a `query_mode`:

- **`local`**: data is on the laptop as parquet (synced via `da sync`).
  Query directly with `da query "SELECT … FROM <table>"`.

- **`remote`** (typically BigQuery): the parquet does NOT exist on the laptop.
  You MUST either:
  1. **`da fetch`** a filtered subset → query the local snapshot, OR
  2. **`da query-remote`** for one-shot server-side execution, OR
  3. **`da query --register-bq`** for hybrid joins (rarely needed).

### `da fetch` workflow (preferred for remote tables)

    # 1. estimate first
    da fetch s1_session_landings \
        --select event_date,country_code,session_id \
        --where "event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) 
                 AND country_code = 'CZ'" \
        --estimate
    # → "estimated_scan_bytes: 4.2 GB, result: ~250k rows, 12 MB locally"

    # 2. if reasonable, fetch
    da fetch s1_session_landings ... --as cz_recent

    # 3. query the local snapshot
    da query "SELECT event_date, COUNT(*) FROM cz_recent GROUP BY 1 ORDER BY 1"

### Heuristics for `da fetch`

- ALWAYS list specific columns in `--select`. Avoid implicit SELECT *.
- ALWAYS include a `--where` for remote tables; otherwise add `--limit`.
- ALWAYS run `--estimate` first when:
  - You're not sure of the data shape
  - The table has `partition_by` or `clustered_by` set (per `da schema`)
  - The fetch could plausibly exceed 1 GB local bytes
- Reuse `da snapshot list` before fetching — if a snapshot covers your
  query already, skip the fetch.

### BigQuery SQL flavor for `--where`

For `source_type=bigquery` (per `da catalog`):

- Date literal: `DATE '2026-01-01'` (NOT `'2026-01-01'::date`)
- Timestamp literal: `TIMESTAMP '2026-01-01 00:00:00 UTC'`
- Now: `CURRENT_DATE()`, `CURRENT_TIMESTAMP()`
- Date arithmetic: `DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)`
- Regex: `REGEXP_CONTAINS(col, r'pattern')` (raw string!)
- NULL: `col IS NOT NULL` (standard)
- Cast: `CAST(x AS INT64)` (NOT `INT`)

For `source_type=keboola` / `source_type=jira` (local), use DuckDB SQL flavor
in your `da query` calls — there's no `--where` on local since fetch is implicit.

### Snapshot hygiene

- Reuse snapshots across questions in the same conversation.
- Use descriptive names: `cz_recent`, `orders_q1_us`, `sessions_today`.
- Drop with `da snapshot drop <name>` when done with a topic.
- `da disk-info` to see total cache size.

### When NOT to use `da fetch`

- Single aggregate on remote table (`SELECT COUNT(*) FROM remote`):
  use `da query-remote "SELECT COUNT(*) FROM s1_session_landings"`.
  No materialization needed; cheap.
- Throwaway exploration with raw BQ syntax: `da query-remote`.
- Cross-table JOIN with both tables remote: combine `da fetch` for one
  side + `da query-remote` for the other; full cross-remote JOIN
  requires more thought (see #101 for design space).
```

### 5.2 Skill file

Standalone skill `agnes-data-querying` at `skills/agnes-data-querying.md` (loadable via the superpowers skill mechanism), which auto-activates when the user is in an Agnes-flavored project and asks data questions. Contents mirror the CLAUDE.md addendum but framed as a runnable workflow.

The skill is short — under 200 lines — and has a quick reference table of common BQ syntax gotchas.

## 6. Migration

### 6.1 Drop the wrap view

`connectors/bigquery/extractor.py::init_extract` currently emits:

```sql
CREATE OR REPLACE VIEW "<table_name>" AS
  SELECT * FROM bigquery_query('<project>', 'SELECT * FROM `<project>.<dataset>.<source_table>`')
```

Change: **don't emit any wrap view for VIEW-type entities**. The `_meta` row still gets written (so the orchestrator catalog has a record), and `_remote_attach` still gets the BQ entry (so the master DB can query via the secret), but no master-side view exists.

For BASE TABLE entities, keep the existing direct-ref view template — Storage Read API handles those fine.

Result: `analytics.duckdb` only has master views for source-type=keboola / source-type=jira / BQ-base-tables. BQ views are **not** queryable directly through `da query --remote "SELECT * FROM s1_session_landings"`. They MUST be either fetched or queried via `bigquery_query()` explicitly.

### 6.2 Backwards compatibility

Existing PRs against `zs/test-bq-e2e` ship the wrap-view code. This design replaces that. The migration:

- One commit drops the wrap-view code path in the extractor.
- One commit removes the orchestrator's `_attach_remote_extensions` BQ-secret refresh in cases where no BQ-typed view exists (it's still needed for BASE TABLE refs).
- Tests updated.

`/api/sync/manifest` already filters out `query_mode='remote'` tables for `da sync` (Task 6/7). Snapshot views are not in the manifest — they're laptop-local only.

### 6.3 Data already on dev VM

The dev VM has `s1_session_landings` registered as a remote-mode view. Post-migration:
- `analytics.duckdb` won't have a master view for it (existing wrap view will be dropped on next orchestrator rebuild).
- Claude is expected to use `da fetch` instead.

User's existing test workflow: `da fetch s1_session_landings --where ...` → snapshot → `da query`.

## 7. Out of scope

These are real concerns but explicitly NOT addressed in this design:

- **Cross-remote JOINs**: A query joining two remote BQ views directly. Workaround: fetch one side as a snapshot, then `da query-remote` with `bigquery_query()` for the other side. Long-term: see #101 follow-up "predicate templates" or "hosted Postgres bridge" alternatives.
- **Streaming results**: `da fetch` materializes the full Arrow buffer before writing to disk. For multi-GB fetches this can pause for tens of seconds. Future optimization: chunked Arrow stream → parquet writer pipe.
- **Async fetches**: `da fetch` is synchronous. No background mode. If fetch times out (default 5 min), user must retry.
- **Cross-org BQ**: assume one BQ project per Agnes deployment. Multi-project fan-out is a separate spec.
- **Custom DuckDB extension** (option A from brainstorming): not pursued because the primitives-based approach delivers 80% of the UX at 10% of the engineering cost. Revisit if production pain demands it.

## 8. Effort estimate

| Component | Owner | Days |
|-----------|-------|------|
| `/api/v2/scan` server endpoint + sqlglot WHERE validator | server | 1 |
| `/api/v2/scan/estimate` (BQ dryRun) | server | 1 |
| `/api/v2/catalog` + `/api/v2/schema` + `/api/v2/sample` | server | 1.5 |
| Server-side caching layer (LRU+TTL) | server | 0.5 |
| `da fetch` + snapshot metadata + refresh support | client | 1 |
| `da snapshot list/refresh/drop/prune` + `da disk-info` | client | 1 |
| `da catalog/schema/describe` (with SQL flavor info) | client | 1 |
| Arrow over HTTP serialization (pyarrow) | shared | 0.5 |
| Client-side cache at `~/agnes-data/user/cache/` | client | 0.5 |
| Drop wrap-view code path + tests | server | 0.5 |
| CLAUDE.md instructions + skill file | docs | 1 |
| Tests (unit + 1-2 integration tests against real BQ) | shared | 1.5 |
| **Total** | | **~10.5** |

Two developers in parallel could finish in ~5-6 calendar days. One developer: 2 weeks.

## 9. Risks & open questions

1. **WHERE validator coverage**: sqlglot may misclassify some BQ-specific functions (e.g., `ARRAY_AGG(DISTINCT x ORDER BY y)`). Allow-list will need iteration. Mitigation: explicit fall-through to "reject with clear error message; user retries with simpler predicate."
2. **Snapshot refresh staleness**: `da snapshot refresh` re-runs the same WHERE — if the WHERE used `CURRENT_DATE()`, the data shifts naturally. If it used a fixed literal (`DATE '2026-01-01'`), refresh is a no-op modulo upstream changes. Document this.
3. **BQ dry-run accuracy for scan estimate**: BQ reports `totalBytesProcessed` accurately, but our `estimated_result_rows` heuristic is approximate. Worst case: under-estimate → fetch larger than expected → disk usage warning fires. Acceptable.
4. **Per-user concurrent fetch limit**: hard cap (5) might frustrate power users running multiple parallel notebooks. Configurable per-user via admin UI (#91 follow-up). For v1, default is fine.
5. **Multi-conversation snapshots**: one user's snapshots persist across `da` invocations. Multiple Claude sessions sharing the same machine could collide on `--as <name>`. Snapshot list is shared scope; OK for now since dev VMs are per-user.

## 10. Success criteria

- [ ] Claude can ask "show me X session count for last 30 days" against `s1_session_landings` and produce an answer in <30 s without "Response too large" errors.
- [ ] Server-side `/api/v2/scan` rejects malicious WHERE clauses with clear error messages (verified by 5+ unit tests).
- [ ] `da catalog --json` output is machine-readable and includes `sql_flavor` per table.
- [ ] `da fetch --estimate` outputs both BQ scan bytes and local result bytes.
- [ ] `da snapshot list` shows current cache with sizes and ages.
- [ ] CLAUDE.md instructions are followed by a fresh Claude session without explicit prompting (verified by 3 different unguided agent runs).
- [ ] All existing tests still green (CI on branch).
- [ ] Demo: end-to-end agent flow on dev VM shows the full discover → estimate → fetch → query loop in <2 min wall time.
