# Semantic layer: UI browser/editor and agent parity

**Date:** 2026-08-14
**Status:** approved design, pre-implementation
**Companion spec:** `2026-08-13-open-semantic-layer-contract-design.md` (the
canonical document store this design builds on — referred to below as *the
contract spec*)

## Problem

Agnes imports a project's semantic layer (datasets, metrics, relationships,
constraints, glossary) but gives users and agents almost nothing to do with
it:

- **No UI.** The semantic layer is invisible except as flat rows on
  `/catalog/semantics` and sync counters on `/admin/semantic-layer`. There is
  no way to browse a model, inspect a dataset's fields or AI hints, follow a
  metric to its constraints, or author a new object.
- **No validation.** Imported constraints are stored as a JSON blob nobody
  evaluates. A metric expression written in one SQL dialect is silently
  composed into a query for another engine with no warning.
- **Weak agent surface.** Agents get a flat metric list. The upstream vendor's
  hosted assistant demonstrates a stronger contract over the same objects:
  regex search across the layer, typed context loading, JSON-schema
  introspection, and pre-execution query validation — plus (in an open
  upstream PR) write tools and an authoring skill.

This design closes that gap: a full browse/edit UI in the Agnes design
system, a pre-execution query validator, and an agent toolset whose names and
shapes match the vendor assistant's so agents moving between the two find the
same contract.

## Relationship to the contract spec (hard dependency)

The contract spec owns storage and ingestion:

- `semantic_models` table — canonical document (raw text + `document_json`),
  `spec_version`, `content_hash`, `source`/`source_ref` provenance,
  `status`/`validation_errors`.
- `semantic_sources` registry and adapters (native + metastore).
- Central `validate(document) -> [errors]` against the JSON schema vendored
  in-repo and pinned by `spec_version`.
- Projection into `metric_definitions` / `glossary_terms` /
  `column_metadata`, including dialect handling.
- `data_package_semantic_models` junction — RBAC rides data packages.
- The entire migration-ladder footprint. **This design adds no migration.**

This design consumes those pieces and adds no second representation: the
"graph" is the document. Objects are `{type, id, attributes}` nodes in
`document_json`; edges are the typed references already in the data
(`metric.dataset`, `relationship.from/to`, `constraint.metrics[]`,
`glossary.seeAlso`). Models are small (tens to hundreds of objects), so every
consumer below loads the document whole and resolves references in memory.

Anything storage-shaped found missing during implementation is a change
request against the contract spec, not a local workaround here.

## Design

### 1. UI

Three levels, mirroring the information architecture the vendor's own
semantic-layer editor proved out, rendered with the Agnes design system
(`base_page.html`, `ds.*` macros, `--ds-*` tokens, both themes, both chrome
layouts, `_chrome_ctx` spread on every route):

```
/semantic-layer                      model list
/semantic-layer/{slug}?tab=…         model detail; tabs: datasets (default) ·
                                     metrics · constraints · relationships · glossary
/semantic-layer/{slug}/{object_id}   object detail; edit for native models
```

- **Model list:** name, description, SQL dialect, source badge, object
  counts per type, validation status (`status='invalid'` renders the stored
  errors — an invalid import is visible, not silent).
- **Model detail:** one tab per object type; tabs are query params, so every
  view has a URL. Cross-links between tabs carry a `?q=` prefilter (metric →
  its constraints, dataset → metrics on it).
- **Object detail:** renders everything the flat projection drops — dataset
  `fields[]` as a Name/Type/Role/Description table with role/type badges; the
  `ai` block in five groups (keywords, synonyms, anti-keywords, hints,
  warnings); metric SQL fragment with its dialect; relationship with both
  sides linked.
- **Read-only rule:** objects from an imported source (`keboola_metastore`,
  `git`) show an "Imported from {source}" badge and no edit affordance.
  Native models (created in Agnes or uploaded) are editable. Write-back to an
  upstream source is explicitly phase 2 and out of scope here.
- **Existing pages stay:** `/admin/semantic-layer` remains the sync-ops view,
  `/catalog/semantics` remains the projection view. The new pages are the
  document view and hang in the nav next to Library.
- **RBAC:** model visibility follows data-package grants via the junction
  table; all mutations are `Depends(require_admin)` in v1.

### 2. Editing (native models only)

**Mutations edit the document, not rows.** Every write is: load
`document_json` → modify the object subtree → run the contract spec's central
`validate()` on the whole document → save (new `content_hash`,
`updated_at`). The editor never persists an invalid document —
`status='invalid'` is a state reserved for imports; the UI renders validation
errors inline and refuses the save.

- **Forms per object type** (fields mirror the object schemas): dataset
  (name, description, grain, primaryKey, table binding as a picker over
  `table_registry` — never free text, `fields[]` row editor, `ai` editor for
  all five groups); metric (name, description, SQL fragment + dialect select,
  dataset picker); constraint (name, type, rule, severity, metric
  multi-select); relationship (from/to dataset pickers, type, on-clause);
  glossary (term, definition, seeAlso).
- **Concurrency:** optimistic lock on `content_hash`. The form carries the
  hash it loaded; a save against a changed hash returns 409 with a reload
  prompt. Sufficient for v1's small models and admin-only mutation.
- **Referential integrity:** deleting an object that others reference
  (dataset used by a metric or relationship; metric named by a constraint)
  returns 409 listing the referrers with links. No cascade.
- **Immediate reprojection:** a successful save triggers the contract spec's
  projection for that model, so `metric_definitions` and the catalog update
  immediately instead of on the next scheduled sync.
- **Imported models refuse mutations** on every surface with an error naming
  the owning source.

### 3. Query validator

The pre-execution check the vendor assistant ships as
`validate_semantic_query`, with the same input/output contract and the same
honesty about its limits (best-effort string matching over the document —
not SQL parsing; the docstring carries the same LIMITATIONS section).

- **Input:** `sql_query`, model slug(s), optional expected semantic objects.
- **Detection:** heuristic matching of dataset names/table ids/column names/
  metric names from `document_json` against the SQL text.
- **Output** (field names match the vendor tool): `valid`,
  `used_datasets`, `used_metrics`, `matched_relationships`, `violations`
  (pre-execution, `error` severity ⇒ `valid=false`), `post_execution_checks`,
  `sql_dialects` (+ mixed-dialect warning), `summary`. One Agnes-specific
  addition: `locally_executable` — false, with a warning, when a used
  metric's only expressions are for engines other than the local one. This
  is the direct fix for today's silent dialect trap.
- **Constraints** are read from the document's `custom_extensions` under the
  Agnes vendor name, per the contract spec (the core interchange spec has no
  constraint slot). Where a rule cannot be checked statically it degrades to
  a `post_execution_checks` entry, never a false `valid=false`.
- **Gating:** like the vendor's server, the tool is only offered when the
  instance has ≥1 valid semantic model; the check fails closed.
- **Core is a pure module** (`src/semantic_validation.py`): functions over a
  document dict, no DB access, no HTTP. Every surface below wraps it.

### 4. Agent read tools and skill

Three read tools whose names and shapes match the vendor assistant's, backed
by `document_json` instead of a live metastore:

- `search_semantic_context(patterns, semantic_types?, model_ids?,
  case_sensitive?, max_results?)` — regex over names, descriptions and
  stringified attributes; compact matches with `matched_paths`, grouped by
  model.
- `get_semantic_context(selections, model_ids?)` — typed selections; empty
  `ids` returns all objects of the type compactly, explicit `ids` return
  full attributes.
- `get_semantic_schema(semantic_types)` — the JSON schema per object type,
  served from the schema vendored by the contract spec.

Together with `validate_semantic_query` (§3) and the three write tools
(§2's MCP surface) this is the full parity set. Tool docstrings follow the
vendor's WHEN TO USE / WHEN NOT TO USE / LIMITATIONS format — and are API
contract here (openapi snapshot).

**Prompt layer:** the vendor puts semantic-layer guidance in its system
prompt; the Agnes equivalent is the generated workspace `CLAUDE.md`
(`src/claude_md.py`). A "Semantic layer" section states: the layer is the
authoritative source of business meaning; prefer its definitions over
inferring from table/column names; reuse metric SQL rather than inventing
calculations; validate before running. The existing "Business Metrics" rails
section links into it rather than duplicating it.

**Skill `semantic-layer-building`** mirrors the vendor's authoring skill:
`SKILL.md` + `references/modeling-rules.md` (dataset vs. metric, naming,
grain, when a constraint) + `references/payloads.md` (object shapes — the
interchange format, not the vendor wire format) +
`references/maintenance.md` (layer audits, dead objects, drift against the
table registry). Payload validation is a call to the central `validate()`
via the CLI, not a bundled script. Distributed through the standard skill
distribution path; agents load it before using the write tools.

### 5. Surfaces (REST × CLI × MCP — parity enforced by the coverage ratchet)

| Capability | REST | CLI | MCP |
|---|---|---|---|
| List/read models | contract spec's | contract spec's | contract spec's |
| Create native model | `POST /api/admin/semantic-models` | `agnes admin semantic-model create` | `apply_semantic_model` |
| Mutate object | `POST/PUT/DELETE /api/admin/semantic-models/{slug}/objects[/{id}]` | `agnes admin semantic-model object add\|update\|remove` | `update_semantic_objects`, `delete_semantic_objects` |
| Validate query | `POST /api/semantic-models/validate-query` | `agnes semantic-model validate-query` | `validate_semantic_query` |
| Search/context/schema | `GET /api/semantic-models/context…` | `agnes semantic-model search\|show` | `search_semantic_context`, `get_semantic_context`, `get_semantic_schema` |

- Mutation endpoints: `require_admin` + CSRF on web POSTs; MCP write tools
  gated on an admin PAT.
- Read endpoints: not admin-only — analysts and agents are the audience;
  model visibility RBAC applies.
- `agnes query` integration: with an active semantic layer, a query that
  trips a constraint gets a `[semantic]` stderr note (advisory, never
  blocking).
- MCP foundation tools: the pinned exact-set test (`test_mcp_http.py`) and
  the 4-place registration checklist apply; run the pin test locally.

### 6. Testing

- **Vendor-shape goldens:** fixture document → `search`/`get`/`validate`
  outputs snapshot-tested against the documented shapes.
- **Editor:** happy path per object type; both 409 paths (stale hash,
  referenced delete); "imported model refuses mutation" on REST and MCP.
- **Validator:** case table — constraint violation, dialect mix,
  `locally_executable=false`, statically-uncheckable rule degrades to
  post-execution, no-models gating.
- **UI contract:** pages pass `test_design_system_contract.py` and
  `test_ui_layout_theme.py` (both themes × both layouts); read-only badge
  asserted for imported sources.
- Every new test is run against unfixed code first to prove it can fail.

## Sequencing

The contract spec's storage slice must land first; every surface here except
two reads or writes `semantic_models`. Implementation order:

1. **Now, independent of storage:** `src/semantic_validation.py` (pure
   functions over a document dict) with its case-table tests; the
   `semantic-layer-building` skill content.
2. **After the storage slice lands:** repo-layer wiring, REST/CLI/MCP
   surfaces, UI pages, reprojection hook, CLAUDE.md section (it reads model
   presence).

## Out of scope

- Write-back to an upstream metastore (phase 2; the editor's document-level
  write path is the seam it will plug into).
- Retiring `/admin/semantic-layer` or `/catalog/semantics`.
- Any schema migration (the ladder footprint is entirely the contract
  spec's).
- Multi-user collaborative editing beyond the optimistic lock.
- SQL parsing in the validator (heuristic by declared contract).
