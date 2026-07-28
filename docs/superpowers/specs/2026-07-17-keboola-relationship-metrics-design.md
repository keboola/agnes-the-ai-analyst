# Keboola Semantic Layer — Relationship-Based JOIN Metrics — Design

Status: approved for implementation planning
Date: 2026-07-17

## Motivation

The Keboola semantic-layer importer (`connectors/keboola/semantic_layer.py`,
shipped in [keboola/agnes-the-ai-analyst#873](https://github.com/keboola/agnes-the-ai-analyst/issues/873))
skips any `semantic-metric` whose SQL expression references a column
qualified by an alias other than its own dataset (`skip_reason =
"foreign_alias_reference"`). These are genuine multi-table metrics whose join
condition lives in `semantic-relationship`, which v1 explicitly deferred. In
the one live project sampled during that design, 7 of 36 metrics (~19%) hit
this skip path — a real, non-trivial gap. This design adds relationship
resolution so those metrics can be composed as JOIN SQL instead of silently
dropped, while preserving the "skip and count, never guess" contract for
anything the resolver can't handle unambiguously.

## Background — verified facts

Verified live against the same test project's Metastore API used for both
prior designs (master-token GET, structural/aggregate inspection only — no
real table, column, or entity names are reproduced in this document).

### `semantic-relationship` wire shape

```
{
  "type": "semantic-relationship",
  "id": "<uuid>",
  "attributes": {
    "name": "<str, slug, no spaces>",
    "modelUUID": "<uuid>",
    "type": "<str — only \"left\" observed in the sample (29/29 items)>",
    "on": "<str, consistent pattern: alias.\"column\" = alias.\"column\">",
    "from": "<str, tableId — 2 dots, same shape as semantic-dataset.tableId>",
    "to": "<str, tableId — 2 dots, same shape as semantic-dataset.tableId>"
  },
  "meta": {...}
}
```

### Cross-check: relationship aliases vs. metric SQL aliases

The critical open question from the original design's "Open risks" section
was whether a metric's foreign-alias reference can be resolved against
`semantic-relationship` data. Verified against the live sample (36 metrics,
7 with foreign-alias references; 29 relationships):

- **Alias names do NOT match between the two item types.** For all 7
  foreign-alias metrics, the alias letter(s) used in `semantic-metric.sql`
  (e.g. a single- or multi-character local alias) did not equal either side's
  alias in any `semantic-relationship.on` clause touching that metric's own
  dataset — 0/7 direct matches. Alias strings are chosen independently by
  whoever authored each item; there is no shared alias registry per model.
  Resolving by alias-name string match is **not viable**.
- **Dataset connectivity resolves unambiguously instead.** For every one of
  the 7 foreign-alias metrics, exactly **one** `semantic-relationship` item
  has `from == metric.dataset OR to == metric.dataset`. No metric in the
  sample had zero or multiple candidate relationships.
- One metric used two distinct alias spellings in its SQL for what resolves
  to the same single relationship — confirming the metric's local alias is
  just an author-chosen SQL label, not a semantic-layer-wide identifier.
- In all 7 cases, the metric's own dataset was the relationship's `to` side
  and the foreign dataset was the `from` side, with `type == "left"`.
  **The sample never exercised the reverse case** (metric's dataset on the
  `from` side) — what `type == "left"` means for row-preservation semantics
  when the metric's own dataset is the `from` side, rather than the `to`
  side, is unverified. Composing `FROM t LEFT JOIN joined` unconditionally
  regardless of which side `t` was on would risk silently flipping which
  rows get preserved vs. NULL-padded for that unverified case — a
  syntactically valid but semantically **wrong** query, which is strictly
  worse than skipping. v1 scope below restricts composition to the verified
  direction only and skips the rest.

This means join-path resolution must key on **dataset identity** (which
tableId sits on which side of which relationship), not on alias text — a
materially different mechanism than initially assumed possible.

## Scope (v1)

**In scope:**
- Relationship resolution keyed on dataset connectivity: for a metric whose
  SQL references a foreign alias, look up `semantic-relationship` items where
  `from == metric.dataset OR to == metric.dataset`.
  - Exactly one match → compose a JOIN using that relationship, **only when
    the metric's dataset is on the relationship's `to` side** (the only
    direction verified live — see Background). `t` is always the side that
    matches the metric's own dataset, so restricting to the verified `to`
    case is equivalent to: skip whenever the metric's dataset is the `from`
    side.
  - Metric's dataset is the `from` side → skip and count
    (reason `unverified_relationship_direction`) — direction semantics
    unconfirmed, never guess.
  - Zero or two-or-more matches → skip and count
    (reason `ambiguous_relationship`), same as any other unresolvable case.
- `type == "left"` only — the only value observed live. Any other value
  (unconfirmed semantics) → skip and count (reason `unsupported_relationship_type`).
- Single-hop joins only. No metric in the sample needed more than one
  relationship; multi-hop (chained joins across 2+ relationships) is out of
  scope until real evidence of need appears.
- `metric_definitions.tables` (existing `VARCHAR[]` column, currently unused
  by the Keboola importer, already consumed by
  `MetricRepository.get_table_map()` for multi-table metrics) gets populated
  with `[primary_table, joined_table]`; `table_name` stays the primary/anchor
  table. No new column, no new migration, no new parity work.

**Out of scope, explicitly:**
- Multi-hop / transitive join resolution across chains of relationships.
- Any `semantic-relationship.type` other than `"left"`.
- Changing the `metric_definitions` schema. This feature is purely additive
  logic inside the existing importer module — same table, same repository,
  same factory.
- Resolving relationships that don't involve the metric's own dataset at all
  (e.g. relationships between two other unrelated datasets) — irrelevant to
  metric composition and not fetched into any lookup beyond what's needed.

## Architecture

Extends `connectors/keboola/semantic_layer.py` in place — no new module,
no new table, no new scheduler job.

```
sync_semantic_layer()
        │
        ├─ existing: fetch semantic-model / semantic-dataset / semantic-metric
        │            / semantic-constraint  (unchanged)
        │
        ├─ NEW: fetch semantic-relationship
        │     → relationship_lookup_by_dataset(items)
        │       {tableId: [relationship, ...]}  (a dataset can appear on
        │        either side of multiple relationships; ambiguity is
        │        detected per-metric at resolution time, not pre-filtered)
        │
        └─ build_metric_row() — NEW step inserted before the existing
           foreign_alias_reference skip check:
              │
              │  references_foreign_alias(expression) == True?
              │        │
              │        ▼
              │  resolve_relationship(metric.dataset, relationship_lookup)
              │        │
              │        ├─ exactly 1 match, type == "left",
              │        │  metric.dataset == relationship.to (verified direction)
              │        │     → rewrite expression's foreign alias(es) to a
              │        │       canonical join alias, compose:
              │        │       SELECT {rewritten} FROM "{t}" AS t
              │        │       LEFT JOIN "{joined}" AS j ON {on, remapped}
              │        │     → tables = [t, joined]; continue as a normal row
              │        │
              │        ├─ exactly 1 match, but metric.dataset == relationship.from
              │        │  (unverified direction)
              │        │     → skip_reason = "unverified_relationship_direction"
              │        │
              │        └─ 0 or 2+ matches, or unsupported type
              │              → skip_reason = "ambiguous_relationship"
              │                 / "unsupported_relationship_type"
              │
              │  (unresolved, e.g. joined dataset not registered in Agnes's
              │   table_registry either)
              │        → falls through to the existing
              │          skip_reason = "foreign_alias_reference" path,
              │          UNCHANGED — no regression for anything the new
              │          resolver can't handle
```

### Field mapping (additions only — existing mapping table from the
original importer design is unchanged for single-table metrics)

| Keboola | Agnes `metric_definitions` |
|---|---|
| primary `semantic-dataset.tableId` → resolved table | `table_name` (unchanged) |
| joined dataset's resolved table | appended to `tables[]` |
| relationship `on` (alias-remapped) | folded into composed `sql` |
| — | `skip_reason = "ambiguous_relationship"` (new) |
| — | `skip_reason = "unsupported_relationship_type"` (new) |
| — | `skip_reason = "unverified_relationship_direction"` (new) |

`skip_reason` values stay bare (no `skipped_` prefix), matching the existing
convention (`"foreign_alias_reference"`, `"unresolved_table"`,
`"missing_name"`, `"embedded_sql_comment"`); the sync result's aggregate
counters use the `skipped_<name>` prefix, e.g. `skipped_ambiguous_relationship`.

### Table resolution requirement

The **joined** dataset's `tableId` must also resolve against Agnes's
`table_registry` (same `resolve_table_name()` mechanism already used for the
metric's primary dataset). A relationship whose foreign dataset isn't a
registered Agnes table cannot be composed — falls through to the existing
`foreign_alias_reference` skip, not a hard error.

The wire shape's `on` field (`alias."column" = alias."column"`) doesn't
itself label which alias belongs to `from` vs. `to` — determining that
mapping (needed to remap the ON-clause onto `t`/`j`) is an implementation
detail for the plan, not this design; the important contract is the same as
elsewhere in this importer: when the mapping can't be determined with
confidence, skip and count rather than guess.

### Error handling

- Everything the existing importer's error contract already guarantees
  (Metastore/Storage preflight failures abort the whole run; empty responses
  never prune existing rows) is unchanged and now also covers the
  `semantic-relationship` fetch — a failed relationship fetch aborts the run
  the same way a failed metric fetch does, not a partial degrade to
  single-table-only mode.
- Ambiguous relationship (0 or 2+ candidates) → skip and count, never guess
  which one was intended.
- Unsupported `type` value → skip and count, never assume it behaves like
  `"left"`.
- Metric's dataset on the relationship's unverified (`from`) side → skip and
  count, never assume the row-preservation semantics mirror the verified
  (`to`) case.
- A metric with foreign-alias references that the resolver can't handle for
  any reason falls through to the pre-existing `foreign_alias_reference` skip
  path unchanged — this feature can only ever *rescue* metrics that were
  previously skipped, never change the outcome for a metric it doesn't
  successfully resolve.

### Testing

- Pure-function tests for `resolve_relationship()`: exactly-one-match on the
  verified (`to`) side succeeds, exactly-one-match on the unverified
  (`from`) side skips with `unverified_relationship_direction`, zero-match
  skip, multiple-match skip, unsupported-type skip — mirrors
  `test_keboola_semantic_layer_mapping.py`'s structure.
- Pure-function tests for alias rewriting and JOIN SQL composition,
  including the observed real case of multiple distinct local alias
  spellings resolving to the same relationship.
- Explicit test: joined dataset resolves a relationship but is **not**
  itself registered in `table_registry` → falls through to the existing
  `foreign_alias_reference` skip, not a crash and not a new skip reason.
- Orchestrator tests extending `test_keboola_semantic_layer_sync.py`:
  end-to-end sync producing a JOIN metric row with correct `tables[]`;
  regression test confirming existing single-table metrics compose
  identically to before (no behavior change for the unaffected path).
- Regression test: a metric that would have hit
  `skip_reason="foreign_alias_reference"` pre-this-feature now either
  resolves successfully or hits one of the new, more specific skip reasons —
  never silently reverts to the old generic skip when a specific reason is
  determinable.
- CHANGELOG bullet in the implementing PR.

## Alternatives considered (rejected)

- **Alias-name matching between metric SQL and relationship `on` clauses.**
  Empirically rejected — verified live that 0/7 real foreign-alias metrics
  would resolve this way. The alias namespaces are independent.
- **Multi-hop transitive join resolution** (walk a graph of relationships to
  connect arbitrarily distant datasets). Rejected for v1 — no evidence of
  need (every sampled case was single-hop), and it would significantly
  increase the surface for ambiguous/incorrect join composition. Revisit if
  a future project's data shows multi-hop metrics.
- **Best-effort guessing when a relationship is ambiguous** (e.g. pick the
  first match, or match by naive heuristics like alias-first-letter).
  Rejected — directly violates the established "skip and count, never
  silently emit wrong SQL" contract from the original importer design; an
  incorrect JOIN is worse than a metric that stays skipped.
- **New dedicated table for relationships** (mirroring the glossary design's
  "new table" pattern). Rejected — relationships aren't a queryable business
  entity on their own in Agnes; they exist only to compose correct metric
  SQL. Persisting them separately would add parity/migration work with no
  consumer.

## Open risks / follow-ups

1. **`semantic-relationship.type` values other than `"left"` are unverified.**
   Only one project, one value, observed. If a future project surfaces
   `"inner"`/`"right"`/`"full"` (or something else entirely), those metrics
   will skip via `skipped_unsupported_relationship_type` until a follow-up
   confirms the semantics and extends support.
2. **Multi-hop joins are unimplemented.** No evidence of need yet; the
   `skipped_ambiguous_relationship` counter will surface it if a future
   project's metrics need 2+ hops (a metric with 2+ candidate relationships
   for the same dataset might actually need chaining rather than being
   truly ambiguous — worth inspecting real skip counts after this ships
   before assuming the counter always means "author error/edge case").
3. Depends on the same **master-token requirement** as both prior designs —
   no new operational cost, just inherited.
