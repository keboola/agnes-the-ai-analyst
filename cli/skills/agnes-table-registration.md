---
name: agnes-table-registration
description: Use when adding tables to the Agnes catalog so analysts can query them — single registration, bulk discovery, updates, and removals. Admin role required.
---

# Registering tables in Agnes

`da catalog` lists tables from `system.duckdb::table_registry`. A table you can `da fetch` exists in that registry. This skill is the protocol for getting tables into and out of it.

**Auth:** every command here requires admin role. The CLI sends the active PAT (`da auth import-token`); REST examples use `Authorization: Bearer $PAT` against the configured server.

## Decision flow — single vs. bulk vs. update

```
user wants to add tables
├── one specific table they named  → register-table (single)
├── "everything from <source>"     → discover-and-register
├── existing entry, change a field → PUT /api/admin/registry/{id}
└── remove a table from catalog    → DELETE /api/admin/registry/{id}
```

## Before you register — verify the source exists

Registering a table that does NOT exist at the source is silent: the row lands in the registry, but every later `da fetch` / `da query` against it 404s or 500s with an opaque message. Always verify first.

For BigQuery (`source-type=bigquery`):

```bash
# 1. confirm the dataset and table exist (uses the analyst's BQ creds, not the server's)
bq show --project_id=<billing-project> <data-project>:<dataset>.<table>
```

For Keboola (`source-type=keboola`):

```bash
# the discover-and-register dry-run is the lowest-friction probe
da admin discover-and-register --source-type=keboola --dry-run
```

If the source can't confirm the table exists, **stop and ask the user to verify** rather than registering speculatively.

## Single-table registration

```bash
da admin register-table <name> \
    --source-type=<keboola|bigquery|jira> \
    --bucket=<dataset_or_bucket> \
    --source-table=<source_object_name> \
    --query-mode=<local|remote> \
    --description="<short purpose, 1 line>"
```

Field meanings:

| Flag | Meaning | Example |
|---|---|---|
| `<name>` | Display name; the slugged form (`lower`, spaces→`_`) becomes the table id | `User Sessions` → id `user_sessions` |
| `--source-type` | Connector identity | `bigquery`, `keboola`, `jira` |
| `--bucket` | BQ dataset / Keboola bucket / Jira board | `product_analytics` |
| `--source-table` | Object name at the source (case-sensitive for BQ) | `s1_session_landings` |
| `--query-mode` | `local` = synced parquet / `remote` = on-demand BQ | `remote` for BQ views |
| `--description` | One sentence shown in `da catalog` | `"Per-session landing-page rows."` |

**Idempotence:** the API returns `409 Conflict` if the slugged id already exists. Always run `da admin list-tables --json` first and only register when the id is missing.

## Bulk discovery

When the user says "register everything from <source>", let the connector enumerate:

```bash
# 1. preview without writing anything
da admin discover-and-register --source-type=bigquery --dry-run --json

# 2. review output, then commit
da admin discover-and-register --source-type=bigquery
```

`discover-and-register` is **safe on re-run**: existing tables are skipped (not overwritten), new ones added. The `--dry-run` output lists what *would* change.

For Keboola, pass `--token` and `--url` if not already in `instance.yaml`:

```bash
da admin discover-and-register --source-type=keboola \
    --token="$KEBOOLA_TOKEN" --url=https://connection.keboola.com --dry-run
```

## Update an existing entry

No CLI command for this — use REST directly:

```bash
# change description, source-table, or query-mode on a registered entry
curl -sS -X PUT \
    -H "Authorization: Bearer $PAT" \
    -H "Content-Type: application/json" \
    -d '{"description": "Updated copy", "query_mode": "remote"}' \
    "$AGNES_SERVER_URL/api/admin/registry/<table_id>"
```

Only fields you include in the JSON body are updated — unspecified fields keep prior values.

## Remove a table

```bash
curl -sS -X DELETE \
    -H "Authorization: Bearer $PAT" \
    "$AGNES_SERVER_URL/api/admin/registry/<table_id>"
```

Returns `204 No Content` on success, `404` if the id doesn't exist. **The underlying source data is NOT touched** — only the catalog entry. Local snapshots created via `da fetch` also remain on the analyst's laptop until they `da snapshot drop` them.

## Heuristics

- **Slug, not display name.** When a later command asks for `table_id`, use the lower-snake_case form, not the original `--name`. `da admin list-tables` shows both columns.
- **One descriptive line.** `--description` shows up in `da catalog --json` and in agent rails reasoning. Make it count: "What's in this table?" not "Imported 2026-01-15."
- **`local` vs `remote` is permanent until you re-register.** Switching modes mid-life requires PUT-ing `query_mode`; that doesn't move data, just changes how it's served.
- **Don't register joins or views you'd rather compute on-the-fly.** A registered table is a long-term contract — analysts will write to its name. For one-off computations prefer `da query --remote`.

## When NOT to register

- The user wants to inspect a table once, doesn't intend to share it: register the row once with `query_mode='remote'` (admin-only, ~30s) and query it via `da query --remote "SELECT … FROM <registered_id>"`. Direct `bq."<dataset>"."<table>"` syntax is now registry-gated — unregistered paths return 403 `bq_path_not_registered` (closes the pre-existing RBAC + cost-cap bypass).
- The data lives in a third source not yet supported by a connector: implement the connector first (see `connectors.md` skill), then register.
- The dataset already has a registered "parent" view that exposes the rows you want: register-table is for distinct catalog entities, not for slicing existing ones — slice with `da fetch --where`.

## Confirmation flow

After registration, sanity-check:

```bash
da admin list-tables --json | jq '.[] | select(.id == "<table_id>")'
da catalog --json    | jq '.tables[] | select(.id == "<table_id>")'
da schema <table_id>     # forces a real source-side schema fetch — fails fast if source is wrong
```

If `da schema` 500s on a freshly registered remote BQ table, the most common causes (in order): wrong `--source-table` (typo), wrong `--bucket` (dataset), missing `data_source.bigquery.billing_project` when reading cross-project, missing `serviceusage.services.use` IAM on the billing project.
