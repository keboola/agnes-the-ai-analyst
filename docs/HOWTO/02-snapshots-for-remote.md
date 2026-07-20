# Snapshots for remote tables

Remote tables (`query_mode='remote'`) live in BigQuery. There is no local parquet for them — `agnes pull` skips them intentionally. Querying them directly without a cost guardrail can scan hundreds of gigabytes.

This guide explains the safe access pattern.

## Why snapshots?

- **No local parquet** — `agnes query "SELECT * FROM web_sessions"` on a remote table hits BQ through the server, not a local file. (`agnes query` defaults to `--scope auto`: local first, transparent server-side fallback when the table isn't local — a `[scope]` stderr note tells you it happened. `--remote` is the explicit shorthand for `--scope server`.)
- **Cost** — a `SELECT *` on a 225M-row table can scan 50+ GB and cost real money.
- **Latency** — large remote queries are slow; a local snapshot is fast for repeated analysis.

**Rule: always run `--estimate` before fetching. Always use `--select` + `--where`.**

## Step 1: Check what's available

```bash
agnes catalog
# Look for query_mode = remote
```

```bash
agnes schema web_sessions
# Learn columns, types, partition_by, clustered_by
```

## Step 2: Estimate before fetching

```bash
agnes snapshot create web_sessions \
    --select event_date,country_code,session_id,page_views,bounce \
    --where "event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) AND country_code = 'CZ'" \
    --estimate
```

Output:
```
estimated_scan_bytes: 4.2 GB
estimated_result_rows: ~250,000
estimated_local_size: ~12 MB
```

If the scan estimate is large, tighten `--where` or reduce `--select`.

## Step 3: Fetch the snapshot

When the estimate looks reasonable:

```bash
agnes snapshot create web_sessions \
    --select event_date,country_code,session_id,page_views,bounce \
    --where "event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) AND country_code = 'CZ'" \
    --as cz_last_30d
```

This materializes the result as a local DuckDB view named `cz_last_30d`.

## Step 4: Query the snapshot

```bash
agnes query "SELECT event_date, COUNT(*) AS sessions, AVG(page_views) AS avg_pv FROM cz_last_30d GROUP BY event_date ORDER BY event_date"
```

No BQ round-trip. Runs locally, instantly.

## BigQuery SQL flavour for `--where`

Remote tables use BQ SQL in `--where`, not DuckDB SQL:

```sql
-- Dates
event_date >= DATE '2026-01-01'
event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)

-- Timestamps
created_at >= TIMESTAMP '2026-01-01 00:00:00 UTC'

-- Regex
REGEXP_CONTAINS(url, r'/checkout')

-- Cast
CAST(user_id AS INT64)
```

DuckDB-style casts (`::date`, `::int`) will fail on remote tables.

The same BQ flavour also works for `query_mode='materialized'` tables — the
server executes those from its local parquet copy (no BigQuery scan) and
transpiles the predicate to DuckDB automatically. DuckDB flavour (what
`agnes schema` reports for materialized tables) is accepted there too.

## Managing snapshots

```bash
agnes snapshot list           # see all snapshots + sizes
agnes snapshot drop cz_last_30d   # clean up when done
agnes disk-info               # total cache size
```

**Reuse snapshots within a conversation.** If you already fetched `cz_last_30d`, don't fetch again — just query it.

## When NOT to use a snapshot

- Single aggregate on a remote BASE TABLE (cheap scan):
  ```bash
  agnes query --remote "SELECT COUNT(*) FROM web_sessions"
  ```
- Quick exploration of a small slice:
  ```bash
  agnes query --remote "SELECT * FROM web_sessions WHERE event_date = DATE '2026-05-01' LIMIT 100"
  ```
  The server enforces a 5 GiB scan cap — you'll get a `remote_scan_too_large` error if the query exceeds it, with a suggestion to use `snapshot create`.

Both examples also work without `--remote` — the default `--scope auto` routes them server-side automatically once the table isn't local. Use the explicit flag when you want to be sure the query never touches (possibly stale) local data.
