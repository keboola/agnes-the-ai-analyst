# Claude-Driven Fetch Primitives + Discovery + Agent Rails — Design

> **Goal:** Replace the broken "wrap a BQ view in a DuckDB master view" approach (issue #101) with a clean primitives-based model where Claude (the LLM agent) plans the work, and Agnes provides discovery + scoped fetch + local query primitives. No client-side SQL parsing. No GCP creds on the analyst laptop.

**Status:** Design — awaiting code review and implementation plan.

**Author:** ZS (with the in-house Claude agent)

**Related issues:** #101 (BQ view-wrapping doesn't push down outer queries), #91 (admin server-config), #96 (project_id validation, already shipped), #98 (token cache, already shipped)

---

## 1. Motivation

The current BigQuery view pipeline (shipped in branch `zs/test-bq-e2e`, PR #102) wraps each registered BQ view as:

```sql
CREATE VIEW "web_sessions_example" AS
  SELECT * FROM bigquery_query('proj', 'SELECT * FROM `proj.ds.web_sessions_example`')
```

This is correct in principle, but **fails at query time** for any non-trivial view:

```sql
SELECT COUNT(*) FROM web_sessions_example
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

### 3.0 Identifier conventions (applies to all v2 endpoints)

`table_id` is the **registry primary key** (`table_registry.id`) verbatim — lowercase ASCII, alphanumeric + underscore, ≤64 chars, validated by `src/sql_safe.py::validate_identifier`. The display name (`table_registry.name`) may differ in case but is NOT a query key. CLI commands accept `table_id` only. The registry `register-table` endpoint already lowercases id at insert time, which is the canonical normalization point.

### 3.1 `GET /api/v2/catalog`

Returns the user-visible table catalog. Filtered by RBAC (`can_access_table`, table-grain). The user must have an explicit `dataset_permissions` row OR the table must be `is_public=true` OR the user must be `admin`.

Response shape:

```json
{
  "tables": [
    {
      "id": "web_sessions_example",
      "name": "web_sessions_example",
      "description": "Session landings event view",
      "source_type": "bigquery",
      "query_mode": "remote",
      "sql_flavor": "bigquery",
      "where_examples": [
        "event_date > DATE '2026-01-01'",
        "country_code = 'CZ' AND platform = 'web'"
      ],
      "fetch_via": "da fetch web_sessions_example --select <cols> --where '<BQ predicate>' --limit <N>",
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
  "table_id": "web_sessions_example",
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
  "table_id": "web_sessions_example",
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
  "table_id": "web_sessions_example",
  "select": ["event_date", "country_code", "session_id"],
  "where": "event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) AND country_code = 'CZ'",
  "limit": 1000000,
  "order_by": ["event_date DESC"]
}
```

Response: Arrow IPC stream (HTTP body), schema in headers.

**RBAC scope (v1):** **table-grain parity with `/api/query`** — same `can_access_table(user, table_id, conn)` check. **No column-level or row-level access control in v1.** A user who can read the table can fetch any subset of columns and rows from it. Column/row-level RBAC is deferred to a follow-up; if added, it would extend `dataset_permissions` with `column_allowlist` and `row_predicate` fields and the validator would augment user-supplied `where` with a server-pinned predicate.

Server-side flow:

1. Auth: PAT or session → resolved user.
2. RBAC: `can_access_table(user, table_id)` — same gate as `/api/query`. 403 on deny.
3. Validate `where` with the focused validator in §3.7 (sqlglot-backed). Reject malformed → 400 with structured error.
4. Validate `select` columns: each must exist in the table's schema (cross-checked against cached schema endpoint result). 400 on unknown column.
5. Validate `limit` against `instance.yaml: api.scan.max_limit` (hard cap, default 10_000_000). 400 if exceeded.
6. Quota check (§3.8). 429 if exceeded.
7. Build target SQL:
   - For `source_type=bigquery`: `SELECT [select] FROM \`{project}.{dataset}.{source_table}\` WHERE [where] ORDER BY [order_by] LIMIT [limit]`. Pass to `bigquery_query()` with the metadata token (#98 cache helps).
   - For `source_type=keboola` / `source_type=jira`: query the local parquet via DuckDB.
8. Enforce **`max_result_bytes`** guard (`instance.yaml: api.scan.max_result_bytes`, default 2 GB). If the cumulative Arrow stream exceeds this, abort and return partial result with `X-Agnes-Truncated: true` header + warning log. Prevents a single fetch from OOMing the server worker.
9. **Stream Arrow IPC** back over HTTP. Server emits chunks as BQ delivers them; client buffers entire stream into a parquet file before exposing as DuckDB view (no streaming on the client side in v1 — see §7 deferred). Content-Type: `application/vnd.apache.arrow.stream`.
10. Append `audit_log` row per request (§10.1).

### 3.5 `POST /api/v2/scan/estimate`

Same request shape as `/api/v2/scan`, but doesn't actually run the query. Uses BQ's `dryRun: true` flag to get scan size without paying for it.

Response shape:

```json
{
  "table_id": "web_sessions_example",
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

A focused module: `app/api/where_validator.py`. The **load-bearing security perimeter** of `/api/v2/scan`. Targeting ~250 LOC + adversarial test corpus.

#### Parser

Parse with `sqlglot.parse_one(f"WHERE {predicate}", into=exp.Where, dialect="bigquery")`. Reject if parse fails.

#### Structural rejects

Walk AST and reject on any of:
- `exp.Subquery`, `exp.Select` — no nested SELECTs (prevents `WHERE x IN (SELECT ... FROM other_table)` exfiltration)
- Multiple statements (semicolon chaining)
- DDL/DML nodes: `Insert`, `Update`, `Delete`, `Drop`, `Truncate`, `Alter`, `Create`, `Copy`, `Merge`
- `exp.Column` references where the qualifier is anything other than the target `table_id` or unqualified
- Star expressions (`*`) outside aggregates
- Bytes/binary literals raw embedding
- Comments (`--` or `/* */`) — strip in pre-processing or reject

#### Function allow-list (v1, BigQuery dialect)

Allowed function categories. The list is the **explicit** v1 contract; expanding it requires a spec amendment.

| Category | Functions |
|----------|-----------|
| Comparison | `=`, `!=`, `<`, `<=`, `>`, `>=`, `IS NULL`, `IS NOT NULL`, `IN`, `NOT IN`, `BETWEEN`, `LIKE`, `NOT LIKE` |
| Boolean | `AND`, `OR`, `NOT`, `XOR` |
| Date/Time | `CURRENT_DATE`, `CURRENT_TIMESTAMP`, `CURRENT_TIME`, `DATE`, `DATETIME`, `TIMESTAMP`, `TIME`, `DATE_ADD`, `DATE_SUB`, `DATE_DIFF`, `DATE_TRUNC`, `EXTRACT`, `FORMAT_DATE`, `FORMAT_TIMESTAMP`, `PARSE_DATE`, `PARSE_TIMESTAMP`, `UNIX_SECONDS`, `UNIX_MILLIS` |
| String | `CONCAT`, `LENGTH`, `LOWER`, `UPPER`, `SUBSTR`, `SUBSTRING`, `TRIM`, `LTRIM`, `RTRIM`, `REPLACE`, `STARTS_WITH`, `ENDS_WITH`, `CONTAINS_SUBSTR`, `REGEXP_CONTAINS`, `REGEXP_EXTRACT`, `SAFE_CAST` |
| Math | `ABS`, `CEIL`, `FLOOR`, `ROUND`, `MOD`, `POWER`, `SQRT`, `LOG`, `LN`, `EXP`, `SIGN`, `GREATEST`, `LEAST` |
| Casts | `CAST` (target types: `INT64`, `FLOAT64`, `NUMERIC`, `STRING`, `BYTES`, `BOOL`, `DATE`, `DATETIME`, `TIMESTAMP`, `TIME`, `DECIMAL`, `BIGNUMERIC`) |
| Conditional | `IF`, `IFNULL`, `COALESCE`, `NULLIF`, `CASE` |

Any function not on this list is rejected with `unknown_function` error including the function name. We avoid:
- `EXTERNAL_QUERY` (data exfiltration)
- `SESSION_USER`, `CURRENT_USER`, `IS_MEMBER` (impersonation surface)
- `ML.*` (cost surprise — ML predictions are billed by row)
- `ARRAY_AGG`, `STRING_AGG` and all aggregates (predicate context, not aggregate context)
- User-defined functions and table-valued functions
- `ROW_NUMBER`, window functions (predicate context)
- BQ scripting (`BEGIN`, `LOOP`, etc.)

#### Identifier-path validation

Column references in BigQuery can be dotted (`record.subfield.leaf`) or indexed (`array[OFFSET(0)]`). The validator must:
- Walk every `exp.Column` reference
- For each path segment, validate against the cached schema (paths must be present in `INFORMATION_SCHEMA.COLUMNS` field-shape data, not just top-level columns)
- Reject array subscripts containing function calls (e.g. `array[OFFSET(SAFE_CAST(x AS INT64))]` — too clever, overrun risk)

#### Adversarial test corpus

Mandatory test cases the implementer must add (`tests/test_where_validator.py`):
- 20+ accepted predicates (typical analyst-written WHEREs across all function categories)
- 30+ rejected predicates with explicit rejection codes:
  - `nested_select`: `x IN (SELECT y FROM t)`
  - `multi_statement`: `x = 1; DROP TABLE t`
  - `ddl_in_predicate`: `x = (CREATE TABLE t (id INT))`
  - `external_query`: `x = EXTERNAL_QUERY('...')`
  - `unknown_function`: `x = OBSCURE_BUILTIN(y)`
  - `comment_inject`: `x = 1 -- AND y > 0`
  - `wildcard_expansion`: `* = 5`
  - `cross_table_ref`: `other_table.id = 1`
  - `bytes_literal_raw`: `x = b'\\x00...'`
  - And 20+ more permutations

This is the only place sqlglot lives in the codebase. Constrained, testable, single responsibility. **All decisions are explicit and listed**; no "trust sqlglot's defaults".

### 3.8 Quota architecture (v1: process-local)

`/api/v2/scan` quotas live in **process-local memory** for v1. This is a **deliberate trade-off** documented here:

- Per-user concurrent scan: in-memory dict keyed by user_id, value is `set[request_id]`. Default cap: 5. Configurable via `instance.yaml: api.scan.max_concurrent_per_user`.
- Per-user daily byte cap: same dict, value also tracks `bytes_today` + `last_reset_utc`. Reset at UTC midnight. Default: 50 GB. Configurable via `instance.yaml: api.scan.max_daily_bytes_per_user`.

**Multi-replica caveat:** if Agnes is deployed with N FastAPI replicas, each tracks quotas independently — effectively **N× the cap** is the enforced ceiling per user. **Document this in §9 risks and CHANGELOG.** A future v2 with horizontal scale must move quotas to durable storage (recommend: a `quota_state` row in `system.duckdb` mutated under `BEGIN; UPDATE … RETURNING; COMMIT;` per request — or shared Redis if Agnes ever takes a Redis dependency).

429 response shape:

```json
{
  "error": "quota_exceeded",
  "kind": "concurrent_scans" | "daily_bytes",
  "current": 5,
  "limit": 5,
  "retry_after_seconds": 0      // for daily_bytes: seconds until UTC midnight
}
```

CLI translates 429 to exit code 3 with a clear message (§10.3).

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
    [--no-estimate] \
    [--force]
```

Materializes a filtered subset locally as `~/agnes-data/user/snapshots/<name>.parquet`, registers `<name>` as a DuckDB view in `analytics.duckdb`, writes metadata to `~/agnes-data/user/snapshots/<name>.meta.json`.

**`--as <name>` semantics (no interactive prompts ever):**
- Default `<name>` is `<table_id>`.
- If snapshot `<name>` already exists: **fail with exit code 6** (`snapshot_exists`) and a clear message naming the existing snapshot's `fetched_at` / `rows`.
- `--force` overwrites unconditionally. No confirmation prompt; agents can't answer prompts reliably.
- `--no-confirm` is unnecessary — there are no prompts.

**Snapshot install is file-locked.** The write transaction (move parquet into place + `CREATE OR REPLACE VIEW` + write meta sidecar) acquires an exclusive `flock(2)` on `~/agnes-data/user/snapshots/.lock` for the duration. Concurrent `da fetch` invocations queue. Concurrent reads (`da query`) take a shared lock on the analytics.duckdb file via DuckDB's own concurrency model — they're not blocked by snapshot install (DuckDB allows concurrent readers, and `CREATE OR REPLACE VIEW` is metadata-only fast).

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
  "table_id": "web_sessions_example",
  "select": ["event_date", "country_code", "session_id"],
  "where": "event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) AND country_code = 'CZ'",
  "limit": 1000000,
  "order_by": null,
  "fetched_at": "2026-04-27T17:30:00Z",
  "effective_as_of": "2026-04-27T17:30:00Z",   // server eval time of CURRENT_DATE() etc.
  "rows": 245832,
  "bytes_local": 8400000,
  "estimated_scan_bytes_at_fetch": 4400000000,
  "result_hash_md5": "abc123..."                // for refresh diff detection
}
```

**Refresh staleness UX:**

`da snapshot refresh <name>` re-runs the stored fetch with the same `where`. Behavior:

1. WHERE may contain time-relative constructs (`CURRENT_DATE()`, `INTERVAL N DAY`). Server re-evaluates them at refresh time. The new sidecar gets a fresh `effective_as_of`.
2. After refresh, CLI prints a **diff summary**:
   ```
   Refreshed cz_recent
     rows:           245 832  →  248 401   (+2 569)
     bytes_local:    8.4 MB   →  8.5 MB
     effective_as_of: 2026-04-27 17:30 UTC  →  2026-04-28 09:00 UTC
     identical:      no
   ```
3. If `result_hash_md5` matches (rows + content didn't change), print `identical: yes` and skip the parquet swap.
4. If snapshot is older than `~/.agnes/config: snapshot_stale_warn_days` (default 7), `da query` prints a one-line warning when the snapshot is referenced: `WARN: snapshot 'cz_recent' is 12 days old; consider 'da snapshot refresh cz_recent'`.

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
- `da query --remote "..."` — passthrough to `/api/query`. For one-shot aggregates, ad-hoc raw BQ-flavor SQL.
- `da sync` — refreshes local-mode parquets. Snapshot files don't get touched.

**v1 keeps `da query --remote` as-is.** A future rename to `da query-remote` (subcommand instead of flag, for clarity) is OUT OF SCOPE for this spec; track separately if desired.

`da catalog --refresh` clears the **client-side** cache only (forces next call to fetch fresh from server). It does NOT call the admin invalidate endpoint — that requires admin role (separate `da admin catalog-refresh` for admins).

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
  2. **`da query --remote`** for one-shot server-side execution, OR
  3. **`da query --register-bq`** for hybrid joins (rarely needed).

### `da fetch` workflow (preferred for remote tables)

    # 1. estimate first
    da fetch web_sessions_example \
        --select event_date,country_code,session_id \
        --where "event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) 
                 AND country_code = 'CZ'" \
        --estimate
    # → "estimated_scan_bytes: 4.2 GB, result: ~250k rows, 12 MB locally"

    # 2. if reasonable, fetch
    da fetch web_sessions_example ... --as cz_recent

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
  use `da query --remote "SELECT COUNT(*) FROM web_sessions_example"`.
  No materialization needed; cheap.
- Throwaway exploration with raw BQ syntax: `da query --remote`.
- Cross-table JOIN with both tables remote: combine `da fetch` for one
  side + `da query --remote` for the other; full cross-remote JOIN
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

Result: `analytics.duckdb` only has master views for source-type=keboola / source-type=jira / BQ-base-tables. BQ views are **not** queryable directly through `da query --remote "SELECT * FROM web_sessions_example"`. They MUST be either fetched or queried via `bigquery_query()` explicitly.

### 6.2 Backwards compatibility

Existing PRs against `zs/test-bq-e2e` ship the wrap-view code. This design replaces that. The migration:

- One commit drops the wrap-view code path in the extractor.
- One commit removes the orchestrator's `_attach_remote_extensions` BQ-secret refresh in cases where no BQ-typed view exists (it's still needed for BASE TABLE refs).
- Tests updated.

`/api/sync/manifest` already filters out `query_mode='remote'` tables for `da sync` (Task 6/7). Snapshot views are not in the manifest — they're laptop-local only.

### 6.3 Data already on dev VM

The dev VM has `web_sessions_example` registered as a remote-mode view. Post-migration:
- `analytics.duckdb` won't have a master view for it (existing wrap view will be dropped on next orchestrator rebuild).
- Claude is expected to use `da fetch` instead.

User's existing test workflow: `da fetch web_sessions_example --where ...` → snapshot → `da query`.

## 7. Out of scope

These are real concerns but explicitly NOT addressed in this design:

- **Cross-remote JOINs**: A query joining two remote BQ views directly. Workaround: fetch one side as a snapshot, then `da query --remote` with `bigquery_query()` for the other side. Long-term: see #101 follow-up "predicate templates" or "hosted Postgres bridge" alternatives.
- **Streaming results**: `da fetch` materializes the full Arrow buffer before writing to disk. For multi-GB fetches this can pause for tens of seconds. Future optimization: chunked Arrow stream → parquet writer pipe.
- **Async fetches**: `da fetch` is synchronous. No background mode. If fetch times out (default 5 min), user must retry.
- **Cross-org BQ**: assume one BQ project per Agnes deployment. Multi-project fan-out is a separate spec.
- **Custom DuckDB extension** (option A from brainstorming): not pursued because the primitives-based approach delivers 80% of the UX at 10% of the engineering cost. Revisit if production pain demands it.

## 8. Effort estimate

| Component | Owner | Days |
|-----------|-------|------|
| `/api/v2/scan` endpoint + RBAC + quota wiring | server | 1 |
| WHERE validator (§3.7) + adversarial test corpus (50+ cases) | server | 2 |
| `/api/v2/scan/estimate` (BQ dryRun via `google.cloud.bigquery` client) | server | 1.5 |
| `/api/v2/catalog` + `/api/v2/schema` + `/api/v2/sample` + caching | server | 2 |
| Audit log shape + `audit_log` migration if needed | server | 0.5 |
| `da fetch` + snapshot metadata + file-locked install | client | 1.5 |
| `da snapshot list/refresh/drop/prune` + diff summary + stale warn | client | 1.5 |
| `da catalog/schema/describe/disk-info` (with SQL flavor info) | client | 1 |
| Arrow streaming server-side, parquet write client-side | shared | 1 |
| Client-side cache at `~/agnes-data/user/cache/` | client | 0.5 |
| Drop wrap-view code path + migrate existing tests | server | 0.5 |
| CLAUDE.md instructions + skill file (with BQ flavor table + recovery prompts) | docs | 1 |
| Tests — unit (validator, quotas, RBAC) + integration (snapshot lifecycle, real BQ) | shared | 3 |
| **Total** | | **~16.5** |

Realistic timelines:
- **Two developers in parallel:** 8-9 calendar days (server+CLI tracks).
- **One developer:** ~3 weeks.

The estimate **revised upward from 10.5** based on review feedback (validator alone is ~2 d not 1; tests ~3 d not 1.5; estimate dryRun is more involved than `bigquery_query()` can do directly — needs `google.cloud.bigquery` client path).

## 9. Risks & open questions

1. **WHERE validator coverage**: the v1 allow-list (§3.7) is finite; legitimate analyst predicates may be rejected on first cut. Mitigation: explicit `unknown_function` error names the function so the analyst (or Claude) immediately sees what to drop. Allow-list expanded in follow-ups based on production rejection logs.
2. **Snapshot refresh staleness**: `CURRENT_DATE()` re-evaluates at refresh time → data shifts. Fixed literals → refresh is a content no-op (caught by `result_hash_md5` comparison per §4.2). Documented in CLI output (`identical: yes/no`).
3. **BQ dry-run accuracy for scan estimate**: `totalBytesProcessed` is accurate. `estimated_result_rows` is heuristic — worst case under-estimate → user fetches more than expected → max_result_bytes guard truncates (§3.4 step 8) with `X-Agnes-Truncated` header.
4. **Multi-replica quota**: process-local quotas (§3.8) mean N replicas → effective N×cap per user. **Documented caveat for v1.** Single-replica deployments (today's default) unaffected. Horizontal scale upgrade path: durable counter in `system.duckdb`. Captured as a follow-up issue when scale demand emerges.
5. **Multi-conversation snapshots collision**: per §4.2 file-locked install + exit-code-6-on-exists semantics make this safe — concurrent `da fetch --as same_name` causes the second to fail-fast with a clear error rather than corrupt state.
6. **BREAKING change for `da query --remote`**: dropping the wrap view (§6.1) means `da query --remote "SELECT * FROM <bq_view>"` no longer works. Existing automation scripts may break. **Must be flagged as `**BREAKING**` in CHANGELOG** per the project's changelog discipline. Mitigation: optional `--legacy-wrap-views` flag in `connectors/bigquery/extractor.py` for one release cycle to ease rollout (operator-controlled via `instance.yaml: bigquery.legacy_wrap_views: true`). Document in §6.

## 10. Implementation contracts

These are the concrete artifacts the implementer must produce; spec requirements distilled into checkable shapes.

### 10.1 Audit log shape

Every `/api/v2/scan` and `/api/v2/scan/estimate` request appends one row to the existing `audit_log` table:

```
event_type     = 'fetch_scan' | 'fetch_estimate'
user_email     = <from session/PAT>
table_id       = <request.table_id>
event_data     = JSON: {
                   select: [...],
                   where_hash: md5(where || ''),  -- not full text, can be sensitive
                   limit: ...,
                   estimated_scan_bytes: ... | null,
                   actual_result_rows: ... | null,
                   actual_result_bytes: ... | null,
                   latency_ms: ...,
                   status: 'ok' | 'rejected' | 'quota_exceeded' | 'truncated' | 'error',
                   error_kind: 'validator' | 'rbac' | 'bq' | null
                 }
```

Why `where_hash` instead of full text: WHERE clauses can include sensitive constants (user emails, IDs). Hash + structure remains debuggable from the validator's per-request log lines if needed, without persistent disclosure.

### 10.2 CLI exit codes

`da fetch`, `da snapshot *`, `da catalog`, `da schema`, `da describe`, `da disk-info` follow this exit-code contract (used by the agent to branch):

| Code | Meaning |
|------|---------|
| 0 | Success |
| 2 | Validation failed (bad WHERE, unknown column, malformed args) |
| 3 | Quota exceeded (concurrent or daily) |
| 4 | Disk full (snapshot file write failed; soft quota only emits a warning, not an exit) |
| 5 | Server error (5xx; transient) |
| 6 | Snapshot already exists (use `--force`) |
| 7 | Auth failed (no PAT, expired) |
| 8 | RBAC denied (table not accessible) |
| 9 | Network unreachable (server down) |

Each non-zero exit also writes a structured error to stderr (§10.3).

### 10.3 Error UX

CLI error format on stderr (single line + optional next-step hint):

```
Error: <one-line summary>. <Hint about how to recover.>
```

Examples:

```
Error: WHERE validator rejected 'unknown_function'. Function 'OBSCURE_FN' not in v1 allow-list.
       See: da catalog --json | jq '.tables[].sql_flavor' for the supported dialect.

Error: quota exceeded (daily_bytes). Used 51.2 GB of 50 GB cap (resets at 00:00 UTC).
       Hint: 'da snapshot list' to find oversized snapshots, 'da snapshot prune'.

Error: snapshot 'cz_recent' already exists (fetched 2 days ago, 245k rows).
       Pass --force to overwrite, or 'da snapshot refresh cz_recent' to update in place.
```

Server `/api/v2/*` errors return JSON:

```json
{
  "error": "validator_rejected",
  "kind": "unknown_function",
  "details": { "function": "OBSCURE_FN" },
  "request_id": "..."
}
```

`request_id` lets server-side log correlation work without exposing internal stack traces to clients.

### 10.4 Server config knobs (`instance.yaml`)

New section:

```yaml
api:
  scan:
    max_limit: 10000000           # rows
    max_result_bytes: 2147483648  # 2 GB
    max_concurrent_per_user: 5
    max_daily_bytes_per_user: 53687091200  # 50 GB
    bq_cost_per_tb_usd: 5.00      # for cost estimate output
    request_timeout_seconds: 300
  catalog_cache_ttl_seconds: 300  # 5 min
  schema_cache_ttl_seconds: 3600  # 1 h
  sample_cache_ttl_seconds: 3600  # 1 h; admin force-refresh path per §3.6
```

All optional; defaults applied if missing. Documented in `config/instance.yaml.example`.

### 10.5 Client config (`~/.agnes/config`)

New keys:

```yaml
snapshot_quota_gb: 10
snapshot_stale_warn_days: 7
fetch_default_estimate: true   # whether `da fetch` runs --estimate first by default
```

### 10.6 Schema drift handling

When `da snapshot refresh <name>` is called and the upstream BQ schema has changed since the snapshot was taken:

- **New column added** in BQ (not in original `--select`): no-op for refresh (we only re-fetch what's in `select`).
- **Column from `--select` was removed** in BQ: refresh fails with exit code 2 (`schema_drift`) and message `Column 'X' no longer exists in <table_id>. Drop snapshot and re-fetch with updated --select.` — leave the existing snapshot file untouched.
- **Column type changed**: re-fetch proceeds; new parquet has new type. CLI prints `WARN: column 'X' type changed STRING → INT64; downstream queries may break.`

### 10.7 Telemetry / observability

Server emits Prometheus-compatible metrics (`/metrics` endpoint, gated by admin):

- `agnes_v2_scan_request_total{status,user}` counter — request count by status
- `agnes_v2_scan_bytes_total{user}` counter — bytes returned per user
- `agnes_v2_scan_latency_seconds{quantile}` summary — request latency
- `agnes_v2_scan_concurrent_gauge{user}` gauge — current concurrent scans

Wired into existing observability stack (TBD per deploy — minimum: log lines structured for grep).

## 11. Success criteria

CI-verifiable (must pass automatically on the PR):

- [ ] **CI** — All existing tests still green on `zs/test-bq-e2e`.
- [ ] **CI** — `tests/test_where_validator.py` 50+ adversarial cases pass (§3.7 corpus).
- [ ] **CI** — Quota state correctly enforced in unit tests (concurrent + daily byte cap, 429 shape per §3.8).
- [ ] **CI** — `da catalog --json` output is machine-readable and includes `sql_flavor` per table (output-shape test).
- [ ] **CI** — `da fetch --estimate` outputs both BQ scan bytes and local result bytes (output-shape test).
- [ ] **CI** — `da snapshot list/refresh/drop/prune` lifecycle round-trip test.
- [ ] **CI** — Exit codes per §10.2 verified for every documented failure mode.
- [ ] **CI** — Vendor-token scan on touched files: empty.

Manual gates (release-time, signed off by author):

- [ ] **Manual** — On the dev VM, Claude (with the new skill loaded) answers "show me web_sessions_example for last 30 days" and produces an aggregated result in <30 s without "Response too large" errors. Verify the agent followed `da catalog → da schema → da fetch → da query` rather than direct `da query --remote`.
- [ ] **Manual** — 3 different fresh Claude sessions (without explicit prompting) follow the discovery-first protocol when asked about Agnes data. (Manual replay; document transcripts in PR.)
- [ ] **Manual** — End-to-end demo on dev VM: full discover → estimate → fetch → query loop in <2 min wall time, recorded in the PR description.
- [ ] **Manual** — Audit log inspection after demo run shows expected `event_data` shape per §10.1.
