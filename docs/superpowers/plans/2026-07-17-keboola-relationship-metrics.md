# Keboola Relationship-Based JOIN Metrics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the Keboola semantic-layer importer so metrics currently skipped with `skip_reason="foreign_alias_reference"` can be composed as JOIN SQL, using `semantic-relationship` Metastore items to resolve the join path — resolved by dataset connectivity (not alias-name matching, which live data proved unreliable), restricted to the one live-verified join direction and join type, falling through to the existing skip path for anything ambiguous.

**Architecture:** Purely additive changes inside the already-shipped `connectors/keboola/semantic_layer.py` — no new table, no migration, no new module, no new scheduler job. A relationship-resolution step runs inside `build_metric_row()`, before the existing `foreign_alias_reference` skip check, and falls through to that same skip path unchanged whenever it can't resolve a join with full confidence. Determining which side of a relationship's `on` clause belongs to which table (not labeled in the wire data) is resolved via existing column-metadata lookups (`column_metadata_repo()`), not guessed.

**Tech Stack:** Python, existing `metric_repo()` / `table_registry_repo()` / `column_metadata_repo()` factories, `pytest` + `unittest.mock`.

## Global Constraints

- Design: `docs/superpowers/specs/2026-07-17-keboola-relationship-metrics-design.md` (approved, includes a documented review-driven fix restricting composition to the verified join direction).
- No DB migration in this plan — `metric_definitions` schema is unchanged; this only starts *populating* the already-existing, already-unused `tables VARCHAR[]` column for JOIN metrics (`MetricRepository.get_table_map()`, `src/repositories/metrics.py:171-185`, already reads it).
- Single-hop joins only, `type == "left"` only, and only when the metric's own dataset is the relationship's `to` side (the one live-verified direction) — anything else skips and counts under a specific new reason, never guessed.
- **New dependency this plan introduces:** resolving which alias in a relationship's `on` clause belongs to which table requires that table's columns already be known to Agnes via `column_metadata_repo()` (populated by the profiler after a normal sync — `table_registry.profile_after_sync` defaults to `true`). A relationship metric whose two tables haven't been profiled yet skips via the existing generic `foreign_alias_reference` reason (column metadata absence is treated as "can't resolve with confidence," not a hard error) — this is a real, documented limitation, not a bug: note it in the CHANGELOG bullet (Task 6).
- Every existing single-table metric's composed SQL must be byte-for-byte unchanged by this plan — every task with orchestrator-level changes includes an explicit regression test asserting this.
- CHANGELOG bullet required in the final task per repo convention.
- Run `.venv/bin/pytest tests/ --tb=short -n auto -q` before considering the plan done.

---

## File Structure

| File | Responsibility |
|---|---|
| `connectors/keboola/semantic_layer.py` (modify) | New pure functions: `relationship_lookup_by_dataset`, `resolve_relationship`, `parse_on_clause`, `resolve_join_aliases`, `extract_foreign_aliases`, `compose_join_sql`, `try_join_composition`. Restructured `build_metric_row()`. Extended `sync_semantic_layer()`. |
| `tests/test_keboola_semantic_layer_mapping.py` (modify) | Pure-function tests for every new helper + restructured `build_metric_row`. |
| `tests/test_keboola_semantic_layer_sync.py` (modify) | Orchestrator tests: relationship fetch, column-metadata-driven resolution, new skip counters, single-table regression. |
| `CHANGELOG.md` (modify) | `[Unreleased]` bullet. |

---

### Task 1: Dataset-connectivity relationship resolution

**Files:**
- Modify: `connectors/keboola/semantic_layer.py` (append)
- Test: `tests/test_keboola_semantic_layer_mapping.py` (append)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `relationship_lookup_by_dataset(relationship_items: list[dict]) -> dict[str, list[dict]]`; `resolve_relationship(dataset_table_id: str, relationship_lookup: dict[str, list[dict]]) -> tuple[Optional[dict], Optional[str]]` — returns `(relationship_attrs, None)` on a single, verified-direction, supported-type match, or `(None, skip_reason)` where `skip_reason` is one of `"ambiguous_relationship"`, `"unsupported_relationship_type"`, `"unverified_relationship_direction"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_keboola_semantic_layer_mapping.py`:

```python
from connectors.keboola.semantic_layer import relationship_lookup_by_dataset, resolve_relationship


def _relationship_item(name, from_id, to_id, on, rel_type="left", model_uuid="model-1"):
    return {
        "type": "semantic-relationship",
        "id": f"id-{name}",
        "attributes": {
            "name": name, "from": from_id, "to": to_id, "on": on,
            "type": rel_type, "modelUUID": model_uuid,
        },
    }


class TestRelationshipLookupByDataset:
    def test_indexes_by_both_from_and_to(self):
        rel = _relationship_item(
            "orders_to_customers", "in.c-a.orders", "in.c-a.customers", 'o."customer_id" = c."id"'
        )
        lookup = relationship_lookup_by_dataset([rel])
        assert lookup["in.c-a.orders"] == [rel["attributes"]]
        assert lookup["in.c-a.customers"] == [rel["attributes"]]

    def test_empty_items_yields_empty_lookup(self):
        assert relationship_lookup_by_dataset([]) == {}


class TestResolveRelationship:
    def test_resolves_when_dataset_is_verified_to_side(self):
        rel_attrs = _relationship_item(
            "o_to_c", "in.c-a.orders", "in.c-a.customers", 'o."customer_id" = c."id"'
        )["attributes"]
        lookup = {"in.c-a.customers": [rel_attrs], "in.c-a.orders": [rel_attrs]}

        relationship, skip_reason = resolve_relationship("in.c-a.customers", lookup)

        assert skip_reason is None
        assert relationship == rel_attrs

    def test_skips_when_dataset_is_unverified_from_side(self):
        rel_attrs = _relationship_item(
            "o_to_c", "in.c-a.orders", "in.c-a.customers", 'o."customer_id" = c."id"'
        )["attributes"]
        lookup = {"in.c-a.customers": [rel_attrs], "in.c-a.orders": [rel_attrs]}

        relationship, skip_reason = resolve_relationship("in.c-a.orders", lookup)

        assert relationship is None
        assert skip_reason == "unverified_relationship_direction"

    def test_skips_when_no_relationship_touches_dataset(self):
        relationship, skip_reason = resolve_relationship("in.c-a.unrelated", {})
        assert relationship is None
        assert skip_reason == "ambiguous_relationship"

    def test_skips_when_multiple_relationships_touch_dataset(self):
        rel1 = _relationship_item("r1", "in.c-a.orders", "in.c-a.customers", 'o."x" = c."y"')["attributes"]
        rel2 = _relationship_item("r2", "in.c-a.payments", "in.c-a.customers", 'p."x" = c."z"')["attributes"]
        lookup = {"in.c-a.customers": [rel1, rel2]}

        relationship, skip_reason = resolve_relationship("in.c-a.customers", lookup)

        assert relationship is None
        assert skip_reason == "ambiguous_relationship"

    def test_skips_unsupported_relationship_type(self):
        rel_attrs = _relationship_item(
            "o_to_c", "in.c-a.orders", "in.c-a.customers", 'o."x" = c."y"', rel_type="inner"
        )["attributes"]
        lookup = {"in.c-a.customers": [rel_attrs]}

        relationship, skip_reason = resolve_relationship("in.c-a.customers", lookup)

        assert relationship is None
        assert skip_reason == "unsupported_relationship_type"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_keboola_semantic_layer_mapping.py -v -k Relationship`
Expected: FAIL — `ImportError: cannot import name 'relationship_lookup_by_dataset'`

- [ ] **Step 3: Implement**

Append to `connectors/keboola/semantic_layer.py`:

```python
def relationship_lookup_by_dataset(relationship_items: list[dict]) -> dict[str, list[dict]]:
    """Index semantic-relationship attributes by every tableId that appears
    on either side (from or to), so a metric's dataset can be looked up
    against every relationship touching it in O(1).

    A relationship's attributes dict is stored under BOTH its from and to
    tableId keys — resolve_relationship() below determines which side
    (verified vs. unverified direction) the caller's dataset sits on.
    """
    lookup: dict[str, list[dict]] = {}
    for item in relationship_items:
        attrs = item.get("attributes") or {}
        from_id = attrs.get("from")
        to_id = attrs.get("to")
        if from_id:
            lookup.setdefault(from_id, []).append(attrs)
        if to_id:
            lookup.setdefault(to_id, []).append(attrs)
    return lookup


def resolve_relationship(
    dataset_table_id: str,
    relationship_lookup: dict[str, list[dict]],
) -> tuple[Optional[dict], Optional[str]]:
    """Resolve exactly one semantic-relationship for a metric's dataset,
    restricted to the ONE live-verified-safe case (docs/superpowers/specs/
    2026-07-17-keboola-relationship-metrics-design.md):

    - exactly one relationship touches this dataset (from OR to side) —
      zero or multiple candidates return "ambiguous_relationship";
    - that relationship's type == "left" — the only value observed live;
      anything else returns "unsupported_relationship_type";
    - the dataset is on the relationship's "to" side — the only direction
      verified live to compose FROM t LEFT JOIN joined correctly; a
      dataset on the "from" side returns "unverified_relationship_direction"
      rather than assuming the reverse direction behaves the same way.

    Returns (relationship_attrs, None) on success, (None, skip_reason)
    otherwise. Never raises, never guesses.
    """
    candidates = relationship_lookup.get(dataset_table_id, [])
    if len(candidates) != 1:
        return None, "ambiguous_relationship"

    relationship = candidates[0]
    if relationship.get("type") != "left":
        return None, "unsupported_relationship_type"
    if relationship.get("to") != dataset_table_id:
        return None, "unverified_relationship_direction"

    return relationship, None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_keboola_semantic_layer_mapping.py -v -k Relationship`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add connectors/keboola/semantic_layer.py tests/test_keboola_semantic_layer_mapping.py
git commit -m "Add dataset-connectivity relationship resolution for Keboola metrics"
```

---

### Task 2: Resolve which `on`-clause alias belongs to which table

**Files:**
- Modify: `connectors/keboola/semantic_layer.py` (append)
- Test: `tests/test_keboola_semantic_layer_mapping.py` (append)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `parse_on_clause(on: str) -> Optional[tuple[str, str, str, str]]` (returns `(alias1, col1, alias2, col2)` or `None` if the string doesn't match the live-verified `alias."col" = alias."col"` shape); `resolve_join_aliases(on: str, from_columns: set[str], to_columns: set[str]) -> Optional[tuple[str, str]]` — returns `(to_alias, from_alias)` or `None` if the mapping can't be determined with confidence.

**Why this is needed:** live verification (design spec, "Cross-check" section) found `semantic-relationship.on` never labels which alias belongs to `from` vs. `to` — e.g. `o."customer_id" = c."id"` doesn't say whether `o` or `c` is the `from`-side alias. Guessing a fixed convention (e.g. "left operand is always `from`") would risk composing a syntactically valid but semantically wrong JOIN with no evidence it's correct. Instead: match each side's column name against the ALREADY-KNOWN column sets of the resolved `from`/`to` Agnes tables (`column_metadata_repo()`, populated by the profiler) — if exactly one of the two possible pairings is consistent with both tables' real schemas, that's the answer; otherwise, skip.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_keboola_semantic_layer_mapping.py`:

```python
from connectors.keboola.semantic_layer import parse_on_clause, resolve_join_aliases


class TestParseOnClause:
    def test_parses_standard_shape(self):
        assert parse_on_clause('o."customer_id" = c."id"') == ("o", "customer_id", "c", "id")

    def test_handles_extra_whitespace(self):
        assert parse_on_clause('o."customer_id"   =   c."id"') == ("o", "customer_id", "c", "id")

    def test_returns_none_for_unrecognized_shape(self):
        assert parse_on_clause('o.customer_id = c.id') is None
        assert parse_on_clause('some garbage') is None


class TestResolveJoinAliases:
    def test_resolves_when_only_one_pairing_matches_known_columns(self):
        # to_columns (the metric's own table) has "id"; from_columns (the
        # joined table) has "customer_id" — only alias1=o/from, alias2=c/to
        # is consistent.
        on = 'o."customer_id" = c."id"'
        from_columns = {"customer_id", "name", "email"}
        to_columns = {"id", "order_date", "amount"}

        result = resolve_join_aliases(on, from_columns, to_columns)

        assert result == ("c", "o")  # (to_alias, from_alias)

    def test_resolves_reversed_operand_order(self):
        on = 'c."id" = o."customer_id"'
        from_columns = {"customer_id", "name"}
        to_columns = {"id", "order_date"}

        result = resolve_join_aliases(on, from_columns, to_columns)

        assert result == ("c", "o")

    def test_returns_none_when_both_pairings_match(self):
        # Both tables happen to have both column names — genuinely ambiguous.
        on = 'o."x" = c."y"'
        from_columns = {"x", "y"}
        to_columns = {"x", "y"}

        assert resolve_join_aliases(on, from_columns, to_columns) is None

    def test_returns_none_when_neither_pairing_matches(self):
        on = 'o."missing_a" = c."missing_b"'
        from_columns = {"customer_id"}
        to_columns = {"id"}

        assert resolve_join_aliases(on, from_columns, to_columns) is None

    def test_returns_none_for_unparseable_on_clause(self):
        assert resolve_join_aliases("garbage", {"a"}, {"b"}) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_keboola_semantic_layer_mapping.py -v -k "OnClause or JoinAliases"`
Expected: FAIL — `ImportError: cannot import name 'parse_on_clause'`

- [ ] **Step 3: Implement**

Append to `connectors/keboola/semantic_layer.py`:

```python
# Matches the live-verified semantic-relationship.on shape exactly:
# `<alias>."<column>" = <alias>."<column>"`. Verified live (2026-07-17):
# 29/29 sampled relationships matched this pattern with no variation.
_ON_CLAUSE_RE = re.compile(
    r'^\s*(\w+)\s*\.\s*"([^"]+)"\s*=\s*(\w+)\s*\.\s*"([^"]+)"\s*$'
)


def parse_on_clause(on: str) -> Optional[tuple[str, str, str, str]]:
    """Parse a semantic-relationship.on string into (alias1, col1, alias2, col2).

    Returns None if `on` doesn't match the live-verified shape — callers
    must treat that as "can't resolve, skip" rather than a hard error.
    """
    m = _ON_CLAUSE_RE.match(on)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3), m.group(4)


def resolve_join_aliases(
    on: str,
    from_columns: set[str],
    to_columns: set[str],
) -> Optional[tuple[str, str]]:
    """Determine which of the two aliases in `on` belongs to the `to`
    (metric's own) table vs. the `from` (joined) table, by matching each
    side's column name against the real, already-known column sets of
    both tables (column_metadata_repo(), populated by the profiler).

    Returns (to_alias, from_alias) when EXACTLY ONE of the two possible
    pairings is consistent with both tables' real schemas. Returns None —
    "can't resolve with confidence" — when the on-clause doesn't parse,
    when BOTH pairings are consistent (genuinely ambiguous, e.g. both
    tables share a column name used in the join), or when NEITHER pairing
    is consistent (e.g. column metadata is missing or stale).
    """
    parsed = parse_on_clause(on)
    if parsed is None:
        return None
    alias1, col1, alias2, col2 = parsed

    # Candidate A: alias1 is the `to` side, alias2 is the `from` side.
    candidate_a = col1 in to_columns and col2 in from_columns
    # Candidate B: alias1 is the `from` side, alias2 is the `to` side.
    candidate_b = col1 in from_columns and col2 in to_columns

    if candidate_a and not candidate_b:
        return alias1, alias2
    if candidate_b and not candidate_a:
        return alias2, alias1
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_keboola_semantic_layer_mapping.py -v -k "OnClause or JoinAliases"`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add connectors/keboola/semantic_layer.py tests/test_keboola_semantic_layer_mapping.py
git commit -m "Resolve on-clause alias sides via column-metadata matching"
```

---

### Task 3: Alias rewriting and JOIN SQL composition

**Files:**
- Modify: `connectors/keboola/semantic_layer.py` (append)
- Test: `tests/test_keboola_semantic_layer_mapping.py` (append)

**Interfaces:**
- Consumes: `_mask_quoted_regions`, `_ALIAS_QUALIFIER_RE` (existing).
- Produces: `extract_foreign_aliases(expression: str) -> set[str]`; `compose_join_sql(expression: str, primary_table: str, joined_table: str, on: str, to_alias: str, from_alias: str) -> str`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_keboola_semantic_layer_mapping.py`:

```python
from connectors.keboola.semantic_layer import compose_join_sql, extract_foreign_aliases


class TestExtractForeignAliases:
    def test_extracts_single_alias(self):
        assert extract_foreign_aliases('SUM(o."amount")') == {"o"}

    def test_extracts_multiple_distinct_aliases(self):
        # Live-verified real case: a metric used two distinct local alias
        # spellings for what resolved to the SAME single relationship.
        expr = "CASE WHEN p.\"status\" = 'x' THEN SUM(pay.\"value\") ELSE 0 END"
        assert extract_foreign_aliases(expr) == {"p", "pay"}

    def test_ignores_t_alias(self):
        assert extract_foreign_aliases('SUM(t."amount")') == set()

    def test_ignores_dotted_string_literal(self):
        assert extract_foreign_aliases("COUNT(CASE WHEN \"status\" = 'in.progress' THEN 1 END)") == set()


class TestComposeJoinSql:
    def test_composes_left_join_with_rewritten_aliases(self):
        expr = 'ROUND(SUM(TRY_CAST(o."amount" AS DECIMAL(18,2))), 2)'
        sql = compose_join_sql(
            expr, "crm_activities", "crm_opportunities", 'o."opportunity_id" = a."id"', "a", "o",
        )
        assert sql == (
            'SELECT ROUND(SUM(TRY_CAST(j."amount" AS DECIMAL(18,2))), 2) '
            'FROM "crm_activities" AS t '
            'LEFT JOIN "crm_opportunities" AS j '
            'ON j."opportunity_id" = t."id"'
        )

    def test_rewrites_multiple_distinct_aliases_to_canonical_j(self):
        expr = "CASE WHEN p.\"status\" = 'x' THEN SUM(pay.\"value\") ELSE 0 END"
        sql = compose_join_sql(
            expr, "kbc_projects", "kbc_payg_payments", 'p."project_id" = k."id"', "k", "p",
        )
        assert 'p."status"' not in sql
        assert 'pay."value"' not in sql
        assert sql.count('j."') == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_keboola_semantic_layer_mapping.py -v -k "ExtractForeignAliases or ComposeJoinSql"`
Expected: FAIL — `ImportError: cannot import name 'extract_foreign_aliases'`

- [ ] **Step 3: Implement**

Append to `connectors/keboola/semantic_layer.py`:

```python
def extract_foreign_aliases(expression: str) -> set[str]:
    """Return every distinct alias (excluding `t`) that qualifies a column
    in `expression`, masking quoted regions first (same rationale as
    references_foreign_alias / has_embedded_sql_comment).

    A metric may use more than one local alias spelling for what resolves
    to the SAME single relationship (live-verified real case) — all of
    them get rewritten to the canonical join alias in compose_join_sql.
    """
    masked = _mask_quoted_regions(expression)
    aliases = {m.group(1) for m in _ALIAS_QUALIFIER_RE.finditer(masked)}
    aliases.discard("t")
    return aliases


def compose_join_sql(
    expression: str,
    primary_table: str,
    joined_table: str,
    on: str,
    to_alias: str,
    from_alias: str,
) -> str:
    """Compose a two-table LEFT JOIN metric_definitions.sql.

    `to_alias`/`from_alias` are the on-clause's alias tokens as resolved by
    resolve_join_aliases — `to_alias` corresponds to `primary_table`
    (rewritten to the canonical `t`), `from_alias` to `joined_table`
    (rewritten to the canonical `j`). Every foreign-alias-qualified column
    in `expression` (there may be multiple distinct alias spellings for the
    same joined table — see extract_foreign_aliases) is rewritten to `j.`.

    Callers MUST have already checked references_foreign_alias(expression)
    and has_embedded_sql_comment(expression) — this function does not
    itself guard against those cases (mirrors compose_sql's contract).
    """
    rewritten_expression = expression
    for alias in extract_foreign_aliases(expression):
        rewritten_expression = re.sub(
            rf'\b{re.escape(alias)}\s*\.', "j.", rewritten_expression
        )

    on_alias1, on_col1, on_alias2, on_col2 = parse_on_clause(on)  # type: ignore[misc]
    remapped_alias1 = "t" if on_alias1 == to_alias else "j"
    remapped_alias2 = "t" if on_alias2 == to_alias else "j"
    remapped_on = f'{remapped_alias1}."{on_col1}" = {remapped_alias2}."{on_col2}"'

    return (
        f'SELECT {rewritten_expression} '
        f'FROM "{primary_table}" AS t '
        f'LEFT JOIN "{joined_table}" AS j '
        f'ON {remapped_on}'
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_keboola_semantic_layer_mapping.py -v -k "ExtractForeignAliases or ComposeJoinSql"`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add connectors/keboola/semantic_layer.py tests/test_keboola_semantic_layer_mapping.py
git commit -m "Add alias rewriting and JOIN SQL composition for Keboola metrics"
```

---

### Task 4: Integrate join composition into `build_metric_row()`

**Files:**
- Modify: `connectors/keboola/semantic_layer.py` (`build_metric_row`, restructured)
- Test: `tests/test_keboola_semantic_layer_mapping.py` (append)

**Interfaces:**
- Consumes: `resolve_relationship` (Task 1), `resolve_join_aliases` (Task 2), `extract_foreign_aliases` + `compose_join_sql` (Task 3).
- Produces: `try_join_composition(expression: str, dataset_table_id: str, table_lookup: dict, relationship_lookup: dict, column_lookup: dict[str, set[str]]) -> tuple[Optional[dict], Optional[str]]` — `dict` has keys `table_name`, `tables`, `sql`. `build_metric_row()` gains two new optional keyword parameters: `relationship_lookup: Optional[dict] = None`, `column_lookup: Optional[dict] = None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_keboola_semantic_layer_mapping.py`:

```python
from connectors.keboola.semantic_layer import try_join_composition


class TestTryJoinComposition:
    def test_composes_join_when_fully_resolvable(self):
        table_lookup = {
            ("in.c-a", "activities"): "crm_activities",
            ("in.c-a", "opportunities"): "crm_opportunities",
        }
        relationship_lookup = {
            "in.c-a.activities": [
                {
                    "from": "in.c-a.opportunities", "to": "in.c-a.activities",
                    "on": 'o."id" = a."opportunity_id"', "type": "left",
                }
            ],
        }
        column_lookup = {
            "crm_activities": {"opportunity_id", "created_at"},
            "crm_opportunities": {"id", "amount"},
        }

        result, skip_reason = try_join_composition(
            'SUM(o."amount")', "in.c-a.activities", table_lookup, relationship_lookup, column_lookup,
        )

        assert skip_reason is None
        assert result["table_name"] == "crm_activities"
        assert result["tables"] == ["crm_activities", "crm_opportunities"]
        assert 'FROM "crm_activities" AS t' in result["sql"]
        assert 'LEFT JOIN "crm_opportunities" AS j' in result["sql"]

    def test_falls_back_when_relationship_unresolvable(self):
        result, skip_reason = try_join_composition(
            'SUM(o."amount")', "in.c-a.orphan", {}, {}, {},
        )
        assert result is None
        assert skip_reason == "ambiguous_relationship"

    def test_falls_back_when_joined_table_not_registered(self):
        table_lookup = {("in.c-a", "activities"): "crm_activities"}
        relationship_lookup = {
            "in.c-a.activities": [
                {"from": "in.c-a.unregistered", "to": "in.c-a.activities", "on": 'o."x" = a."y"', "type": "left"}
            ],
        }
        result, skip_reason = try_join_composition(
            'SUM(o."x")', "in.c-a.activities", table_lookup, relationship_lookup, {},
        )
        assert result is None
        assert skip_reason == "foreign_alias_reference"

    def test_falls_back_when_column_metadata_missing(self):
        table_lookup = {
            ("in.c-a", "activities"): "crm_activities",
            ("in.c-a", "opportunities"): "crm_opportunities",
        }
        relationship_lookup = {
            "in.c-a.activities": [
                {"from": "in.c-a.opportunities", "to": "in.c-a.activities", "on": 'o."id" = a."opportunity_id"', "type": "left"}
            ],
        }
        result, skip_reason = try_join_composition(
            'SUM(o."amount")', "in.c-a.activities", table_lookup, relationship_lookup, {},
        )
        assert result is None
        assert skip_reason == "foreign_alias_reference"


def _relationship_metric_item(name, sql, dataset, model_uuid="model-1"):
    return {
        "type": "semantic-metric", "id": f"id-{name}",
        "attributes": {"name": name, "sql": sql, "dataset": dataset, "modelUUID": model_uuid},
    }


class TestBuildMetricRowWithRelationships:
    def test_resolves_join_metric_when_relationship_available(self):
        table_lookup = {
            ("in.c-a", "activities"): "crm_activities",
            ("in.c-a", "opportunities"): "crm_opportunities",
        }
        relationship_lookup = {
            "in.c-a.activities": [
                {"from": "in.c-a.opportunities", "to": "in.c-a.activities", "on": 'o."id" = a."opportunity_id"', "type": "left"}
            ],
        }
        column_lookup = {
            "crm_activities": {"opportunity_id"},
            "crm_opportunities": {"id", "amount"},
        }
        metric = _relationship_metric_item("linked_amount", 'SUM(o."amount")', "in.c-a.activities")

        row, skip_reason = build_metric_row(
            metric, table_lookup, {}, [], "model-1",
            relationship_lookup=relationship_lookup, column_lookup=column_lookup,
        )

        assert skip_reason is None
        assert row["table_name"] == "crm_activities"
        assert row["tables"] == ["crm_activities", "crm_opportunities"]

    def test_falls_through_to_foreign_alias_reference_without_lookups(self):
        table_lookup = {("in.c-a", "activities"): "crm_activities"}
        metric = _relationship_metric_item("linked_amount", 'SUM(o."amount")', "in.c-a.activities")

        row, skip_reason = build_metric_row(metric, table_lookup, {}, [], "model-1")

        assert row is None
        assert skip_reason == "foreign_alias_reference"

    def test_single_table_metric_unaffected_by_new_params(self):
        table_lookup = {("in.c-a", "orders"): "crm_orders"}
        metric = _relationship_metric_item("total", 'SUM("amount")', "in.c-a.orders")

        row, skip_reason = build_metric_row(
            metric, table_lookup, {}, [], "model-1",
            relationship_lookup={}, column_lookup={},
        )

        assert skip_reason is None
        assert row["sql"] == 'SELECT SUM("amount") FROM "crm_orders" AS t'
        assert "tables" not in row
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_keboola_semantic_layer_mapping.py -v -k "TryJoinComposition or BuildMetricRowWithRelationships"`
Expected: FAIL — `ImportError: cannot import name 'try_join_composition'`

- [ ] **Step 3: Implement `try_join_composition`**

Append to `connectors/keboola/semantic_layer.py` (before `build_metric_row`):

```python
def try_join_composition(
    expression: str,
    dataset_table_id: str,
    table_lookup: dict[tuple[str, str], str],
    relationship_lookup: dict[str, list[dict]],
    column_lookup: dict[str, set[str]],
) -> tuple[Optional[dict], Optional[str]]:
    """Attempt to resolve a foreign-alias-referencing metric expression into
    a JOIN. Returns (fields, None) with 'table_name' / 'tables' / 'sql'
    keys set on success, or (None, skip_reason) — never raises, every
    failure mode is a specific skip_reason (docs/superpowers/specs/
    2026-07-17-keboola-relationship-metrics-design.md — "skip and count,
    never guess"). Any failure not covered by a resolve_relationship()
    skip_reason falls back to "foreign_alias_reference", the pre-existing
    generic reason — so this function never introduces a regression for a
    metric it can't fully resolve.
    """
    relationship, skip_reason = resolve_relationship(dataset_table_id, relationship_lookup)
    if relationship is None:
        return None, skip_reason

    table_name = resolve_table_name(dataset_table_id, table_lookup)
    joined_table_name = resolve_table_name(relationship["from"], table_lookup)
    if table_name is None or joined_table_name is None:
        return None, "foreign_alias_reference"

    to_columns = column_lookup.get(table_name)
    from_columns = column_lookup.get(joined_table_name)
    if not to_columns or not from_columns:
        return None, "foreign_alias_reference"

    alias_sides = resolve_join_aliases(relationship["on"], from_columns, to_columns)
    if alias_sides is None:
        return None, "foreign_alias_reference"
    to_alias, from_alias = alias_sides

    sql = compose_join_sql(expression, table_name, joined_table_name, relationship["on"], to_alias, from_alias)
    return {"table_name": table_name, "tables": [table_name, joined_table_name], "sql": sql}, None
```

- [ ] **Step 4: Restructure `build_metric_row`**

Replace the ENTIRE existing `build_metric_row` function body in `connectors/keboola/semantic_layer.py` with:

```python
def build_metric_row(
    metric_item: dict,
    table_lookup: dict[tuple[str, str], str],
    dataset_lookup: dict[str, dict],
    constraints: list[dict],
    model_uuid: str,
    relationship_lookup: Optional[dict[str, list[dict]]] = None,
    column_lookup: Optional[dict[str, set[str]]] = None,
) -> tuple[Optional[dict], Optional[str]]:
    """Map one semantic-metric item to a metric_definitions row dict.

    Returns (row, None) on success, or (None, skip_reason) where
    skip_reason is "missing_name", "unresolved_table", "embedded_sql_comment",
    "foreign_alias_reference" (generic fallback — see try_join_composition
    for the more specific relationship-resolution skip reasons this
    function also propagates: "ambiguous_relationship",
    "unsupported_relationship_type", "unverified_relationship_direction").

    `relationship_lookup` / `column_lookup` are optional — omitting them
    (the pre-relationship-feature call shape) preserves the exact
    pre-existing behavior: every foreign-alias expression skips as
    "foreign_alias_reference", unconditionally.
    """
    attrs = metric_item.get("attributes") or {}
    name = attrs.get("name")
    expression = attrs.get("sql") or ""
    dataset_table_id = attrs.get("dataset") or ""

    if not name:
        return None, "missing_name"

    tables: list[str] = []
    if references_foreign_alias(expression):
        if has_embedded_sql_comment(expression):
            return None, "embedded_sql_comment"
        join_fields: Optional[dict] = None
        join_skip_reason: Optional[str] = "foreign_alias_reference"
        if relationship_lookup is not None and column_lookup is not None:
            join_fields, join_skip_reason = try_join_composition(
                expression, dataset_table_id, table_lookup, relationship_lookup, column_lookup,
            )
        if join_fields is None:
            return None, join_skip_reason
        table_name = join_fields["table_name"]
        tables = join_fields["tables"]
        sql = join_fields["sql"]
    else:
        if has_embedded_sql_comment(expression):
            return None, "embedded_sql_comment"
        table_name = resolve_table_name(dataset_table_id, table_lookup)
        if table_name is None:
            return None, "unresolved_table"
        sql = compose_sql(expression, table_name)

    row: dict[str, Any] = {
        "id": f"keboola/{model_uuid}/{name}",
        "name": name,
        "display_name": name,
        "category": "keboola",
        "description": attrs.get("description") or "",
        "expression": expression,
        "table_name": table_name,
        "sql": sql,
        "source": "keboola_semantic_layer",
    }
    if tables:
        row["tables"] = tables

    dataset_attrs = dataset_lookup.get(dataset_table_id) or {}
    grain = dataset_attrs.get("grain")
    if grain:
        row["grain"] = grain
    primary_key = dataset_attrs.get("primaryKey") or []
    if primary_key:
        row["dimensions"] = list(primary_key)
    ai_block = dataset_attrs.get("ai") or {}
    synonyms = ai_block.get("synonyms") or []
    if synonyms:
        row["synonyms"] = list(synonyms)
    notes = list(ai_block.get("hints") or []) + list(ai_block.get("warnings") or [])
    if notes:
        row["notes"] = notes

    validation = merge_constraints(name, constraints)
    if validation is not None:
        row["validation"] = validation

    return row, None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_keboola_semantic_layer_mapping.py -v`
Expected: PASS (the entire file — confirms every pre-existing `build_metric_row` test from the shipped metric importer still passes unchanged, plus all new tests)

- [ ] **Step 6: Commit**

```bash
git add connectors/keboola/semantic_layer.py tests/test_keboola_semantic_layer_mapping.py
git commit -m "Integrate relationship-based JOIN composition into build_metric_row"
```

---

### Task 5: Wire relationships + column metadata into `sync_semantic_layer()`

**Files:**
- Modify: `connectors/keboola/semantic_layer.py` (`sync_semantic_layer`)
- Test: `tests/test_keboola_semantic_layer_sync.py` (append)

**Interfaces:**
- Consumes: `try_join_composition` / `build_metric_row`'s new params (Task 4); `column_metadata_repo()` (existing factory).
- Produces: `sync_semantic_layer()`'s return dict gains three new keys: `skipped_ambiguous_relationship: int`, `skipped_unsupported_relationship_type: int`, `skipped_unverified_relationship_direction: int` (additive — every existing key is unchanged).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_keboola_semantic_layer_sync.py`:

```python
def _seed_column_metadata(table_id: str, column_names: list[str]):
    from src.db import get_system_db
    from src.repositories.column_metadata import ColumnMetadataRepository

    conn = get_system_db()
    try:
        repo = ColumnMetadataRepository(conn)
        for col in column_names:
            repo.save(table_id=table_id, column_name=col, basetype="VARCHAR")
    finally:
        conn.close()


def _relationship_item(name, from_id, to_id, on, rel_type="left", model_uuid="model-1"):
    return {
        "type": "semantic-relationship", "id": f"id-{name}",
        "attributes": {"name": name, "from": from_id, "to": to_id, "on": on, "type": rel_type, "modelUUID": model_uuid},
    }


class TestSyncSemanticLayerRelationships:
    def test_resolves_relationship_metric_end_to_end(self, e2e_env):
        from connectors.keboola.semantic_layer import sync_semantic_layer
        from src.repositories import metric_repo

        _register_keboola_table("in.c-a", "activities", "crm_activities")
        _register_keboola_table("in.c-a", "opportunities", "crm_opportunities")
        _seed_column_metadata("crm_activities", ["opportunity_id", "created_at"])
        _seed_column_metadata("crm_opportunities", ["id", "amount"])

        fake_storage = MagicMock()
        fake_storage.verify_token.return_value = {"isMasterToken": True}
        fake_metastore = MagicMock()
        fake_metastore.list_items.side_effect = lambda item_type, model_uuid=None: {
            "semantic-model": [_model_item()],
            "semantic-dataset": [],
            "semantic-metric": [_metric_item("linked_amount", 'SUM(o."amount")', "in.c-a.activities")],
            "semantic-constraint": [],
            "semantic-relationship": [
                _relationship_item("o_to_a", "in.c-a.opportunities", "in.c-a.activities", 'o."id" = a."opportunity_id"')
            ],
        }[item_type]

        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            result = sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")

        assert result["created_or_updated"] == 1
        assert result["skipped_foreign_alias"] == 0
        row = metric_repo().get("keboola/model-1/linked_amount")
        assert row is not None
        assert row["tables"] == ["crm_activities", "crm_opportunities"]
        assert 'LEFT JOIN "crm_opportunities" AS j' in row["sql"]

    def test_ambiguous_relationship_falls_back_to_specific_skip_counter(self, e2e_env):
        from connectors.keboola.semantic_layer import sync_semantic_layer

        _register_keboola_table("in.c-a", "activities", "crm_activities")

        fake_storage = MagicMock()
        fake_storage.verify_token.return_value = {"isMasterToken": True}
        fake_metastore = MagicMock()
        fake_metastore.list_items.side_effect = lambda item_type, model_uuid=None: {
            "semantic-model": [_model_item()],
            "semantic-dataset": [],
            "semantic-metric": [_metric_item("linked_amount", 'SUM(o."amount")', "in.c-a.activities")],
            "semantic-constraint": [],
            "semantic-relationship": [],  # no relationship touches this dataset
        }[item_type]

        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            result = sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")

        assert result["skipped_ambiguous_relationship"] == 1
        assert result["skipped_foreign_alias"] == 0

    def test_unverified_direction_falls_back_to_specific_skip_counter(self, e2e_env):
        from connectors.keboola.semantic_layer import sync_semantic_layer

        _register_keboola_table("in.c-a", "activities", "crm_activities")
        _register_keboola_table("in.c-a", "opportunities", "crm_opportunities")

        fake_storage = MagicMock()
        fake_storage.verify_token.return_value = {"isMasterToken": True}
        fake_metastore = MagicMock()
        fake_metastore.list_items.side_effect = lambda item_type, model_uuid=None: {
            "semantic-model": [_model_item()],
            "semantic-dataset": [],
            # metric's own dataset (opportunities) is on the relationship's
            # "from" side — the unverified direction.
            "semantic-metric": [_metric_item("linked_amount", 'SUM(a."amount")', "in.c-a.opportunities")],
            "semantic-constraint": [],
            "semantic-relationship": [
                _relationship_item("o_to_a", "in.c-a.opportunities", "in.c-a.activities", 'o."id" = a."opportunity_id"')
            ],
        }[item_type]

        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            result = sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")

        assert result["skipped_unverified_relationship_direction"] == 1

    def test_single_table_metrics_unaffected_by_relationship_step(self, e2e_env):
        """Regression: adding the relationship step must not change a
        single existing single-table-metric assertion."""
        from connectors.keboola.semantic_layer import sync_semantic_layer
        from src.repositories import metric_repo

        _register_keboola_table("in.c-example_source", "orders", "crm_orders")

        fake_storage = MagicMock()
        fake_storage.verify_token.return_value = {"isMasterToken": True}
        fake_metastore = MagicMock()
        fake_metastore.list_items.side_effect = lambda item_type, model_uuid=None: {
            "semantic-model": [_model_item()],
            "semantic-dataset": [],
            "semantic-metric": [_metric_item("total_revenue", 'SUM("amount")', "in.c-example_source.orders")],
            "semantic-constraint": [],
            "semantic-relationship": [],
        }[item_type]

        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            result = sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")

        assert result["created_or_updated"] == 1
        row = metric_repo().get("keboola/model-1/total_revenue")
        assert row["sql"] == 'SELECT SUM("amount") FROM "crm_orders" AS t'
        assert "tables" not in row or row["tables"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_keboola_semantic_layer_sync.py -v -k Relationship`
Expected: FAIL — every fake `list_items.side_effect` dict access for `"semantic-relationship"` currently raises `KeyError` because `sync_semantic_layer()` doesn't fetch that item type yet.

- [ ] **Step 3: Implement**

In `connectors/keboola/semantic_layer.py`, modify `sync_semantic_layer()`:

Extend the per-model fetch `try` block to also fetch relationships:

```python
    try:
        datasets = metastore.list_items("semantic-dataset", model_uuid)
        metrics = metastore.list_items("semantic-metric", model_uuid)
        constraints = metastore.list_items("semantic-constraint", model_uuid)
        relationships = metastore.list_items("semantic-relationship", model_uuid)
    except (MetastoreApiError, requests.RequestException) as e:
        logger.error("Keboola Metastore fetch failed (model %s): %s", model_uuid, e)
        return {"status": "error", "error": f"Metastore fetch failed: {e}"}
```

Extend the `from src.repositories import table_registry_repo, metric_repo` line:

```python
    from src.repositories import table_registry_repo, metric_repo, column_metadata_repo
```

Directly after `dataset_lookup = dataset_lookup_by_table_id(datasets)`, add:

```python
    relationship_lookup = relationship_lookup_by_dataset(relationships)
    column_metadata = column_metadata_repo()
    column_lookup = {
        name: {c["column_name"] for c in column_metadata.list_for_table(name)}
        for name in set(table_lookup.values())
    }
```

Extend the counter initialization (directly after `skipped_embedded_comment = 0`):

```python
    skipped_ambiguous_relationship = 0
    skipped_unsupported_relationship_type = 0
    skipped_unverified_relationship_direction = 0
```

Replace the metric-loop body's skip-reason branch:

```python
    for item in metrics:
        row, skip_reason = build_metric_row(
            item, table_lookup, dataset_lookup, constraints, model_uuid,
            relationship_lookup=relationship_lookup, column_lookup=column_lookup,
        )
        if row is None:
            if skip_reason == "unresolved_table":
                skipped_unresolved_table += 1
            elif skip_reason == "foreign_alias_reference":
                skipped_foreign_alias += 1
            elif skip_reason == "embedded_sql_comment":
                skipped_embedded_comment += 1
            elif skip_reason == "ambiguous_relationship":
                skipped_ambiguous_relationship += 1
            elif skip_reason == "unsupported_relationship_type":
                skipped_unsupported_relationship_type += 1
            elif skip_reason == "unverified_relationship_direction":
                skipped_unverified_relationship_direction += 1
            else:
                logger.warning(
                    "Keboola semantic metric skipped (%s): %r",
                    skip_reason,
                    (item.get("attributes") or {}).get("name"),
                )
            continue
        repo.create(**row)
        seen_ids.add(row["id"])
```

Extend both the `empty_result` dict and the final `return` statement with the three new keys, each initialized/reported as `0` / the counter value, exactly mirroring how `skipped_embedded_comment` was added in the shipped metric importer.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_keboola_semantic_layer_sync.py -v`
Expected: PASS (the entire file — confirms every pre-existing metric-import and glossary-adjacent test, if the glossary plan has already landed in this branch, still passes; if not, every pre-existing metric-import test still passes)

- [ ] **Step 5: Commit**

```bash
git add connectors/keboola/semantic_layer.py tests/test_keboola_semantic_layer_sync.py
git commit -m "Wire relationship-based JOIN resolution into sync_semantic_layer"
```

---

### Task 6: CHANGELOG and full suite verification

**Files:**
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: nothing (documentation-only task).
- Produces: nothing (end of plan).

- [ ] **Step 1: Add the CHANGELOG bullet**

Add under the `## [Unreleased]` heading in `CHANGELOG.md` (if the glossary plan already landed and opened an `### Added` subsection, append to it; otherwise create one):

```markdown
### Added

- Keboola relationship-based JOIN metrics: `semantic-metric` expressions previously skipped as `foreign_alias_reference` now compose a two-table `LEFT JOIN` when exactly one `semantic-relationship` connects the metric's dataset (on the live-verified `to` side) to a registered Agnes table, resolved by real column metadata rather than alias-name matching (verified live: alias names in `semantic-relationship.on` never matched the aliases metric authors used). Anything ambiguous, an unverified join direction, or an unsupported relationship type still skips and counts under a specific reason — never a guessed JOIN.
```

- [ ] **Step 2: Commit**

```bash
git add CHANGELOG.md
git commit -m "Add CHANGELOG entry for Keboola relationship-based JOIN metrics"
```

- [ ] **Step 3: Run the full test suite**

Run: `.venv/bin/pytest tests/ --tb=short -n auto -q`
Expected: All tests pass (no regressions in `tests/test_keboola_semantic_layer_mapping.py`, `tests/test_keboola_semantic_layer_sync.py`, or any other pre-existing test file).

- [ ] **Step 4: Verify no leftover sensitive data**

Run: `git log --oneline -8` and confirm every commit message and every file in this plan's diff contains no real Keboola project data (table/column names, relationship names, project IDs) — this plan's example values are all fabricated placeholders (`crm_activities`, `crm_opportunities`, `kbc_projects`, etc.), consistent with the redacted design spec.
