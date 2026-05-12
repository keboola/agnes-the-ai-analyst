# First query: pull, catalog, query

This guide walks you through the full data access loop from a fresh analyst workspace.

## Prerequisites

- Agnes CLI installed: `pip install agnes-cli` or follow `docs/ONBOARDING.md`.
- Your admin has provisioned your account and at least one table is registered.
- `AGNES_SERVER_URL` and `AGNES_PAT` are set in your environment (or `~/.agnes/config`).

## Step 1: Pull fresh data

`agnes pull` downloads all parquet files you have access to and rebuilds your local DuckDB views.

```bash
agnes pull
```

Expected output:
```
Pulling manifest from https://agnes.example.com ...
  orders_summary     ✓  2.1 MB  (updated)
  customer_segments  ✓  0.4 MB  (unchanged, skipped)
Rebuilt local views in analytics.duckdb
```

- Tables with `query_mode='remote'` are skipped — they have no local parquet (see guide 02).
- Run `agnes pull` at the start of each session. The `SessionStart` hook does this automatically if `agnes init` was run.

## Step 2: Discover available tables

```bash
agnes catalog
```

This lists every table in your manifest: name, source type, query mode, last sync time, and row count.

```
NAME                SOURCE     MODE      ROWS      SYNCED
orders_summary      keboola    local     124,302   5 min ago
customer_segments   keboola    local      12,801   5 min ago
web_sessions        bigquery   remote         —    (remote)
```

For a specific table's columns and types:

```bash
agnes schema orders_summary
```

To see real values (shape check):

```bash
agnes describe orders_summary -n 5
```

Always run `describe` before writing a query — it shows column names, types, and sample values.

## Step 3: Write your first query

```bash
agnes query "SELECT region, SUM(revenue) AS total FROM orders_summary GROUP BY region ORDER BY total DESC LIMIT 10"
```

Output:
```
region          total
-----------     ----------
North America   4,821,300
Europe          3,104,200
APAC            1,892,100
...
```

For longer SQL, use a file:

```bash
agnes query --file my_analysis.sql
```

Or inline with stdin:

```bash
echo "SELECT COUNT(*) FROM customer_segments WHERE tier = 'Gold'" | agnes query --stdin
```

## Tips

- `agnes catalog --json | jq '.[] | select(.query_mode == "local")'` — filter to local tables only.
- `agnes query` runs against your local DuckDB — no server round-trip, no BQ cost.
- For remote tables (`query_mode='remote'`), see [guide 02: Snapshots for remote tables](02-snapshots-for-remote.md).
- For canonical metric definitions, run `agnes catalog --metrics` before inventing formulas.
