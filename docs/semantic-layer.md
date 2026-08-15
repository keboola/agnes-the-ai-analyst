# Semantic layer

A semantic model describes what your data *means* — which datasets exist, what
their columns are, how they join, and which metrics are computed from them.
Agnes stores one as a document in the [Apache Ossie](https://ossie.apache.org/)
format (incubating; the vendor-neutral successor to the Open Semantic
Interchange initiative), and derives its own flat tables from it.

> **The document is the owner.** `metric_definitions`, `glossary_terms` and
> `column_metadata` are projections of a stored document and can be regenerated
> from it. That is what lets fidelity improve later without a migration: an
> attribute Agnes does not surface yet is still *in* the document.

Design rationale:
[`superpowers/specs/2026-08-13-open-semantic-layer-contract-design.md`](superpowers/specs/2026-08-13-open-semantic-layer-contract-design.md).

---

## What a document looks like

```yaml
version: "0.2.0.dev0"
semantic_model:
  - name: retail
    ai_context:
      instructions: Use for order-level revenue questions.
      synonyms: [sales, orders]
    datasets:
      - name: orders
        source: "db.public.orders"
        primary_key: [order_id]
        fields:
          - name: order_date
            datatype: Date
            dimension: {is_time: true}
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: order_date
    relationships:
      - name: orders_to_customers
        from: orders
        to: customers
        from_columns: [customer_id]
        to_columns: [id]
    metrics:
      - name: revenue
        datatype: Decimal
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: SUM(amount)
```

Three shapes trip people up, all enforced by the schema:

- `datasets` is required and must be non-empty. A model with `datasets: []` is
  not a valid document.
- `custom_extensions[].data` is a **JSON-encoded string**, not a nested mapping.
  Write it with `json.dumps`, read it with `json.loads`.
- `expression` is required on a field. A plain column still needs a
  pass-through expression naming itself.

Agnes-specific attributes (registry ids, `query_mode`, constraint and glossary
payloads that core Ossie has no slot for) travel in `custom_extensions` under
the Agnes vendor name — never as new top-level keys, which the schema rejects.

## Sources

A source is where documents come from. Three kinds:

| Kind | Config | What happens |
|---|---|---|
| `git` | `repo_url`, `ref`, `glob`, optional `token_env` | Shallow-clone, read every file the glob matches, containment-checked against the clone root |
| `upload` | `documents` | Documents handed over directly (admin API / CLI) |
| `connection` | connector-specific | The adapter fetches from a configured data-source connection |

```bash
agnes admin semantic-source add --kind git \
  --name "Finance models" \
  --repo-url https://example.com/semantics.git \
  --ref main --glob 'semantic/**/*.yaml'

agnes admin semantic-source sync <source-id>
```

A sync that cannot fetch **fails loudly and imports nothing**. This matters more
than it sounds: an empty document list legitimately means "upstream deleted
everything", which prunes. A failed clone must never be able to present itself
as an empty source, so the error is recorded on the source row and re-raised.

## Adapters — adding a source format

An adapter turns one source's payload into Ossie documents and does nothing
else. It never writes to the database; validation and persistence happen once,
centrally.

```python
class MyAdapter:
    def extract(self, config: dict) -> list[str]:
        """Return Ossie documents as text."""
```

Register it in `src/semantic/adapters/__init__.py`. That is the whole contract —
which is the point: a new format is one function, not a new write path.

Return the document text **as produced**, never re-serialized through a YAML
dumper. Export hands that exact text back out, so a round-trip through
parse-and-dump would silently reorder keys and strip comments.

Two adapters ship today: `native` (the source already publishes Ossie) and
`keboola_metastore` (composes a document from a Keboola project's metastore
objects).

## Ownership: imported models are read-only

A model that came from a registered source cannot be edited through the API —
`PUT` returns `409 source_owned` and names where to change it instead. Edit it
at the source: a commit in the git repo, or upstream for a connection-backed
source. A model created directly through the admin API stays editable.

This is not bureaucracy. A scheduled sync prunes what upstream no longer has, so
an edit made downstream would be reverted on the next run — silently, and at an
unpredictable time.

## Provenance and pruning

Every projected row is stamped with the model's `source` and `source_ref`, and a
sync prunes only within its own `(source, source_ref)`. Two sources can never
delete each other's rows.

One documented exception: `column_metadata` has no `source_ref` column, so column
descriptions prune at `(table_id, source)` granularity. Two sources sharing a
`source` value *and* describing the same physical table can prune each other's
column descriptions. Metrics and glossary terms are unaffected.

## Export

```bash
agnes admin semantic-model export retail > retail.yaml
```

Or over HTTP, gated on a grant for a Data Package the model is linked to (or a
direct grant on the model):

```
GET /api/semantic-models/retail.yaml
```

The bytes you get back are the bytes that were stored.

## Commands

```bash
agnes admin semantic-model list [--json] [--limit N]
agnes admin semantic-model show <slug>
agnes admin semantic-model import <file>
agnes admin semantic-model export <slug>
agnes admin semantic-model validate <file>   # offline: no server, no token

agnes admin semantic-source add ... | list | sync <id>
```

`validate` deliberately needs neither a server nor a token — someone fixing a
document should not need an instance to check their work.
