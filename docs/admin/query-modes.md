# Query Modes — when to register a table as `local`, `remote`, or `materialized`

Source-agnostic guide to the three `query_mode` values Agnes supports. Pick the right mode at registration time and the analyst-side experience is fast, cost-aware, and predictable. Pick wrong and you'll either burn BQ scan budget on every query or spend hours waiting on syncs that didn't need to happen.

## TL;DR — decision tree

```
Is the table small (< 1 GB) and updated daily-or-slower?
  └─ YES → query_mode: local       (sync to laptop, query offline)

Is the table the result of an aggregate SQL the operator controls?
  └─ YES → query_mode: materialized  (server runs SQL → parquet, distributed)

Otherwise:
  └─ query_mode: remote   (data stays in upstream; analyst queries on demand)
```

## Three modes side-by-side

| Aspect | `local` | `materialized` | `remote` |
|---|---|---|---|
| Where the data lives | Analyst laptop (parquet) | Agnes server filesystem (parquet) | Upstream (BigQuery, Keboola, …) |
| Who runs the query | Analyst's local DuckDB | Analyst's local DuckDB | Upstream engine via DuckDB extension |
| Cost model | Free after sync | Free after each sync | Per-query scan cost on the analyst's first hit |
| Freshness | As fresh as last sync | As fresh as last scheduled run | Live |
| Scan limits | None (laptop disk) | None (server disk) | `bq_max_scan_bytes` cost gate (default 5 GiB) |
| Best for | Stable reference data, daily-updated facts | Aggregates, daily snapshots | Big tables, live data, residency-restricted |

## Per-source-type reference

### BigQuery — `query_mode: remote`

The most common use case for `remote`. Data stays in BQ; analysts query on demand via the Agnes server's service account.

**IAM:** the server's SA must have:
- `roles/bigquery.dataViewer` on the dataset (read access)
- `roles/bigquery.jobUser` on the *billing* project (run jobs)

If `data_source.bigquery.billing_project == data_source.bigquery.project`, set the SA's `serviceusage.services.use` permission too — the BQ extension can otherwise 403 USER_PROJECT_DENIED on the first query. The instance health check (`agnes diagnose`) surfaces this as an `info`-tier entry on `bq_config`.

**Register via UI:** `/admin/tables` → "Add table" → Source type `bigquery` → Mode `remote` → fill `dataset` (your BQ dataset name) + `source_table` (the BQ table id within that dataset).

**Register via CLI:**

```bash
agnes admin register-table sales_2024 \
    --source-type bigquery \
    --bucket dwh_base \
    --source-table sales_2024 \
    --query-mode remote
```

After registration, smoke-test the SA's access:

```bash
agnes query --remote "SELECT COUNT(*) FROM sales_2024"
```

A 403 here means the SA is missing `dataViewer` or `jobUser`; fix in IAM and re-test.

**Cost guardrail:** `bq_max_scan_bytes` (default 5 GiB) refuses queries whose pre-execution scan estimate exceeds the cap. Configurable in `/admin/server-config`. When an analyst hits the cap, the response includes a hint to use `agnes snapshot create --where '<predicate>'` to materialise a scoped subset locally.

### BigQuery — `query_mode: materialized`

The server runs a scheduled SQL aggregate against BigQuery and writes the result to a parquet on the Agnes filesystem. Analysts get the parquet via `agnes pull` like any other local table.

**Register via CLI:**

```bash
agnes admin register-table monthly_kpis \
    --source-type bigquery \
    --bucket dwh_base \
    --source-table monthly_kpis \
    --query-mode materialized \
    --query @path/to/monthly_kpis.sql \
    --sync-schedule "daily 03:00"
```

**Cost guardrail:** `data_source.bigquery.max_bytes_per_materialize` (default 10 GiB; set `0` to disable) refuses materialise runs whose query plan exceeds the cap. Catches a typo'd `WHERE` clause that would otherwise scan a year of data.

**Analyst reads are served from the parquet:** `agnes snapshot create` / `POST /api/v2/scan` (and `/estimate`) on a materialized table read the server-side parquet directly — zero upstream BigQuery scan cost, and `/estimate` reports `estimated_scan_bytes: 0`. The scheduled materialize run is the only thing that touches BigQuery. `--where` predicates may use BigQuery or DuckDB flavor (both are validated and rendered as DuckDB for the local read). Until the first materialize run completes there is no parquet yet, so reads return 404 — they never fall back to scanning the raw upstream table.

### Keboola — `query_mode: local` (the production path)

The Agnes server's Keboola DuckDB extension downloads the table to a parquet on the server filesystem; `agnes pull` distributes it to analyst laptops.

**Setup:** `instance.yaml.data_source.type: keboola` + Storage API token via `KEBOOLA_STORAGE_TOKEN` env var (or whatever `instance.yaml.token_env` points at).

**Register via CLI:**

```bash
agnes admin register-table users \
    --source-type keboola \
    --bucket in.c-crm \
    --source-table users \
    --query-mode local
```

**`query_mode: remote` for Keboola** is architecturally supported via the `_remote_attach` mechanism (the orchestrator can ATTACH the Keboola DuckDB extension on demand the same way it does for BQ), but **not in active deployment use today**. If you have an analyst workflow against a Keboola table that's too big to sync, file an issue — the architecture is in place but the registration UX hasn't been polished.

### Jira — `query_mode: local` only

Event-driven: webhooks update parquets incrementally. No `remote` or `materialized` mode for Jira today.

## Worked examples

**1. Big BigQuery fact table you query weekly:** `query_mode: remote`. SA needs `dataViewer` + `jobUser`. Analyst runs `agnes query` for one-off aggregates (the default `--scope auto` routes remote tables server-side automatically; `--remote` is the explicit shorthand) and `agnes snapshot create` for cross-week joins.

**2. Daily Keboola dimension table:** `query_mode: local`. Synced once a day by the scheduler; analyst's `agnes pull` picks it up.

**3. Monthly KPI aggregate from a BQ datawarehouse:** `query_mode: materialized` + `--sync-schedule "0 3 1 * *"` (3:00 on the 1st of each month). The server runs your aggregate SQL once a month; analysts get a parquet of the result.

## See also

- `docs/RBAC.md` — granting analysts access to a registered table.
- `config/instance.yaml.example` — the `data_source` config block.
- `agnes catalog --json` — inspect a registered table's mode + size hints.
- `agnes diagnose` — surface `bq_config` IAM issues and other health entries.
