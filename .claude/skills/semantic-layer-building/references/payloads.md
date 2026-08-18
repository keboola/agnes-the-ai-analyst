# Payload shapes — the interchange format

The exact object shapes a semantic-model document uses, straight from the
vendored Apache Ossie schema (`src/semantic/schema/osi-schema.json`) — read
the live version with `agnes semantic-model schema <type>` before relying on
the examples below, since the schema is versioned (`spec_version`) and this
file can drift from a future bump.

A stored document is `{"version": "<spec_version>", "semantic_model": [{...
one model ...}]}`. Everything below is the shape of one `semantic_model`
entry.

## Top level

```yaml
version: '0.2.0.dev0'
semantic_model:
  - name: retail                 # required — the model's slug
    description: "Retail orders, customers, and revenue."
    ai_context: "Ask me about orders, revenue, and customer segments."
    datasets: [ ... ]             # required, minItems 1
    relationships: [ ... ]        # optional
    metrics: [ ... ]              # optional
    custom_extensions: [ ... ]    # optional — see "Constraints" below
```

## Dataset

```yaml
- name: orders                    # required, unique within the model
  source: analytics.orders        # required — database.schema.table, or a query
  description: "One row per completed order line."
  primary_key: [order_id]
  unique_keys: [[order_id, line_no]]
  ai_context:
    instructions: "Prefer this over cart_items for anything post-checkout."
    synonyms: ["sales orders"]
    examples: ["orders placed last week"]
  fields:
    - name: order_id
      datatype: String
      description: "Primary key."
    - name: order_date
      datatype: Date
      dimension: {is_time: true}
    - name: amount
      expression:
        dialects:
          - {dialect: duckdb, expression: "amount_cents / 100.0"}
```

`fields[].expression` is required by the schema — a plain passthrough column
still needs one (`{dialects: [{dialect: duckdb, expression: "amount"}]}`),
even though the value equals the physical column name; there is no
"bare column" shorthand.

## Metric

```yaml
- name: revenue
  description: "Total order revenue, in the target currency."
  expression:
    dialects:
      - {dialect: duckdb, expression: "SUM(amount)"}
      - {dialect: bigquery, expression: "SUM(amount)"}
  datatype: Decimal
```

`expression.dialects` needs `minItems: 1`. A metric with only a
`snowflake`-dialect expression is legitimate to declare, but
`validate-query` will mark any query using it `locally_executable: false`
against a DuckDB/BigQuery target — declare an `ansi_sql` dialect entry
instead of a single vendor-specific one when the expression is portable SQL
(it composes on every target engine, per `check_dialects` in
`src/semantic_validation.py`).

## Relationship

```yaml
- name: orders_to_customers
  from: orders                    # dataset name, many side
  to: customers                   # dataset name, one side
  from_columns: [customer_id]
  to_columns: [customer_id]
```

## Constraints (Agnes vendor extension — not core schema)

The core Apache Ossie schema has no constraint slot; Agnes constraints ride
`custom_extensions` under `vendor_name: AGNES`, `data` a JSON *string*.
Write the tag as `AGNES` (upper-case): that is the canonical spelling the
Keboola adapter emits and the only one the projector recognises when it writes
the flat `metric_definitions` / `glossary_terms` / constraint projections —
readers (the query validator and the browse UI) accept any casing, but a
document authored with a lower-case tag would silently fail to project.

```yaml
custom_extensions:
  - vendor_name: AGNES
    data: |
      {
        "constraints": [
          {
            "name": "region_filter_required",
            "constraint_type": "required_filter",
            "rule": "region = 'EU'",
            "severity": "error",
            "metrics": ["revenue"]
          }
        ]
      }
```

Field notes (`src/semantic_validation.py::extract_constraints` is the exact
reader):

- `constraint_type` — only `required_filter` is statically checkable today;
  any other value is a legitimate declaration that always degrades to a
  `post_execution_checks` entry (never guessed).
- `severity` — `"error"` (sets `valid: false` on `validate-query` when
  violated) or `"warning"` (recorded, doesn't block); anything else is
  treated as `"warning"`.
- `metrics` — the constraint only fires when a query is detected as using
  one of these metric names. An empty/absent `metrics` list means the
  constraint is never evaluated — there is no model-wide scope in the
  current convention.

## Reading these back with the agent tools

```
agnes semantic-model context dataset --id orders          # full Dataset object
agnes semantic-model context metric                       # every metric, compact
agnes semantic-model schema relationship                  # the Relationship JSON Schema
```

`get_semantic_context`'s FULL mode returns exactly the object dict stored in
the document (plus a `"model"` provenance key) — what you get back is the
same shape you'd write.
