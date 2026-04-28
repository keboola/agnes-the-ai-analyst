---
name: agnes-data-querying
description: Use when querying any data in Agnes — discovery first, estimate before fetch, materialize scoped subsets locally
---

# Querying Agnes data

When asked about ANY data in Agnes, follow this protocol: **discover → choose tool → fetch (with estimate) → query locally → clean up**.

## Discovery first

Before writing ANY query, understand what's available:

```bash
da catalog --json | jq <filter>     # know what's available
da schema <table>                    # learn columns + types
da describe <table> -n 5             # see real values for shape
```

**Never** write `SELECT * FROM <table>` blindly. For local-mode tables it's wasteful; for remote-mode tables it can blow up at 225M+ rows.

## Choose the right tool

Tables in `da catalog` have a `query_mode`:

| Mode | Means | How to query |
|------|-------|--------------|
| `local` | parquet synced on laptop | `da query "SELECT …"` directly |
| `remote` (BigQuery) | parquet NOT on laptop | `da fetch` subset → snapshot, OR `da query --remote` one-shot |

For **remote tables**, you MUST either:
1. `da fetch` a filtered subset → query the local snapshot (preferred), OR
2. `da query --remote` for one-shot server-side execution, OR
3. `da query --register-bq` for hybrid joins (rare; see docs)

## The `da fetch` workflow (preferred for remote tables)

### 1. Estimate first

Always estimate before fetching:

```bash
da fetch web_sessions_example \
    --select event_date,country_code,session_id \
    --where "event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) 
             AND country_code = 'CZ'" \
    --estimate
```

Output tells you scan cost, expected rows, and local bytes — so you know if it's reasonable.

### 2. If reasonable, fetch to snapshot

```bash
da fetch web_sessions_example \
    --select event_date,country_code,session_id \
    --where "event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) 
             AND country_code = 'CZ'" \
    --as cz_recent
```

### 3. Query the local snapshot

```bash
da query "SELECT event_date, COUNT(*) FROM cz_recent GROUP BY 1 ORDER BY 1"
```

## Heuristics for `da fetch`

| Requirement | Why |
|-------------|-----|
| **Always `--select` specific columns** | Avoid implicit `SELECT *` on remote (expensive) |
| **Always `--where` for remote tables** | Otherwise add `--limit` to keep result bounded |
| **Always `--estimate` first if unsure** | Partition/clustering metadata + shape matters; dry runs are free |
| **Reuse snapshots across questions** | `da snapshot list` before fetching — existing snapshot? Skip the fetch |

## BigQuery SQL flavor for `--where`

For `source_type=bigquery` (per `da catalog`), use BigQuery SQL syntax:

| Syntax | Example |
|--------|---------|
| Date literal | `DATE '2026-01-01'` (NOT `'2026-01-01'::date`) |
| Timestamp literal | `TIMESTAMP '2026-01-01 00:00:00 UTC'` |
| Now | `CURRENT_DATE()`, `CURRENT_TIMESTAMP()` |
| Date arithmetic | `DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)` |
| Regex | `REGEXP_CONTAINS(col, r'pattern')` (raw string!) |
| NULL check | `col IS NOT NULL` (standard) |
| Cast | `CAST(x AS INT64)` (NOT `INT`) |

For `source_type=keboola` / `source_type=jira` (local), use **DuckDB SQL** in your `da query` calls — there's no `--where` on local since fetch is implicit.

## Snapshot hygiene

- Reuse snapshots across questions in the same conversation
- Use descriptive names: `cz_recent`, `orders_q1_us`, `sessions_today`
- Drop with `da snapshot drop <name>` when done with a topic
- Check total cache size with `da disk-info`

## When NOT to use `da fetch`

| Scenario | Use instead |
|----------|------------|
| Single aggregate on remote table (`SELECT COUNT(*)`) | `da query --remote "SELECT COUNT(*) FROM web_sessions_example"` — cheap, no fetch needed |
| Throwaway exploration with raw BQ syntax | `da query --remote` — one-shot, no snapshot |
| Cross-table JOIN with both remote | Use `da fetch` for one side + `da query --remote` for the other; full cross-remote JOIN needs design (see #101) |

## When the table you need isn't in `da catalog`

The catalog reads from `system.duckdb::table_registry` — entries land there only via admin registration, not auto-discovery. If `da catalog` doesn't show what the user is asking about:

1. Tell the user the table isn't registered
2. Hand off to an admin (or, if you have admin role yourself, follow the **agnes-table-registration** skill)
3. Don't `da query --remote` your way around it — the catalog gap means the registry doesn't track this dataset, RBAC can't gate it, and quotas don't apply

## Protocol summary

1. **Discover**: `da catalog`, `da schema`, `da describe`
2. **Check query_mode**: local (direct) or remote (fetch or --remote)?
3. **For remote**: `--estimate` first, then `da fetch` with `--select` + `--where`
4. **Snapshot name**: descriptive (`cz_recent`), reuse across questions
5. **Query**: `da query` against snapshot; DuckDB SQL syntax
6. **Cleanup**: `da snapshot drop` when done; `da disk-info` to check size
