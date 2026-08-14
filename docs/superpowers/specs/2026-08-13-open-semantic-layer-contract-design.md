# Open semantic-layer contract: canonical Ossie document, adapter seam, git transport

**Date:** 2026-08-13
**Status:** Approved design (brainstorm output), pre-implementation
**Builds on:** `2026-07-15-keboola-semantic-layer-importer-design.md`,
`2026-07-28-semantic-layer-sources-design.md`

## Problem

Agnes ingests a semantic layer from exactly one kind of source and flattens it on
the way in. The importer reads a metastore's model, dataset, metric, constraint,
relationship and glossary objects, then projects them onto two flat tables —
`metric_definitions` and `glossary_terms`. Everything the flat shape has no column
for is dropped at the door:

- the model's declared SQL dialect (never read, while composed metric SQL is
  emitted verbatim into a DuckDB query — a live correctness bug, not just a
  fidelity gap),
- per-column `fields[]` (name, type, role, description); only the primary key
  survives, as `dimensions`,
- keyword-style AI metadata (synonyms, hints and warnings are carried; keywords
  are not),
- constraints, stored as a JSON blob no code evaluates,
- relationships, honored only in one narrow single-relationship case,
- the second and any further model in a project — the importer takes `models[0]`
  and logs a warning.

The dropped material is not recoverable, because nothing keeps the source
document. Re-reading it means re-fetching from the source.

Two consequences follow. First, fidelity cannot be improved incrementally: every
newly-preserved attribute needs a new column somewhere. Second, and more
importantly for this design, **there is no way to accept a semantic layer that
did not come from that one source.** An organization that already models its
metrics in another tool has no path in, and Agnes has no path out — the flat
tables are not a format anyone else reads.

Meanwhile a vendor-neutral standard for exactly this material now exists.

## Background: Apache Ossie

The Open Semantic Interchange initiative published a v1.0 specification on
2026-01-27 under Apache 2.0 and has since moved into the Apache Incubator as
**Apache Ossie** (renamed to avoid the OSI acronym collision). It defines a
vendor-neutral model for semantic-layer constructs and ships machine-readable
schemas plus reference converters.

The document shape (from `core-spec/spec.md`):

```yaml
version: "<spec version>"
semantic_model:
  - name: retail_model
    description: ...
    ai_context: {instructions: ..., synonyms: [...], examples: [...]}
    datasets:
      - name: orders
        source: "db.schema.orders"        # physical table reference
        primary_key: [order_id]
        unique_keys: [[...]]
        fields:
          - name: order_date
            expression:
              dialects: [{dialect: ANSI_SQL, expression: "order_date"}]
            datatype: Date                # String|Integer|Decimal|Float|Boolean|
            dimension: {is_time: true}    #   Date|Time|DateTime|DateTimeTz|Opaque
            description: ...
            ai_context: {synonyms: [...]}
    relationships:
      - name: orders_to_customers
        from: orders                      # "many" side
        to: customers                     # "one" side
        from_columns: [customer_id]
        to_columns: [id]
    metrics:
      - name: revenue
        expression:
          dialects: [{dialect: ANSI_SQL, expression: "SUM(...)"}]
        datatype: Decimal
        ai_context: {synonyms: [...]}
    custom_extensions:
      - {vendor_name: <VENDOR>, data: "<JSON string>"}
```

Three properties make it the right canonical shape for Agnes:

1. **It is a graph in a document.** Datasets, per-column fields, relationships
   and metrics live in one tree. Nothing about the incoming structure needs a
   graph database or a table per object type; storing the document whole
   preserves the graph, and relational projections stay derived views of it.
2. **`ai_context` is first-class at every level.** Instructions, synonyms and
   examples are exactly the agent-facing material Agnes already curates on data
   packages (`when_to_use`, `example_questions`).
3. **`custom_extensions` is a sanctioned seam.** Agnes-specific attributes
   (`query_mode`, registry ids, remote-attach hints) attach without corrupting
   the portable core, and without forking the schema.

Converters are per-format installable Python packages (`pip install
apache-ossie-<format>`) exposing both directions as classes plus a CLI. Adding a
new source format is a dependency and a thin wrapper, not a new parser.

## Goals

1. One canonical, lossless representation of a semantic layer inside Agnes: the
   Ossie document as received, stored whole and validated against a pinned
   schema.
2. An adapter seam so any source format can be added additively, with the first
   two adapters proving the seam against both a native and a non-native shape.
3. Git as a first-class transport, so a semantic layer maintained in a repo
   syncs into an instance the way marketplace content already does.
4. Provenance and prune isolation per source, so several sources coexist without
   deleting each other's rows.
5. Export: an instance can emit its semantic models as Ossie documents.

## Non-goals

Deliberately out of scope; these belong to the parallel UI/fidelity effort and
build **on** this seam:

- A semantic-layer editor in the Agnes UI.
- Surfacing negative signals ("do not use this dataset for X") as a product
  feature.
- A pre-execution query validator that checks SQL against constraints and
  dialect before running it.
- Write-back to an upstream source.
- Retiring the existing flat projections. They stay; they gain a source.

## Design

### 1. Canonical store

New table `semantic_models`, with the mandatory DuckDB and Postgres siblings
(`src/repositories/semantic_models.py` + `_pg.py`), factory dispatch entry, and a
cross-engine contract test in the same change.

| Column | Purpose |
|---|---|
| `id` | PK |
| `slug` | stable handle for CLI/API/export filename |
| `name`, `description` | from the document, denormalized for listing |
| `document` | the document text exactly as the adapter produced it, never re-serialized |
| `document_json` | parsed form, for queries and projection |
| `spec_version` | schema version the document validated against |
| `content_hash` | unchanged re-import is a no-op write |
| `source`, `source_ref` | provenance, same vocabulary as `metric_definitions` |
| `status`, `validated_at`, `validation_errors` | validation outcome, honest failure state |
| `created_at`, `updated_at` | |

Junction `data_package_semantic_models` links models to data packages M:N. The
data package remains the distribution and RBAC unit; the model carries meaning.
This split matters because a model is inherently cross-table — relationships and
cross-dataset metrics span whatever any one package curates.

`ResourceType.SEMANTIC_MODEL` is registered with a `ResourceTypeSpec` and a
`list_blocks` projection delegate. No DB migration for the resource type itself.

**The document is the owner; every projection is a derivative** and can be
regenerated from it at any time. That single rule is what makes fidelity
improvable later without another migration: a newly-projected attribute reads
from a document that already contains it.

### 2. Adapter contract

An adapter is a module that returns validated Ossie documents and nothing else:

```python
def extract_semantic_models(config: dict) -> list[str]:
    """Return Ossie documents (YAML text) for this source."""
```

Adapters never write to `semantic_models`, `metric_definitions`, `glossary_terms`
or `column_metadata`. Validation happens centrally, against the JSON schema
vendored into the repo and pinned by `spec_version`; an invalid document is
stored with `status='invalid'` and its errors, never silently partially applied.
This mirrors the `extract.duckdb` contract for data-source connectors: one output
shape, one place that consumes it.

Agnes-specific attributes travel in `custom_extensions` under a single Agnes
vendor name, never in core fields.

Slice 1 ships two adapters:

- **native** — the source already publishes Ossie documents; the adapter is
  identity plus validation.
- **Keboola metastore** — the existing importer (`connectors/keboola/`), rewired
  to compose one document from the six object types it already fetches, instead
  of flattening to two tables. Nothing new is fetched; the same payload stops
  being discarded.

The pair is deliberate: one adapter whose source is already Ossie, one whose
source is a different shape entirely. A seam proven only against its own native
format is not proven.

### 3. Transports and the source registry

Three transports, one registry:

- **git** — a registered repository, cloned on a schedule, documents discovered by
  glob. Reuses the existing marketplace clone path (`src/marketplace.py`).
- **upload** — a document POSTed to the admin API.
- **connection** — an existing configured data-source connection, as today.

The 2026-07-28 design deliberately derived semantic sources from existing
connections rather than introducing a registry entity. A git repository cannot be
derived from anything, so this design adds `semantic_sources` (id, kind, name,
config, schedule, last-sync state) and reclassifies that non-goal. Connection-backed
sources keep working; they become rows with `kind='connection'`.

### 4. Projection, provenance, prune, ownership

Import is: fetch → validate → store document (skip if `content_hash` unchanged) →
project.

Projection writes `metric_definitions`, `glossary_terms` and `column_metadata`
stamped with the model's `source` and `source_ref`. Prune is scoped to that
`source_ref` — the isolation model already proven for YAML metric imports and
per-connection metastore syncs.

**One exception, found during implementation and not designed around:**
`column_metadata` has no `source_ref` column — only `source` — so column-level
prune can be scoped no finer than `(table_id, source)`. The consequence is
narrow but real: two sources that share a `source` value AND describe the same
physical table can prune each other's column descriptions. Metrics and glossary
terms are unaffected; both carry `source_ref`.

The clean fix is to add `source_ref` to `column_metadata` on both ladders. It is
deliberately NOT bundled into this design: it widens the migration beyond the
tables this contract introduces, and the collision requires two sources sharing
a `source` value, which the source registry does not currently produce. Revisit
it the moment a second adapter writes columns under an existing `source`.

**Imported rows are read-only in the UI.** Editing an imported definition in place
would put the editor in a race with a scheduled importer that prunes what upstream
no longer has — silent loss of fresh edits, the exact failure this repo has already
seen with hand-run sync scripts. Edits belong in the source: a commit for git-backed
models, a write to the upstream API for connection-backed ones. The write-back path
itself is out of scope here and is the parallel effort's concern; this design only
guarantees there is exactly one owner.

### 5. Dialect handling

Read the DuckDB expression when the document offers one; otherwise ANSI SQL, which
DuckDB accepts.

When a metric offers neither — only a warehouse-specific dialect — and the target
table is local, the metric is **marked unusable locally** with the reason, not
composed into SQL that will fail or, worse, silently mean something else. This
replaces today's behavior of splicing a foreign-dialect fragment into a DuckDB
query.

### 6. Surfaces

Per the command-UX standard and the REST × CLI × MCP coverage gate:

- **REST** — `/api/admin/semantic-models` (CRUD, admin-gated),
  `/api/admin/semantic-sources` (CRUD + trigger sync),
  `GET /api/semantic-models/{slug}.yaml` (export, resource-gated).
- **CLI** — `agnes admin semantic-model list|show|import|export|validate`,
  `agnes admin semantic-source add|list|sync`. Positional search term where a
  search exists, `--limit`, `--json`; not-found errors hint the next step.
- **MCP** — read-only lookup and search over models, defined once in the
  server-side foundation tools.

### 7. Testing

- Cross-engine contract test for the new repository pair, both backends through
  one assertion set.
- **Golden regression:** a metastore fixture through the new path must produce
  projections equal to today's output. Nothing that works today may change.
- **Explicit non-loss assertions**, one per attribute the current importer
  discards: per-column fields, keyword metadata, the second and further model,
  the declared dialect, relationships beyond the single narrow case, constraints.
  Each asserts presence in the stored document — the guarantee this design makes —
  independent of whether a projection surfaces it yet.
- Schema validation against fixtures drawn from the upstream examples.
- Migration ladder: matching DuckDB step and Alembic revision, both reaching the
  same endpoint.

## Upstream contributions

Two gaps found while mapping Agnes and a metastore's model onto the spec, both
worth raising with the incubating project rather than working around silently:

1. **No DuckDB dialect** in the dialect enumeration, though DuckDB is the query
   engine here and increasingly common as a local analytics target.
2. **No slot for negative guidance.** `ai_context` carries instructions, synonyms
   and examples, but there is no "do not use this for X" field — while at least
   one production semantic layer already models anti-keywords, and Agnes data
   packages already carry `when_not_to_use`. Until then it rides
   `custom_extensions`, which is portable in storage but not in meaning.

**Constraints** have no home in the core spec either; they travel as an extension.
This is a real portability limit and should be stated plainly rather than sold as
full interchange.

## Risks and open questions

- **The spec is moving.** The initiative announced v1.0 as finalized, but the
  incubating repository's core spec carries a `0.2.0.dev0` version and describes
  the schema as mutable pre-release. Mitigation: vendor the schema, pin
  `spec_version` per document, treat an upgrade as a deliberate change with its
  own test run.
- **Converter reuse is unproven in slice 1.** Both slice-1 adapters are ours; the
  claim that further formats are cheap rests on upstream converter packages and is
  only tested when the first one is adopted.
- **Migration number collision.** Several branches are open against the schema
  ladder concurrently; the step number must be confirmed immediately before
  implementation, not taken from this document.
- **Document size.** Documents are stored inline. If real-world models turn out
  large enough to strain row storage, the fallback is content-addressed files on
  the data volume with the hash in the row — deliberately not designed now.

## Slices

Slices 1 and 2 together are the agreed first delivery — the seam is not
considered proven until a non-native source rides it. They are separated here
because each is independently reviewable, not because either ships alone.

1. **Seam** — `semantic_models` + `semantic_sources` tables, both backends,
   validation, native adapter, upload and git transports, projection with
   per-source prune, export, admin API, CLI, MCP read tools.
2. **Keboola metastore adapter rewired** — the existing importer emits a
   document; golden regression plus the non-loss assertions.
3. **First upstream converter** — adopt a converter package and add the
   corresponding source kind, proving the additive path.
