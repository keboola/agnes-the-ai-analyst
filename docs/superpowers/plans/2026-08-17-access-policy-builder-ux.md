# Access-policy no-SQL builder — Implementation Plan (Slice 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an admin author a table access policy by picking columns and masks (no SQL, no table knowledge) — the server compiles a structured spec into the same validated DuckDB SQL the resolver already runs, so the stored artifact stays SQL and the dangerous masking-leak becomes impossible by construction.

**Architecture:** Additive, flag-gated (`access_policies.enabled`). The stored policy remains the verbatim SQL string on `table_registry.access_policy_sql` — nothing about enforcement changes. Two new **read/compile** endpoints feed a builder UI folded into the existing `accessPolicyModal`: (1) a columns+samples endpoint so the modal shows real schema without the admin knowing it; (2) a `policy/compile` endpoint that turns a `{row_rules, column_masks}` spec into canonical `SELECT * EXCLUDE(...), <derived> FROM t WHERE <pred>` SQL — **always excluding a column before re-deriving it**, so `SELECT *, md5(email) AS email` (the two-column plaintext leak) can never be emitted. The client posts the spec, gets SQL back, drops it into the existing `#apSql` textarea, and reuses the existing preview + PUT-save path unchanged. Raw SQL stays as the "Advanced" tab.

**Tech Stack:** FastAPI + Pydantic (endpoints), sqlglot (already the resolver's parser/validator), DuckDB `DESCRIBE` + existing profiler for schema/samples, Jinja2 template + vanilla JS (the existing `admin_tables.html` inline JS), pytest + Starlette `TestClient`.

## Global Constraints

- **Flag-gated:** every new surface is inert unless `access_policies.enabled` (config `access_policies.enabled` / env `AGNES_ACCESS_POLICIES_ENABLED`). Default OFF — an existing instance is byte-identical until an admin turns it on. (`app/switches.py` flag `access_policies`.)
- **SQL stays the single source of truth.** The compile endpoint RETURNS sql; it never stores a structured spec. Storage remains `table_registry.access_policy_sql` via the existing `PUT /api/admin/registry/{id}`.
- **Reuse the existing validation gate.** Compiled SQL passes through `src.access_policy_validate.validate_policy_sql` + `probe_policy` — the builder must never bypass or fork it. `tests/test_access_policy_surface_ratchet.py` stays green.
- **Admin-only:** every new route is `Depends(require_admin)` (compile + columns are admin authoring surfaces, not caller reads — same posture as `admin.py::preview_table_policy`, which the ratchet lists EXEMPT for this reason).
- **DuckDB↔Postgres parity:** no new repo methods are needed (no schema change; `policy_mapping`/`access_policy_sql` columns and `set_*` methods already exist on both backends). If any repo method is added, add the `_pg.py` sibling + contract test in the same task.
- **No migration.** `table_registry.access_policy_sql`, `access_policy_note`, `access_policy_updated_*`, `policy_mapping` already exist (migration `0063_access_policy_columns_v116`, schema v116). Do not add one.
- **Vendor-agnostic, no AI attribution, CHANGELOG bullet under `[Unreleased]` in the same PR.**
- **Command/endpoint copy:** user-facing, plain-language ("Hide", "Pseudonymize"), never leaks internals.

---

## File structure

- `app/api/admin.py` — **modify**: add `GET /api/admin/registry/{table_id}/policy/columns` (schema+samples for the builder) and `POST /api/admin/registry/{table_id}/policy/compile` (spec→SQL). Both next to the existing `preview_table_policy` (~line 4640) so they share the eligibility/registry-lookup helpers.
- `src/access_policy_compile.py` — **create**: pure, dependency-light `compile_policy(spec, columns) -> CompiledPolicy` (the canonical SQL generator + the EXCLUDE-before-derive invariant). Kept out of `admin.py` so it is unit-testable without HTTP and reusable by the CLI later.
- `app/web/templates/admin_tables.html` — **modify**: the `accessPolicyModal` (~3590-3665) gains a **Builder** tab (default) and keeps today's textarea as an **Advanced SQL** tab; new inline JS renders the column list + mask menus, calls `columns` on open and `compile` on change, and writes the returned SQL into `#apSql`.
- `tests/test_access_policy_compile.py` — **create**: unit tests for `compile_policy` (the safety-critical core).
- `tests/test_admin_access_policy_builder_api.py` — **create**: endpoint tests (columns + compile, RBAC, flag gate).
- `tests/test_admin_tables_access_policy_ui.py` — **modify**: assert the builder DOM (tabs, column-list mount, compile wiring) renders when the flag is on.
- `CHANGELOG.md` — **modify**: one `[Unreleased]` bullet.

---

### Task 1: `compile_policy` — the structured-spec → SQL generator (the safety core)

**Files:**
- Create: `src/access_policy_compile.py`
- Test: `tests/test_access_policy_compile.py`

**Interfaces:**
- Produces:
  - `RowRule` = a dict `{"column": str, "op": "in_caller_groups" | "eq_caller_email" | "eq_caller_id" | "eq" | "in", "value": Any | None}`.
  - `ColumnMask` = one of `"show" | "hide" | "nullify" | "hash" | "unmask"`; for `"unmask"` a `{"choice": "unmask", "group": str}` object.
  - `PolicySpec` = `{"table": str, "row_rules": list[RowRule], "row_combine": "and" | "or", "column_masks": dict[str, ColumnMask | dict]}`.
  - `compile_policy(spec: PolicySpec, columns: list[str]) -> CompiledPolicy` where `CompiledPolicy` is a dataclass `{sql: str, excluded: list[str], derived: list[str], warnings: list[str]}`.
  - Consumed by Task 3 (`/policy/compile` endpoint).

- [ ] **Step 1: Write the failing test — masking never emits a duplicate column**

```python
# tests/test_access_policy_compile.py
from src.access_policy_compile import compile_policy

COLS = ["invoice_id", "cost_center", "email", "national_id", "amount_eur"]

def test_hash_mask_excludes_before_rederiving():
    spec = {
        "table": "invoices",
        "row_rules": [{"column": "cost_center", "op": "in_caller_groups"}],
        "row_combine": "and",
        "column_masks": {"national_id": "hide", "email": "hash"},
    }
    out = compile_policy(spec, COLS)
    # both re-derived and hidden columns land in EXCLUDE, so `*` never re-emits them
    assert "* EXCLUDE (national_id, email)" in out.sql
    assert "md5(email) AS email" in out.sql
    assert "list_contains($user_groups, cost_center)" in out.sql
    # the leak shape is structurally impossible: exactly one `email` in the output
    assert out.sql.count(" email") >= 1
    assert "national_id" in out.excluded and "email" in out.excluded
```

- [ ] **Step 2: Run it, verify it fails**

Run: `PYTHONPATH=. ./.venv/bin/pytest tests/test_access_policy_compile.py -x -q`
Expected: FAIL — `ModuleNotFoundError: src.access_policy_compile`.

- [ ] **Step 3: Implement `compile_policy`**

```python
# src/access_policy_compile.py
"""Compile a structured builder spec into the canonical DuckDB policy SQL.

The stored policy is always SQL (src/access_policy.py runs it verbatim); this
module is the no-SQL builder's generator. Its one hard invariant: a re-derived
column (mask that emits `<expr> AS col`) is ALWAYS added to `* EXCLUDE (...)`
first, so the two-column plaintext leak `SELECT *, md5(col) AS col` can never
be produced. Pure and HTTP-free so it unit-tests without a request and can be
reused by the CLI.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.sql_ident import quote_ident


@dataclass
class CompiledPolicy:
    sql: str
    excluded: list[str] = field(default_factory=list)
    derived: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


_ID_TOKENS = {
    "eq_caller_email": "$user_email",
    "eq_caller_id": "$user_id",
}


def _mask_choice(m):
    return m.get("choice") if isinstance(m, dict) else m


def _predicate(rule: dict) -> str:
    col = quote_ident(rule["column"])
    op = rule["op"]
    if op == "in_caller_groups":
        return f"list_contains($user_groups, {col})"
    if op in _ID_TOKENS:
        return f"{col} = {_ID_TOKENS[op]}"
    if op == "eq":
        return f"{col} = {_sql_literal(rule.get('value'))}"
    if op == "in":
        vals = rule.get("value") or []
        return f"{col} IN ({', '.join(_sql_literal(v) for v in vals)})"
    raise ValueError(f"unknown row op: {op!r}")


def _sql_literal(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def compile_policy(spec: dict, columns: list[str]) -> CompiledPolicy:
    known = set(columns)
    warnings: list[str] = []
    excluded: list[str] = []
    derived: list[str] = []

    for col, raw in (spec.get("column_masks") or {}).items():
        if col not in known:
            warnings.append(f"unknown column ignored: {col}")
            continue
        choice = _mask_choice(raw)
        if choice == "show":
            continue
        # EVERY non-show mask excludes the column from `*` first — this is the
        # anti-leak invariant. hide stops there; the rest re-derive.
        excluded.append(col)
        q = quote_ident(col)
        if choice == "hide":
            continue
        if choice == "nullify":
            derived.append(f"NULL AS {q}")
        elif choice == "hash":
            derived.append(f"md5({q}) AS {q}")
        elif choice == "unmask":
            grp = raw.get("group", "") if isinstance(raw, dict) else ""
            derived.append(
                f"CASE WHEN list_contains($user_groups, {_sql_literal(grp)}) "
                f"THEN {q} ELSE NULL END AS {q}"
            )
        else:
            raise ValueError(f"unknown mask: {choice!r}")

    proj = "*"
    if excluded:
        proj = "* EXCLUDE (" + ", ".join(quote_ident(c) for c in excluded) + ")"
    if derived:
        proj = proj + ", " + ", ".join(derived)

    rules = [r for r in (spec.get("row_rules") or []) if r.get("column") in known]
    where = ""
    if rules:
        joiner = " OR " if spec.get("row_combine") == "or" else " AND "
        where = " WHERE " + joiner.join(_predicate(r) for r in rules)

    table = quote_ident(spec["table"])
    sql = f"SELECT {proj} FROM {table}{where}"
    if not rules and not excluded and not derived:
        warnings.append("This policy returns the full table to every caller — nothing is filtered or masked.")
    return CompiledPolicy(sql=sql, excluded=excluded, derived=derived, warnings=warnings)
```

- [ ] **Step 4: Run it, verify it passes**

Run: `PYTHONPATH=. ./.venv/bin/pytest tests/test_access_policy_compile.py -x -q`
Expected: PASS.

- [ ] **Step 5: Add the round-trip-against-the-validator test (the SQL must be acceptable)**

```python
def test_compiled_sql_passes_the_real_validator():
    from src.access_policy_validate import validate_policy_sql
    spec = {"table": "invoices",
            "row_rules": [{"column": "cost_center", "op": "in_caller_groups"}],
            "row_combine": "and",
            "column_masks": {"email": "hash", "national_id": "hide"}}
    out = compile_policy(spec, COLS)
    # allowed-table set includes the base table; validator must not reject the builder's own output
    validate_policy_sql(out.sql, table_name="invoices", allowed_mapping_tables=set())
```

Run: `PYTHONPATH=. ./.venv/bin/pytest tests/test_access_policy_compile.py -x -q`
Expected: PASS. (If `validate_policy_sql`'s real signature differs, adjust the call to match `src/access_policy_validate.py` — read it first; do not change the validator.)

- [ ] **Step 6: Commit**

```bash
git add src/access_policy_compile.py tests/test_access_policy_compile.py
git commit -m "feat(access-policy): structured spec -> SQL compiler (EXCLUDE-before-derive)"
```

---

### Task 2: `GET /policy/columns` — real schema + sample values for the builder

**Files:**
- Modify: `app/api/admin.py` (add route next to `preview_table_policy`)
- Test: `tests/test_admin_access_policy_builder_api.py`

**Interfaces:**
- Produces: `GET /api/admin/registry/{table_id}/policy/columns` → `{"columns": [{"name", "type", "samples": [str], "distinct": int|None, "pii": bool}], "mapping_tables": [str], "eligible": bool}`.
- Consumes: existing `build_schema` (columns/types), `get_table_profile` (samples/distinct/top_values), the `policy_mapping` list already computed at `admin.py:4699`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_admin_access_policy_builder_api.py  (fixtures: reuse the existing
# admin-client + seeded-table fixtures from tests/test_admin_access_policy_api.py)
def test_columns_endpoint_returns_schema_and_samples(admin_client, seeded_policy_table):
    tid = seeded_policy_table  # a server_only/remote registered table
    r = admin_client.get(f"/api/admin/registry/{tid}/policy/columns")
    assert r.status_code == 200
    body = r.json()
    names = [c["name"] for c in body["columns"]]
    assert "email" in names
    assert all("type" in c and "samples" in c for c in body["columns"])
    assert "mapping_tables" in body and body["eligible"] is True

def test_columns_endpoint_is_admin_only(analyst_client, seeded_policy_table):
    r = analyst_client.get(f"/api/admin/registry/{seeded_policy_table}/policy/columns")
    assert r.status_code in (401, 403)
```

- [ ] **Step 2: Run, verify fail** — Run: `PYTHONPATH=. ./.venv/bin/pytest tests/test_admin_access_policy_builder_api.py -x -q` — Expected: FAIL (404 route missing).

- [ ] **Step 3: Implement the route** — add, next to `preview_table_policy` in `app/api/admin.py`, an admin-gated handler that: looks up the registry row (reuse the same lookup the preview uses); computes `eligible = query_mode=='remote' or server_only`; reads columns/types via `build_schema` (or a `DESCRIBE {quote_ident(name)}` on the analytics read-only connection, the exact query `preview_table_policy` already runs); reads samples/distinct via `get_table_profile`'s data (guarded so a not-yet-profiled table returns columns with empty `samples`); builds `mapping_tables` from the existing `policy_mapping` list; marks `pii` from the profiler's uniqueness alert or a name heuristic. Return the shape above. Read `preview_table_policy` (~4640) and `get_table_profile` (~catalog.py:91) first and mirror their column-name handling (`quote_ident`, never bare f-string).

- [ ] **Step 4: Run, verify pass.**

- [ ] **Step 5: Commit** — `git commit -m "feat(access-policy): builder columns+samples endpoint"`.

---

### Task 3: `POST /policy/compile` — spec → SQL over the endpoint

**Files:**
- Modify: `app/api/admin.py`
- Test: `tests/test_admin_access_policy_builder_api.py`

**Interfaces:**
- Consumes: `compile_policy` (Task 1), the columns lookup (Task 2's helper).
- Produces: `POST /api/admin/registry/{table_id}/policy/compile` body `{row_rules, row_combine, column_masks}` → `{"sql": str, "warnings": [str]}`. The `table` in the spec is taken from the registry row (never trusted from the client).

- [ ] **Step 1: Write the failing test**

```python
def test_compile_endpoint_builds_safe_sql(admin_client, seeded_policy_table):
    r = admin_client.post(f"/api/admin/registry/{seeded_policy_table}/policy/compile", json={
        "row_rules": [{"column": "cost_center", "op": "in_caller_groups"}],
        "row_combine": "and",
        "column_masks": {"email": "hash", "national_id": "hide"},
    })
    assert r.status_code == 200
    sql = r.json()["sql"]
    assert "EXCLUDE (national_id, email)" in sql
    assert "md5(email) AS email" in sql
    assert "list_contains($user_groups, cost_center)" in sql
```

- [ ] **Step 2-4:** run→fail; implement (resolve the registry row → `name`; fetch its column list via Task 2's helper; call `compile_policy({...spec, table: name}, columns)`; return `sql` + `warnings`; admin-gated); run→pass.

- [ ] **Step 5: Commit** — `git commit -m "feat(access-policy): policy/compile endpoint (structured spec -> validated SQL)"`.

---

### Task 4: Builder UI in the policy modal (column list + mask menus → compile → #apSql)

**Files:**
- Modify: `app/web/templates/admin_tables.html` (the `accessPolicyModal`, ~3590-3665, and its inline JS ~5666-5959)
- Modify: `tests/test_admin_tables_access_policy_ui.py`

**Interfaces:**
- Consumes: `GET .../policy/columns`, `POST .../policy/compile`.
- Produces: DOM the UI test asserts — a `#apBuilder` mount, a `.ap-tab` pair (Builder / Advanced SQL), and a `#apColList` container.

- [ ] **Step 1: Write the failing rendered-HTML test**

```python
# extend tests/test_admin_tables_access_policy_ui.py
def test_builder_scaffold_renders_when_flag_on(seeded_app):
    c, token = seeded_app["client"], seeded_app["admin_token"]
    html = c.get("/admin/tables", headers=_auth(token)).text
    assert 'id="apBuilder"' in html
    assert 'id="apColList"' in html
    assert 'data-ap-tab="builder"' in html and 'data-ap-tab="sql"' in html
```

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement the builder markup + JS.** In `accessPolicyModal`: wrap the existing textarea in a `data-ap-tab="sql"` panel; add a `data-ap-tab="builder"` panel (default active) containing `#apColList` (column rows) and the row-rule block. In JS: on `openAccessPolicyModal(table)`, fetch `/policy/columns`, render each column (`name · type · sample chips · distinct`, PII hint), each with a mask `<select>` (Show/Hide/Nullify/Pseudonymize/Unmask). On any change, build the spec from DOM state, `POST /policy/compile` (debounced ~250ms), and write `resp.sql` into `#apSql` (so the existing `apRunPreview` + `apSavePolicy` keep working unchanged). Tabs toggle panel visibility. Keep everything gated: the modal already only opens for policy-eligible flows. Follow the design-system contract (`--ds-*` tokens only; no raw hex; no `var(--primary)`), matching the existing modal's classes so `tests/test_design_system_contract.py` and `tests/test_admin_tables_tab_ui.py` stay green (reuse `.data-table`, never a new private table class).

- [ ] **Step 4: Run the UI test + the design/layout guards.**

Run: `PYTHONPATH=. AGNES_ACCESS_POLICIES_ENABLED=1 ./.venv/bin/pytest tests/test_admin_tables_access_policy_ui.py tests/test_admin_tables_tab_ui.py tests/test_design_system_contract.py -q`
Expected: PASS.

- [ ] **Step 5: Commit** — `git commit -m "feat(access-policy): no-SQL builder UI in the policy editor"`.

---

### Task 5: Inline eligibility + mapping toggle (kills the two-popup dead-end)

**Files:**
- Modify: `app/web/templates/admin_tables.html`
- Modify: `tests/test_admin_tables_access_policy_ui.py`

**Interfaces:**
- Consumes: existing `PUT /api/admin/registry/{id}` (already accepts `server_only` and `policy_mapping`).

- [ ] **Step 1: Write the failing test** — assert the modal renders a `#apMakeServerOnly` action for an ineligible table and a `#apMappingToggle` control.

```python
def test_inline_eligibility_and_mapping_controls_render(seeded_app):
    c, token = seeded_app["client"], seeded_app["admin_token"]
    html = c.get("/admin/tables", headers=_auth(token)).text
    assert 'id="apMakeServerOnly"' in html
    assert 'id="apMappingToggle"' in html
```

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement.** In the interlock warning path (`_apRenderInterlockWarning`), replace the "set server_only first" sentence with a `#apMakeServerOnly` button that `PUT`s `{server_only: true}` then re-opens the builder enabled. Add a `#apMappingToggle` (a switch) that `PUT`s `{policy_mapping: bool}` so a table can be marked referenceable from the UI (today CLI-only), with copy that says "referenceable from any policy — not a grant."

- [ ] **Step 4: Run the UI tests.** — Expected: PASS.

- [ ] **Step 5: Commit** — `git commit -m "feat(access-policy): inline make-server-only + mapping toggle in the editor"`.

---

### Task 6: CHANGELOG + release-cut decision

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add one `[Unreleased]` → `### Added` bullet** naming: the no-SQL builder (column list + real samples, per-column mask menu, structured `policy/compile` that always excludes-before-derives), inline make-server-only + mapping toggle; still flag-gated; SQL remains the stored artifact; raw-SQL "Advanced" tab retained.

- [ ] **Step 2: Commit** — `git commit -m "docs: changelog for the access-policy no-SQL builder"`.

- [ ] **Step 3:** Leave the version bump for the release-cut at merge time (patch vs minor per the flag-gated-no-default-change rule). Do NOT cut it now.

---

## Self-review notes

- **Spec coverage:** feedback #1 (too complex) → Tasks 1–4 (no SQL, real samples). #3 (no table knowledge) → Task 2 columns + Task 4 pickers. #2 (two-popup confusion) → Task 5 inline eligibility (full IA merge is a later slice). #4 (progressive/interactive) → Task 4 live column list; before/after persona preview + matrix are the explicit **Slice 2** follow-up (not in this plan).
- **Not in Slice 1 (Slice 2):** row-filter builder for arbitrary rules, mapping-join guided picker with joined preview, before/after persona preview (needs a `base_sample_rows` field on `preview_table_policy`), the multi-persona matrix (needs a "distinct group-sets that can reach this table" helper), and the fuller drawer/page IA merge.
- **Carry-forward risk (flag for Slice 2):** `preview_table_policy` already runs two uncapped `COUNT(*)` scans per call on remote/BQ tables outside the `remote_scan_too_large` guardrail — the interactive preview loop in Slice 2 must move to a bounded local sample. Not triggered by Slice 1 (compile is pure; columns uses the precomputed profile).
- **Type consistency:** `compile_policy(spec, columns)` signature is identical in Task 1 (def), Task 3 (call). `PolicySpec` field names (`row_rules`, `row_combine`, `column_masks`) match across Tasks 1/3/4.
