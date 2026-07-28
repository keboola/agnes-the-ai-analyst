# BigQuery billable-job cost-attribution labels (#752)

**Status:** approach 2 (route billable execution through labeled `client.query`)
adopted; scan + fully-materialized remote-select paths done. Interactive
LIMIT-capped `/api/query --remote` deferred (see "Remaining gap").

## Problem

PR #751 added BQ job labels (`workload_type` / `agent_name` / `environment` /
`user_id`) for per-user cost attribution, labelling every `client.query` job it
controls. But the **billable** execution on the two main remote paths ran through
the DuckDB `bigquery_query()` community extension:

- `/api/v2/scan` → `_run_bq_scan`
- `/api/query --remote` → `run_remote_select_to_arrow` / `execute_query`

The extension owns the job config and exposes **no label hook and no job id**
(verified against the extension's documented settings — only `bigquery_load()` /
`bigquery_extract()` take a `labels` MAP, and only `bigquery_execute()` returns a
job id; `bigquery_query()` does neither). So those billable jobs carried no
labels; only the free dry-run estimates were labeled.

## Direction decision

| approach | verdict |
|---|---|
| **1. Capture `bq_job_id` from the extension, correlate after the fact** | Not possible — `bigquery_query()` does not surface a job id. |
| **2. Route billable execution through `client.query(labels=...)`** | **Adopted.** The only in-repo path that works today. |
| **3. Upstream label support into the DuckDB bigquery extension** | Out of scope — external repo, long lead; would later let the interactive path be labeled without the client.query trade-offs. |

There is also no `SET bq_*` session option for labels, so the extension can't be
labeled via `apply_bq_session_settings` either.

## What shipped

- **Shared helper** `connectors.bigquery.access.run_bq_query_to_arrow(bq, sql,
  *, labels)` → `(arrow_table, job_info)`. Runs the billable job via
  `client.query(job_config=QueryJobConfig(labels=...))`, with the same BQ Storage
  Read API → REST fallback and error translation as `register_bq` (#751).
- **`/api/v2/scan`** — `_run_bq_scan` now delegates to the shared helper
  (behavior unchanged; it already used `client.query` since the first #752 slice).
- **`/api/query --remote --auto-snapshot`** (`run_remote_select_to_arrow`) — when
  the query is fully pushed to BQ, the billable job runs via the labeled helper
  (`agent_name="query"`) instead of the extension. This path fully materializes
  the result to Arrow anyway, so `client.query(...).to_arrow()` is
  shape-equivalent. `BqAccessError` propagates so `scan_endpoint` maps it to the
  correct HTTP status (500/502/400). Queries that can't be pushed (cross-source
  joins, DuckDB-only syntax) fall back to the extension path, unlabeled.

`_rewrite_user_sql_for_bigquery_query` was refactored into
`_bq_remote_execution_plan`, which additionally returns the BQ-native inner SQL
and billing project so the caller can run the labeled `client.query`. The old
2-tuple wrapper is preserved for `execute_query` and existing tests.

## Remaining gap (deliberate)

The interactive `/api/query --remote` path (`execute_query`) still runs its
billable job through the extension. Unlike the snapshot path it streams under a
`LIMIT N+1` cap, so its billable byte volume is small and bounded, and routing it
through `client.query` is the "larger change with memory/streaming trade-offs"
#752 itself flags. Options when revisited: mirror the snapshot change (run the
capped inner SQL via the shared helper) or adopt approach 3 upstream. Attribution
value is highest on the **uncapped** paths (scan + snapshot), which are now
covered.
