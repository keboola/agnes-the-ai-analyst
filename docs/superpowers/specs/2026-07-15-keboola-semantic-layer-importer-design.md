# Keboola Semantic Layer → `metric_definitions` Importer — Design

Status: approved for implementation planning
Date: 2026-07-15

## Motivation

Keboola projects can define a **semantic layer** (aka Metastore) — a project-scoped
catalogue of datasets, metrics, relationships, constraints, and glossary terms,
each metric backed by a real SQL expression bound to a real Storage table.
Agnes already has an equivalent, lighter-weight concept — the `metric_definitions`
table, populated today via `agnes admin metrics import docs/metrics/` (manual
YAML) or `src/catalog_export.py` (OpenMetadata). This design adds a third
source: importing a Keboola project's semantic layer directly into
`metric_definitions`, so metrics authored in Keboola become available to
Agnes's business-metric rails without hand-transcription.

## Background — verified facts

Everything below was verified either against the public OSS `keboola/cli`
source (test fixtures, not just docs) or against a live Keboola project's
Metastore API during this design's brainstorming session. Nothing here is
speculative.

### The Metastore API

- Separate service at `metastore.<stack>`, derived from the project's
  `connection.<stack>` Storage API URL by string substitution
  (`connection.` → `metastore.`). Same `X-StorageApi-Token` auth as Storage.
- REST CRUD: `GET/POST/PUT/DELETE /api/v1/repository/{type}`, where `type` is
  one of `semantic-model`, `semantic-dataset`, `semantic-metric`,
  `semantic-relationship`, `semantic-constraint`, `semantic-glossary`,
  `semantic-reference-data`.
- List responses: `{"data": [{"type", "id", "attributes", "meta"}, ...]}`.
- **Requires a master (owner) Storage API token.** Verified live: a
  non-master token with zero bucket permissions AND a separate non-master
  token with full bucket read/manage permissions both failed identically
  with `401 {"exception": "Failed to create project scope"}`; the project's
  actual master token succeeded immediately (`200`, returned real data).
  This mirrors Kai's documented requirement (`kai preflight` explicitly
  checks `is_master_token`) even though the CLI's own docs don't state this
  for the semantic-layer group. The CLI's own test suite and error-handling
  code have **no special case** for this error string — it is not a known
  client-side condition, it is a hard server-side requirement.

### Real data shapes (verified against a live test project's Metastore — sample size: several dozen datasets and metrics)

Field/attribute names below are the real, verified wire shape. Example
*values* (table names, formulas) are fabricated placeholders — the live
project used for verification contains real business data and is not
reproduced here.

`semantic-dataset.attributes`:
```
{name, tableId, fqn, modelUUID, description, grain, primaryKey: [...],
 fields: [{name, type, role, description}, ...],
 ai: {hints: [...], keywords: [...], synonyms: [...], warnings: [...]}}
```
- `tableId` is the Keboola `bucket.table` id as seen from the token's own
  project (e.g. `in.c-example_source.orders`) — this is what must be
  matched against Agnes's `table_registry`.
- `fqn` is the **physical** Snowflake path (e.g.
  `"EXAMPLE_DB"."out.c-example"."orders"`) and can point at a different
  bucket/project than `tableId` implies (linked-bucket architecture) — it is
  **not** usable for matching against `table_registry` and is ignored by
  this importer.
- The `ai` block (keywords/synonyms/hints) is real, populated data — a good
  source for Agnes's `synonyms`/`notes` fields, for free.

`semantic-metric.attributes`:
```
{name, sql, dataset, modelUUID, description}
```
- `sql` is **never a full query** — it is a bare aggregation expression,
  e.g. `SUM("amount")`, `COUNT(CASE WHEN "status" = 'error' THEN 1 END)`,
  `SUM("value") * 12`. Confirmed at scale (several dozen real metrics), not
  just isolated test fixtures.
- `dataset` is a `tableId` string, matching a `semantic-dataset.tableId`.
- **Some real expressions reference OTHER datasets via an alias** not
  present in `dataset` — e.g. one observed metric's
  `ROUND(SUM(TRY_CAST(o."amount" AS DECIMAL(18,2))), 2)` (alias `o`) and
  another's expression referencing two other aliases from joined tables.
  These are genuine multi-table JOINs whose join condition lives in
  `semantic-relationship`, not in the metric itself.

`semantic-constraint.attributes` (from `keboola/cli` docs/gotchas, not yet
verified live against this project): `{name, constraintType, rule: "<SQL-ish
string, e.g. 'value >= 0'>", metrics: ["metric_name", ...], severity}`. Name
must match `^[a-z][a-z0-9_]*$`.

## Scope (v1)

**In scope:** `semantic-dataset` (lookup only, not persisted), `semantic-metric`,
`semantic-constraint`.

**Out of scope, explicitly:**
- `semantic-relationship` and multi-dataset JOIN composition. A metric whose
  `sql` expression references an alias other than its own `dataset` cannot be
  safely composed without relationship data — it is skipped and counted, not
  guessed. Real-world impact: a minority but non-trivial fraction of metrics
  in the verification sample hit this path. Follow-up iteration once
  relationship data has a clear consumption path.
- `semantic-glossary`. No natural 1:1 home in the flat `metric_definitions`
  table (glossary terms are conceptual definitions, not per-metric); a
  name-match heuristic into `synonyms`/`notes` was considered and rejected
  as unreliable (glossary terms are typically multi-word phrases like
  "Monthly Recurring Revenue", metric names are snake_case like `mrr` —
  exact-match would silently miss almost everything).
- Web UI. No admin page exists today for `metric_definitions` from ANY
  source (manual/yaml_import/OpenMetadata) — only `agnes catalog --metrics`.
  Filed as a separate, source-agnostic issue:
  [keboola/agnes-the-ai-analyst#853](https://github.com/keboola/agnes-the-ai-analyst/issues/853).
- Multiple `semantic-model` support. v1 picks the first model in the
  project and logs a warning if more than one exists.

## Architecture

Mirrors `app/api/bq_metadata_refresh.py` — a standalone scheduled job, not a
YAML round-trip, not folded into `connectors/keboola/extractor.py`'s
per-table sync loop (rejected alternatives; see rationale in "Alternatives
considered" below).

```
services/scheduler (new job tuple, own interval env var)
        │  POST, shared SCHEDULER_API_TOKEN auth (existing mechanism)
        ▼
app/api/keboola_semantic_layer_refresh.py  (Depends(require_admin))
        │
        ▼
connectors/keboola/semantic_layer.py :: sync_semantic_layer()
        │
        ├─ connectors/keboola/metastore_client.py  (new, GET-only for v1)
        │     resolves model → fetches dataset/metric/constraint lists
        │
        ├─ in-memory lookup: table_registry_repo().list_by_source("keboola")
        │     → {(bucket, source_table): agnes_view_name}
        │     (no new repo method — reuses an existing, already-parity'd call)
        │
        ├─ per metric: resolve dataset.tableId → agnes table_name
        │     compose sql = f'SELECT {expression} FROM "{table_name}" AS t'
        │     merge matching semantic-constraint rows → validation JSON
        │
        └─ metric_repo().create(..., source="keboola_semantic_layer")  (upsert)
           + prune: delete source='keboola_semantic_layer' rows absent
             from this run's set (never touches other sources)
```

### Field mapping

| Keboola | Agnes `metric_definitions` |
|---|---|
| `semantic-metric.name` | `name` |
| `semantic-metric.description` | `description` |
| `semantic-metric.sql` (fragment) | `expression` |
| composed `SELECT {expression} FROM "{table_name}" AS t` | `sql` (required) |
| `semantic-metric.dataset` → resolved via dataset lookup | `table_name` |
| `semantic-dataset.grain` | `grain` |
| `semantic-dataset.primaryKey` / `fields[]` | `dimensions[]` |
| `semantic-dataset.ai.synonyms` | `synonyms[]` |
| `semantic-dataset.ai.hints` / `.warnings` | `notes[]` |
| `semantic-constraint.rule` (joined by `metrics[]`) | `validation` (JSON) |
| — | `source = "keboola_semantic_layer"` |
| — | `id = "keboola/{model_uuid}/{metric_name}"` |

The consistent `AS t` alias handles both bare column references
(`SUM("amount")`) and any single-table qualified references without
needing a SQL parser/dialect translator — `sqlglot` (already a repo
dependency, used by `app/api/where_validator.py`) was considered for a more
general AST-based rewrite but is unnecessary once the real data showed `sql`
is a fragment, not a full query, for the single-dataset case that is in
scope.

### Table resolution

Keboola `tableId` = `bucket.source_table` (verified:
`connectors/keboola/extractor.py:208,878`). Agnes addresses tables by
`table_registry.name` (the DuckDB view name), a different identifier.
Resolution: split `tableId` on the first two segments, match against
`table_registry_repo().list_by_source("keboola")` rows' `bucket` +
`source_table` fields (built once per sync run into an in-memory dict — no
new repository method, no new parity work). A metric whose dataset isn't
registered in Agnes at all is skipped and counted (`skipped_unregistered_table`),
not guessed.

### Configuration

Requires a **master Storage API token** for the Keboola project — the
same token type Kai requires. This is a real operational/security
consideration: broader privilege than a typical read-only connector
credential (a master token can manage buckets and tokens). Document this
explicitly wherever the importer's setup is documented; do not silently
reuse an existing narrower `KEBOOLA_STORAGE_TOKEN` if that token is not a
master token — fail fast with a clear error naming the requirement, the
same way `kai preflight` does, rather than the opaque
`"Failed to create project scope"` the raw API returns.

### Error handling

- Metastore unreachable / 401 / 5xx at the top-level fetch → whole run
  aborts, logs, returns `{"status": "error", ...}`. **Never prunes** on a
  failed/partial fetch.
- Metric with unresolvable `dataset` (tableId not in Agnes's
  `table_registry`) → skip that metric only, log + count, continue.
- Metric whose `sql` expression references an alias outside its own
  `dataset` → skip, log + count separately from unresolved-table skips
  (`skipped_multi_dataset_expression`) — detection: after composing
  `SELECT {expression} FROM "{table_name}" AS t`, a metric is flagged if its
  raw `sql` fragment contains any bare `<alias>.` qualifier other than `t.`
  or an unqualified column. (Exact detection heuristic is an implementation
  detail for the plan, not this design — the important contract is
  "when in doubt, skip and count, never silently emit wrong SQL.")
- More than one `semantic-model` in the project → use the first, log a
  warning.
- Prune only ever deletes rows with `source='keboola_semantic_layer'`.

### Testing

- Unit tests for `MetastoreClient` (mocked HTTP): host derivation, auth
  header, list/error envelope parsing.
- Unit tests for `semantic_layer.py` mapping logic as pure functions:
  dataset-lookup join, constraint merge, multi-dataset-expression
  detection, prune diffing — no live API needed.
- Endpoint test via existing test-client patterns + `metric_repo()` against
  a test DuckDB.
- DuckDB↔PG parity: **no new parity work** — only existing
  `create()`/`delete()`/`list()` on `metric_repo()` are used.
- CHANGELOG bullet in the implementing PR.

## Alternatives considered (rejected)

- **YAML mirror of `src/catalog_export.py`** (write `docs/metrics/keboola/*.yml`,
  rely on `agnes admin metrics import` to load it). Rejected once the sync
  was confirmed to be a scheduled, non-human-reviewed job: nobody hand-edits
  the generated YAML, so the on-disk round-trip is pure overhead versus
  writing straight through `metric_repo()` (the `bq_metadata_refresh.py`
  pattern).
- **Folding into `connectors/keboola/extractor.py`'s `run()`**. Rejected —
  that function's contract is explicitly about `_meta`/`extract.duckdb`
  table data; mixing in catalog writes muddies an already-large function
  and couples metric-sync cadence to table-sync cadence for no benefit.
- **sqlglot AST-based dialect rewrite** for arbitrary metric SQL. Rejected
  once live data showed `sql` is a fragment, not a full dialect-specific
  query, for the in-scope (single-dataset) case — simple string composition
  is sufficient and has no dialect-ambiguity risk.
- **Glossary → `synonyms`/`notes` via name matching**. Rejected as
  unreliable (see Scope section).

## Open risks / follow-ups

1. **Master-token requirement** is an operational cost for operators
   deploying this importer — needs to be called out plainly in setup docs,
   not just in this spec.
2. **Multi-dataset (relationship-based) metrics** are skipped in v1; no
   solid estimate yet of what fraction of a typical project's metrics this
   excludes — a minority but non-trivial share in the one live project
   sampled during this design.
3. **`semantic-constraint` shape** (`rule`, `constraintType`, `metrics[]`,
   `severity`, name regex `^[a-z][a-z0-9_]*$`) was verified from
   `keboola/cli`'s public docs/gotchas but not yet against live constraint
   data — worth a quick live check during implementation.
4. Admin metrics web UI tracked separately:
   [keboola/agnes-the-ai-analyst#853](https://github.com/keboola/agnes-the-ai-analyst/issues/853).
