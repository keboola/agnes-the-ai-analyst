---
name: agnes-analyst
description: How to use Agnes as an analyst — discover tables, choose the right query path for local vs remote data, take filtered snapshots of large remote tables, and look up canonical business-metric definitions. Use whenever someone asks about querying Agnes data, the catalog, snapshots, `agnes query`, remote/BigQuery tables, or how to compute a business metric. Triggers on "query Agnes", "what tables are there", "agnes catalog", "snapshot a remote table", "how do I compute revenue/MRR".
---

# Working with Agnes data — analyst rails

Use this when working with data served by an Agnes instance. Follow this
protocol before writing any query.

## 1. Discover before you query

Never write `SELECT * FROM <table>` blindly — for local tables it's wasteful,
for remote tables it can scan enormous data. First:

```bash
agnes catalog --json | jq <filter>     # what's available
agnes schema <table>                   # columns + types
agnes describe <table> -n 5            # real values for shape
```

## 2. Choose the path by `query_mode`

Every table in `agnes catalog` has a `query_mode`:

- **`local`** — data is on the laptop as parquet (synced via `agnes pull`).
  Query directly: `agnes query "SELECT … FROM <table>"` (DuckDB SQL).
- **`remote`** (typically BigQuery) — the parquet is NOT local. Either:
  1. `agnes snapshot create` a filtered subset, then query the local snapshot, or
  2. `agnes query --remote "…"` for one-shot server-side execution (cost-guarded
     by a scan cap; on hit it returns `remote_scan_too_large` — pivot to a
     filtered snapshot).

## 3. Snapshot workflow (preferred for remote tables)

```bash
# estimate first
agnes snapshot create <id> --select col_a,col_b \
    --where "<predicate>" --estimate
# if reasonable, fetch
agnes snapshot create <id> --select col_a,col_b --where "<predicate>" --as my_subset
# query the local snapshot
agnes query "SELECT col_a, COUNT(*) FROM my_subset GROUP BY 1"
```

Heuristics: always list explicit columns in `--select`; always add a `--where`
(or `--limit`) for remote tables; always `--estimate` first when the shape is
unknown, the table is partitioned/clustered, or the fetch could exceed ~1 GB.
Reuse snapshots across questions; drop them with `agnes snapshot drop <name>`
when done; check cache size with `agnes disk-info`.

## 4. SQL flavor depends on the source

- `source_type=bigquery` (remote `--where`): BigQuery SQL — `DATE '2026-01-01'`,
  `DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)`, `REGEXP_CONTAINS(col, r'…')`,
  `CAST(x AS INT64)`.
- Local sources (`agnes query`): DuckDB SQL.

## 5. Business metrics — never invent a calculation

Before computing any business metric, look up its canonical definition:

```bash
agnes catalog --metrics                       # find it
agnes catalog --metrics --show <metric>       # read the SQL + business rules
```

Use that SQL, adapted to the question. Never invent a metric formula.

## When NOT to snapshot

- A single aggregate on a remote table (`SELECT COUNT(*) …`) → use
  `agnes query --remote` (the cap and pushdown handle it cheaply).
- Throwaway exploration on a registered id → `agnes query --remote`.
- Reuse an existing snapshot (`agnes snapshot list`) before fetching again.
