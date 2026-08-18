# Modeling rules

How to decide what an object is, and how to name and grain it, before you
write any YAML. These rules mirror what
`src/semantic/schema/osi-schema.json` (the vendored Apache Ossie schema) and
`src/semantic_validation.py` actually enforce or read — nothing here is
aspirational.

## Dataset vs. metric

- A **dataset** is a logical table: rows you can select, with `fields[]` you
  can group or filter by. It binds to a physical `source`
  (`database.schema.table`, or a query) — never free text guessed from a
  table name.
- A **metric** is a single quantitative expression computed FROM a dataset —
  it has no rows of its own, only an `expression` (per-dialect SQL
  fragments) and, optionally, a `datatype`.

Rule of thumb: if the user would ever say "group by X" or "filter to X", X
is a dataset field, not a metric. If the user would say "what is X", X is a
metric.

**Don't model a pre-aggregated view as a dataset just because it's
convenient to query.** If a "dataset" only ever appears as the source of one
metric's `SUM`/`COUNT`, the object you actually want is a metric over the
underlying grain-level dataset — modeling the aggregate as a dataset hides
the real grain from anyone who joins it.

## Relationships

A `relationship` connects two *datasets* (`from`/`to`) via
`from_columns`/`to_columns` — never two metrics, and never a dataset field
that isn't a real key. Name a relationship for what it means in the
business, not for its mechanics (`orders_to_customers`, not `fk_customer_id`).

## Naming

- **Names are identifiers, matched by exact-ish string presence** (case-
  insensitive) by the query validator (`src/semantic_validation.py::
  _word_present`) and by `get_semantic_context`'s `ids` lookup. A name that
  is also a common English word (`amount`, `total`, `status`) produces false
  positives/negatives in both — prefer a name that's unambiguous in a SQL
  statement (`order_amount` over `amount`).
- Keep a dataset's `name` distinct from its `source`'s trailing segment only
  when they'd otherwise collide across models — the query validator also
  matches on the source's last dotted segment (`analytics.orders` matches
  `orders`), so two datasets across models both named `orders` are
  indistinguishable from SQL text alone. `get_semantic_context` disambiguates
  by attaching `"model"` to every returned object; a human reading raw SQL
  cannot.
- One name, one meaning, one casing convention per model — `snake_case`
  throughout is what every shipped example in this repo uses
  (`tests/fixtures/ossie/tpcds_semantic_model.yaml`).

## Grain

State a dataset's grain in its `description`, not just its `primary_key`.
`primary_key`/`unique_keys` tell a consumer what's unique; they don't say
what one row *means* ("one row per completed order line", not "one row per
order" — a subtle but consequential difference for anyone writing a
`COUNT(*)`). This is exactly the kind of business-rules text `CLAUDE.md`
tells an agent to trust over inference from column names — write it here so
it's trustable.

## `ai_context`

Every dataset/metric/relationship/field may carry `ai_context` — either a
bare string or `{instructions, synonyms, examples}`. This is what
`get_semantic_context`'s COMPACT mode falls back to for a summary when
`description` is absent, and what an agent reads to disambiguate an
ambiguous name. Fill it in for anything whose name alone is not
self-explanatory to someone outside the team that built it — don't leave it
for "obvious" objects only; obvious to the author is not obvious to a
model with no domain context.

## When something is a constraint, not a description

A **constraint** is a rule an agent should actively check before running a
query built from this layer — not merely explain in prose. Two signals it
belongs in a constraint rather than a `description`/`ai_context` note:

1. **Violating it produces a wrong or dangerous answer**, not just a
   suboptimal one (a required filter that scopes a metric to a region/tenant
   is the canonical case — `region_filter_required` in the fixture used by
   `tests/test_semantic_validation.py`).
2. **It's checkable, even approximately, from the query text or the query's
   result** — a constraint that can never be evaluated either way is
   documentation wearing a constraint's shape; put it in `ai_context.hints`
   instead.

Constraints ride `custom_extensions` under `vendor_name: agnes` as a JSON
string — see `references/payloads.md` for the exact shape. Only
`constraint_type: required_filter` is statically checkable today
(`src/semantic_validation.py::_STATICALLY_CHECKABLE_CONSTRAINT_TYPES`);
anything else (a value-range rule, a row-count expectation) is legitimate to
declare but will surface as a `post_execution_checks` entry from
`validate-query`, never a guessed pass/fail — write its `rule` field for a
human/agent to reason about even though the validator can't check it itself.
