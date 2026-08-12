# Table Access Policies Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Spec:** [`docs/superpowers/specs/2026-08-11-table-access-policies-design.md`](../specs/2026-08-11-table-access-policies-design.md). Section references below (§N) point into it — read the referenced section before starting a task.
>
> **Repo playbooks the builder is expected to follow rather than have re-taught here:** `.claude/skills/agnes-conventions/references/{migration,repo-parity,endpoint-rbac,web-page,command-ux,security}.md`. Where a task says "per the migration playbook", the concrete paths and the exact change are still given — the playbook covers the *mechanics* (which test guards fire, the ladder shape), not the *what*.

**Goal:** Let an admin attach one SQL policy per registered non-distributed table that Agnes substitutes for that table on every server-side read, filtering rows and masking columns by the caller's identity.

**Architecture:** One resolver (`src/access_policy.py`) turns `(table_id, principal)` into a readable relation — policy-wrapped or raw. SQL read surfaces substitute it into the parsed query tree; `table_id`-shaped surfaces build their `FROM` from it. Enforcement is inert until a policy is attached, and attachment is gated behind `access_policies.enabled` (off by default), so the whole change lands dark in one PR and is activated per-instance.

**Tech Stack:** FastAPI, DuckDB 1.5.2, sqlglot 30.6.0 (already a dependency), Postgres (Alembic), Jinja2 admin templates, Typer CLI.

## Global Constraints

- **Dual-backend parity is non-negotiable.** Every `src/repositories/table_registry.py` change gets the matching `table_registry_pg.py` change in the same task, with the contract test extended. Reach repos through the factory (`src/repositories/__init__.py` → `table_registry_repo()`), never instantiate directly.
- **Migration ladders move together.** A `_v115_to_v116` step in `src/db.py` (three edit sites: `_SYSTEM_SCHEMA` DDL, fresh-install chain, upgrade chain) + `SCHEMA_VERSION = 116` at `src/db.py:59`, plus a matching Alembic revision. Endpoint pinned by `tests/test_db_schema_version.py`. Bump the two literal version strings in `docs/runbooks/wal-recovery.md` (guarded by `tests/test_runbook_wal_recovery.py`).
- **Every failure denies.** No code path may degrade to returning unfiltered rows on error. This is the single invariant the whole feature exists to hold.
- **Vendor-agnostic.** No customer names, project IDs, or internal hosts in code, comments, or the CHANGELOG.
- **CHANGELOG discipline.** The final task adds one `## [Unreleased]` bullet covering the whole feature; individual tasks do not each add bullets.
- **No AI attribution** in commits or the PR.
- **Feature flag:** `feature_enabled("access_policies", "enabled", env_var="AGNES_ACCESS_POLICIES_ENABLED", default=False)`. Register it in the `FEATURE_FLAGS` registry in `app/instance_config.py`.
- **Identifier escaping:** never build a quoted identifier with an f-string. Use `quote_ident` from `src/sql_ident.py`.

---

## Phase 0 — Prerequisites (not part of this plan)

PRs [#1264](https://github.com/keboola/agnes-the-ai-analyst/pull/1264) (block SQL-as-string table functions, merged `27fa5a36b`) and [#1265](https://github.com/keboola/agnes-the-ai-analyst/pull/1265) (server-side distribution gate, merged `93da08ef3`) are **already in `main`** as of this plan. Cut the implementation branch from a `main` that includes them:

```bash
git fetch origin main && git log --oneline origin/main | grep -E "block SQL-as-string|server_only and query_mode"
# both lines must appear before starting Task 1
```

---

## Phase 1 — Storage & validation (the trunk)

Everything depends on this phase. Build it first and serially; the rest of the plan fans out from Task 5.

### Task 1: Registry columns + migration ladder

**Files:**
- Modify: `src/db.py` (three sites: `_SYSTEM_SCHEMA` DDL near the `table_registry` CREATE ~`:432`; fresh-install chain after `:8256`; upgrade chain after `:8539`; `SCHEMA_VERSION` at `:59`)
- Create: `migrations/versions/00XX_access_policy_columns_v116.py` (Alembic; next revision number after the current head `0061_agent_status_backfill_v115.py`)
- Modify: `src/models/ops.py` (SQLAlchemy `table_registry` model — add the four columns)
- Modify: `docs/runbooks/wal-recovery.md` (two literal `v115` → `v116` strings)
- Test: `tests/test_db_schema_version.py` (already asserts both ladders reach the same version — it will fail until both sides move)

**Interfaces:**
- Produces: four new `table_registry` columns — `access_policy_sql VARCHAR NULL`, `access_policy_note VARCHAR NULL`, `access_policy_updated_at TIMESTAMP NULL`, `access_policy_updated_by VARCHAR NULL`, `policy_mapping BOOLEAN DEFAULT FALSE`. (§4)

- [ ] **Step 1: Write the failing test.** Extend `tests/test_db_schema_version.py` expectation to `116` (find the `EXPECTED_VERSION`/`SCHEMA_VERSION` assertion).

- [ ] **Step 2: Run it, verify it fails.**
  Run: `.venv/bin/pytest tests/test_db_schema_version.py -q`
  Expected: FAIL — DuckDB ladder still at 115.

- [ ] **Step 3: Add the DuckDB side (all three sites) + bump `SCHEMA_VERSION` to 116.** Follow the pattern of an existing recent step (e.g. the `server_only` column add). The `_v115_to_v116` upgrade step runs `ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS ...` for each column.

- [ ] **Step 4: Add the Alembic revision** (`down_revision` = current head) with the same five `add_column` operations, and add them to `src/models/ops.py`.

- [ ] **Step 5: Bump the two `wal-recovery.md` version strings.**

- [ ] **Step 6: Run the ladder + parity gates.**
  Run: `.venv/bin/pytest tests/test_db_schema_version.py tests/db_pg/test_schema_parity.py -q`
  Expected: PASS.

- [ ] **Step 7: Commit.**
  ```bash
  git add src/db.py migrations/versions/ src/models/ops.py docs/runbooks/wal-recovery.md tests/test_db_schema_version.py
  git commit -m "feat(db): table_registry access-policy columns (schema v116)"
  ```

### Task 2: Repository setters/readers (DuckDB + PG parity)

**Files:**
- Modify: `src/repositories/table_registry.py`
- Modify: `src/repositories/table_registry_pg.py`
- Modify: `src/repositories/table_registry.py` `register()` **and** the `PUT /registry` strip-tuple at `app/api/admin.py:3635-3644` — the five new keys must be accepted by `register()` OR stripped there, or every registry PUT raises `TypeError` (§18).
- Test: `tests/db_pg/test_access_policy_column_contract.py` (new; clone `tests/db_pg/test_server_only_column_contract.py`)

**Interfaces:**
- Produces (on both repos):
  - `set_access_policy(table_id: str, sql: str | None, note: str | None, updated_by: str) -> None`
  - `set_policy_mapping(table_id: str, value: bool) -> None`
  - `get(table_id)` / `list_all()` rows now carry the five columns.

- [ ] **Step 1: Write the failing contract test.** Parametrized over both backends: register a table, `set_access_policy(...)`, assert `get()` returns the sql/note/updated_by; `set_access_policy(..., sql=None)` clears; `set_policy_mapping(id, True)` reflects in `get()`.

- [ ] **Step 2: Run it, verify it fails** (methods undefined).
  Run: `.venv/bin/pytest tests/db_pg/test_access_policy_column_contract.py -q`

- [ ] **Step 3: Implement both methods on `table_registry.py`, then the identical signatures on `table_registry_pg.py`.** Per the repo-parity playbook.

- [ ] **Step 4: Fix the `register(**merged)` path** — add the five keys to the `_docs_key` strip-tuple at `app/api/admin.py:3635` so the read-modify-write PUT loop doesn't `TypeError`.

- [ ] **Step 5: Run contract + the static parity guard.**
  Run: `.venv/bin/pytest tests/db_pg/test_access_policy_column_contract.py tests/db_pg/test_repo_method_parity.py tests/test_backend_split_guard.py -q`
  Expected: PASS.

- [ ] **Step 6: Commit.**
  ```bash
  git commit -am "feat(repo): access-policy setters with DuckDB/PG parity"
  ```

### Task 3: Save-time validator

**Files:**
- Create: `src/access_policy_validate.py`
- Test: `tests/test_access_policy_validate.py`

**Interfaces:**
- Produces:
  - `class PolicyValidationError(ValueError)` — carries `.reason: str` and `.detail: str`.
  - `validate_policy_sql(sql: str, *, table_id: str, table_name: str, mapping_table_names: set[str], for_remote: bool) -> None` — raises `PolicyValidationError` on any rule violation; returns `None` if valid. Rules are §14.1–§14.5 (the `LIMIT 0` execution probe of §14.6 is a separate task — Task 12 — because it needs a live connection).

**Rules to enforce (each is a test case), from §14 + §5.2 + §6.1:**
1. Parses as exactly one statement, and it is a `SELECT` (`sqlglot.parse`, `read="duckdb"`).
2. No DDL/DML nodes (`exp.Insert/Update/Delete/Create/Drop/Copy/Merge/Alter/Command`), no `ATTACH/DETACH/INSTALL/LOAD/PRAGMA/CALL`.
3. Node-type allowlist (permitted `exp` types) and function-name allowlist — reject anything outside. Reuse the SQL-string-table-function rejection from `app/api/query.py` (`_has_sql_string_table_function`).
4. Table references ⊆ `{table_name} ∪ mapping_table_names`. Any other `exp.Table` → reject naming it.
5. Every `$name` is one of `user_email`, `user_id`, `user_groups`; none appears in identifier position; none appears as the RHS of `LIKE`/`ILIKE`/`SIMILAR TO` or inside a regex function (`regexp_matches`, `regexp_full_match`, …). (§6.1)
6. If `for_remote`: `sqlglot.transpile(sql, read="duckdb", write="bigquery")` succeeds, and a `$user_groups` membership uses `list_contains` (warn, not reject, on the `unnest`-in-`IN` form — §6.3).

- [ ] **Step 1: Write the failing tests** — one per rule above, each with a concrete SQL string that must reject, plus the happy-path policy from §1 that must pass. Example:

```python
def test_rejects_reference_to_unlisted_table():
    with pytest.raises(PolicyValidationError) as e:
        validate_policy_sql(
            "SELECT * FROM invoices JOIN secret ON secret.id = invoices.id",
            table_id="invoices", table_name="invoices",
            mapping_table_names=set(), for_remote=False,
        )
    assert e.value.reason == "policy_unlisted_table_reference"
    assert "secret" in e.value.detail

def test_rejects_identity_var_in_like_position():
    with pytest.raises(PolicyValidationError) as e:
        validate_policy_sql(
            "SELECT * FROM invoices WHERE owner LIKE $user_email",
            table_id="invoices", table_name="invoices",
            mapping_table_names=set(), for_remote=False,
        )
    assert e.value.reason == "policy_var_in_pattern_position"

def test_accepts_row_and_column_policy():
    validate_policy_sql(
        "SELECT * EXCLUDE (national_id), md5(email) AS email FROM invoices "
        "WHERE list_contains($user_groups, cost_center)",
        table_id="invoices", table_name="invoices",
        mapping_table_names=set(), for_remote=True,
    )
```

- [ ] **Step 2: Run, verify all fail** (`validate_policy_sql` undefined).
  Run: `.venv/bin/pytest tests/test_access_policy_validate.py -q`

- [ ] **Step 3: Implement `validate_policy_sql`** walking the parsed tree per the six rules. Keep it allowlist-shaped (§14 rule 2 rationale).

- [ ] **Step 4: Run, verify all pass.**

- [ ] **Step 5: Commit.**
  ```bash
  git commit -am "feat(access-policy): save-time SQL policy validator"
  ```

### Task 4: The distribution interlock

**Files:**
- Modify: `app/api/admin.py` — the `PUT /registry/{table_id}` validator block where the `server_only ↔ query_mode` invariant already lives (~`:3648`)
- Test: `tests/test_journey_access_policy_interlock.py`

**Interfaces:**
- Consumes: `table_registry_repo().get()`, `set_access_policy` (Task 2), `validate_policy_sql` (Task 3).
- Produces: registry-write rejections (HTTP 422) for the three interlock cases.

**Cases (§3.1–3.2), each a test:**
1. Attaching a policy to a table that is neither `remote` nor `server_only` → 422, detail names the fix ("set server_only=true first").
2. Clearing `server_only` / moving `query_mode='local'` on a policied table → 422.
3. Registering a row whose `source_query` / `(bucket, source_table)` / `bq_fqn` resolves to a policied table's physical source, when the new row is distributable → 422 (§3.2).

- [ ] **Step 1: Write the three failing journey tests** (HTTP-level, admin token, mirroring `tests/test_journey_server_only.py`).

- [ ] **Step 2: Run, verify they fail** (writes currently succeed).

- [ ] **Step 3: Implement the interlock** in the existing validator block; gate the whole policy-write path behind `feature_enabled("access_policies","enabled",...)` (a policy write when the flag is off → 422 `access_policies_disabled`).

- [ ] **Step 4: Run the interlock suite + the existing server_only journey (no regression).**
  Run: `.venv/bin/pytest tests/test_journey_access_policy_interlock.py tests/test_journey_server_only.py -q`

- [ ] **Step 5: Commit.**
  ```bash
  git commit -am "feat(access-policy): distribution interlock on registry writes"
  ```

---

## Phase 2 — The resolver (the single junction)

### Task 5: `policied_relation()` — the contract everything downstream consumes

**Files:**
- Create: `src/access_policy.py`
- Test: `tests/test_access_policy_resolver.py`

**Interfaces:**
- Consumes: `table_registry_repo()`, the live group read `user_group_members_repo().list_group_names_for_user(user_id)` (§6.2), the credential-surface helper used by `src/rbac.py` for admin bypass (§12).
- Produces (this is the block every later task binds against):

```python
# src/access_policy.py
from dataclasses import dataclass

@dataclass(frozen=True)
class PoliciedRelation:
    relation_sql: str      # a parenthesizable SELECT yielding the rows to read.
                           # Policy-wrapped when .policied, else "SELECT * FROM <base>".
    params: dict           # bind values: {"user_email","user_id","user_groups"} subset actually referenced
    policied: bool         # True iff a policy was applied (feeds §10 disclosure, §11 schema)
    table_id: str

class PolicyIdentityUnresolvable(Exception): ...   # co-drive session (§12)
class PolicyMappingEmpty(Exception):                # §15.1
    def __init__(self, mapping_table: str, last_sync): ...
class PolicyError(Exception):                       # policy failed to execute (§16)
    def __init__(self, table_id: str): ...

def policied_relation(table_id: str, principal, *, dialect: str = "duckdb") -> PoliciedRelation:
    """Resolve (table, caller) to a readable relation.

    - No policy on the table  → PoliciedRelation(relation_sql="SELECT * FROM <base>", policied=False)
    - Admin (credential surface 'all', §12) → same, policied=False
    - Policy present, resolvable identity → policied=True, relation_sql = the policy body
      with $vars kept as bind markers, params filled from identity
    - Co-drive / no single identity → raise PolicyIdentityUnresolvable
    dialect='bigquery' returns the transpiled body (Task 10 fills this arm).
    """
```

- [ ] **Step 1: Write failing tests** covering: (a) no policy → passthrough `policied=False`; (b) admin principal → passthrough even with a policy set; (c) a solo user with a policy → `policied=True`, `params["user_groups"]` is that user's live group list, `relation_sql` contains the policy body; (d) a `SessionPrincipal` with a policy → `PolicyIdentityUnresolvable`. Use a seeded registry row with `access_policy_sql` set via Task 2's setter.

- [ ] **Step 2: Run, verify fail.**
  Run: `.venv/bin/pytest tests/test_access_policy_resolver.py -q`

- [ ] **Step 3: Implement the DuckDB arm** (`dialect="duckdb"`). Identity extraction: `user_email`/`user_id` from the principal dict; `user_groups` from the live repo read. Reject pattern-metachar group names before binding (§6.1). Admin bypass via the credential-surface check. Leave `dialect="bigquery"` raising `NotImplementedError` for now (Task 10).

- [ ] **Step 4: Run, verify pass.**

- [ ] **Step 5: Commit.**
  ```bash
  git commit -am "feat(access-policy): policied_relation resolver (DuckDB arm)"
  ```

### Task 6: AST substitution helper for SQL surfaces

**Files:**
- Modify: `src/access_policy.py` (add the rewrite function)
- Test: `tests/test_access_policy_rewrite.py`

**Interfaces:**
- Consumes: `policied_relation` (Task 5).
- Produces:
  - `class PolicyNameCollision(Exception)` — `.table_id`.
  - `rewrite_sql(sql: str, principal, *, resolve=policied_relation) -> tuple[str, dict, list[str]]` — returns `(rewritten_sql, merged_params, policied_table_ids)`. Substitutes every policied-table node with `(<relation_sql>) AS <original_alias_or_name>`. Non-policied tables untouched. Raises `PolicyNameCollision` if a caller CTE/subquery alias shadows a policied table name (§5.2 r4). Raises `HTTPException(400)` (or a typed error the caller maps) if the SQL references a policied table but does not parse (§5.2 r3).

- [ ] **Step 1: Write failing fixtures** (§19 rewrite list): unqualified, 2- and 3-part qualified, aliased, CTE-nested, subquery, `LATERAL`, `UNION ALL BY NAME`, DuckDB FROM-first, comment-interleaved — each asserts the policied table is wrapped and its alias preserved and a non-policied sibling is untouched. Plus: collision on a `CTE` alias and on a `Subquery` alias both raise `PolicyNameCollision`; unparseable-but-references-policied raises; unparseable-no-policied is returned unchanged.

```python
def test_qualified_aliased_table_is_wrapped():
    out, params, ids = rewrite_sql(
        "SELECT * FROM analytics.main.invoices i JOIN dim d ON d.k=i.k",
        solo_user, resolve=fake_resolver_policying("invoices"),
    )
    assert "AS i" in out and "national_id" not in out  # policy dropped it
    assert ids == ["invoices"]

def test_cte_name_collision_rejected():
    with pytest.raises(PolicyNameCollision):
        rewrite_sql("WITH invoices AS (SELECT 1) SELECT * FROM invoices",
                    solo_user, resolve=fake_resolver_policying("invoices"))
```

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement `rewrite_sql`** via `sqlglot.parse_one(read="duckdb")` + `.transform()` replacing matched `exp.Table` nodes with `exp.Subquery`. Match on registry id resolved from name (§5.3). Collision check scans `exp.CTE` and derived-table/`Subquery` aliases.

- [ ] **Step 4: Run, verify pass.**

- [ ] **Step 5: Commit.**
  ```bash
  git commit -am "feat(access-policy): AST substitution for SQL read surfaces"
  ```

---

## Phase 3 — Enforcement fan-out (parallelizable after Task 6)

Tasks 7–12 are mutually independent and can be built in parallel worktrees, then integrated. Each wires one surface (or concern) to the resolver.

### Task 7: Wire `/api/query` + `run_remote_select_to_arrow`

**Files:**
- Modify: `app/api/query.py` (`execute_query` ~`:942`; `run_remote_select_to_arrow` ~`:2241`)
- Test: `tests/test_access_policy_query_endpoint.py`

**Interfaces:**
- Consumes: `rewrite_sql` (Task 6). Call it at the point where `get_accessible_tables(user, conn)` is already resolved (`:998`). Bind `params` into the DuckDB `execute`. Collect `policied_table_ids` for the disclosure envelope (Task 11).

- [ ] **Step 1: Failing E2E test** — two seeded users, a policy on `invoices` filtering by `$user_groups`; user A sees their rows, user B sees theirs, admin sees all; a masked column is absent from A's result.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Insert the `rewrite_sql` call + bind params; thread `policied_table_ids` onto the response object** (the `row_scope` field lands in Task 11 — here just carry the ids).
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit.** `git commit -am "feat(access-policy): enforce on /api/query"`

### Task 8: Wire the `table_id` surfaces via a shared FROM builder

**Files:**
- Modify: `app/api/v2_sample.py` (`:255-262` local, `:155-161` BQ), `app/api/v2_scan.py` (`:203-205`, `:506-521`), `app/api/mcp_per_table.py` (`_build_select` `:170`)
- Test: `tests/test_access_policy_table_id_surfaces.py`

**Interfaces:**
- Consumes: `policied_relation` (Task 5). These surfaces have no caller SQL — they build `FROM <source>`. Replace the `read_parquet(...)` / BQ-path expression with `policied_relation(table_id, principal).relation_sql` as the FROM, binding `params`.

- [ ] **Step 1: Failing tests** — `/api/v2/sample` and `POST /api/mcp/query-table` on a policied table return only the caller's slice and omit masked columns; a non-policied table is byte-identical to before.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Route each surface's FROM through the resolver.** `mcp_per_table`'s 400 handler must list *effective* columns (Task 9's schema), not raw (§8).
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit.** `git commit -am "feat(access-policy): enforce on sample/scan/query-table surfaces"`

### Task 9: Effective schema

**Files:**
- Modify: `app/api/v2_schema.py` (`build_schema_uncached` `:123`, the `DESCRIBE` at `:205-210`)
- Modify: `src/access_policy.py` (add `effective_schema(table_id, principal) -> list[ColumnSpec]` using a `LIMIT 0` run of the resolver's relation)
- Test: `tests/test_access_policy_effective_schema.py`

**Interfaces:**
- Produces: `effective_schema(...)` returning columns with `hidden`/`masked` markers; `/api/v2/schema` returns it for non-admin callers.

- [ ] **Step 1: Failing test** — schema of a policied table with `EXCLUDE (national_id)` omits `national_id` for a non-admin, includes it for admin; `md5(email) AS email` reports the effective type.
- [ ] **Step 2–4: TDD** the `LIMIT 0` derivation + wire `/api/v2/schema`.
- [ ] **Step 5: Commit.** `git commit -am "feat(access-policy): effective schema hides excluded columns"`

### Task 10: BigQuery arm — transpile, named params, ordering, fail-closed

**Files:**
- Modify: `src/access_policy.py` (`dialect="bigquery"` arm of `policied_relation`)
- Modify: `connectors/bigquery/access.py` (`run_bq_query_to_arrow` `:237` — add `query_parameters` passthrough to `QueryJobConfig`)
- Modify: `app/api/query.py` (§7.3 ordering: policy substitution resolves against the physical BQ path and runs before the bare-name→backtick pass; §7.4: remove the `except → analytics.execute(request.sql)` fallbacks at `:1106` and `:2350` **for policied queries** — rename the post-rewrite SQL to a single local so the original is unreachable)
- Test: `tests/test_access_policy_bigquery.py`

**Interfaces:**
- Consumes: `policied_relation(..., dialect="bigquery")`, `run_bq_query_to_arrow(..., query_parameters=...)`.

- [ ] **Step 1: Failing tests** — (a) `policied_relation(dialect="bigquery")` transpiles `EXCLUDE`→`EXCEPT`, `md5`→`TO_HEX(MD5())`, `$user_groups`→`@user_groups`; (b) `run_bq_query_to_arrow` passes named params to the job config; (c) a policy that BigQuery would reject raises `PolicyError`, and the response contains **no** rows (the removed fallback no longer fires). Mock the BQ client.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** the transpile arm, the param passthrough, the ordering, and the fallback removal. Guard the fallback removal on "query touches a policied table" so non-policied BQ queries keep their existing retry.
- [ ] **Step 4: Run, verify pass** — include a fail-closed assertion.
- [ ] **Step 5: Commit.** `git commit -am "feat(access-policy): BigQuery transpile, named params, fail-closed"`

### Task 11: Disclosure — `row_scope` across API / CLI / MCP / pull

**Files:**
- Modify: `app/api/query.py` (`QueryResponse` model `:507` — add `row_scope`), `app/api/v2_sample.py` (JSON), `app/api/v2_scan.py` (`X-Agnes-Row-Scope` header — no JSON body)
- Modify: `cli/commands/query.py` (`:403-416` reader — print the `[scope]` line to **stderr**, preserving `--format json` stdout `:407-409`)
- Modify: `app/api/mcp/foundation_tools.py` + `cli/mcp/server.py` (docstrings: document `row_scope` and instruct the agent to qualify aggregates — §10 item 3)
- Modify: `cli/lib/pull.py` (`:1814+` — write a `.claude/rules/` entry naming policied tables in the stack, §10 item 4)
- Test: `tests/test_access_policy_disclosure.py`

**Interfaces:**
- Consumes: `policied_table_ids` carried from Tasks 7/8. Produces `row_scope: {policied_tables: [id], note: str} | null`.

- [ ] **Step 1: Failing tests** — `/api/query` on a policied table returns non-null `row_scope`; `/api/v2/scan` sets the header; the CLI prints `[scope]` to stderr and keeps JSON clean on stdout; `agnes pull` writes the rules file.
- [ ] **Step 2–4: TDD** each surface.
- [ ] **Step 5: Commit.** `git commit -am "feat(access-policy): row_scope disclosure across surfaces"`

### Task 12: Response-cache keying + save-time execution probe

**Files:**
- Modify: `app/api/v2_sample.py` (`_sample_cache` key `:25`), `app/api/v2_schema.py` (`_schema_cache` key `:24`)
- Modify: `src/access_policy_validate.py` (add the §14.6 `LIMIT 0` live probe as `probe_policy(sql, table_id, conn) -> list[ColumnSpec]`)
- Modify: `app/api/admin.py` (call `probe_policy` in the policy-write path before persisting)
- Test: `tests/test_access_policy_cache_keying.py`

**Interfaces:**
- Produces: cache keys for policied tables carry `(user_id, sorted(groups))`; a guard asserts a policied table's key contains a caller-identity component (§9).

- [ ] **Step 1: Failing tests** — user A caches a sample of a policied table, user B does not receive A's rows; save-time probe rejects a policy referencing a dropped column.
- [ ] **Step 2–4: TDD.**
- [ ] **Step 5: Commit.** `git commit -am "feat(access-policy): identity-keyed caches + save-time probe"`

---

## Phase 4 — Surface ratchet (the one gate)

### Task 13: The enforcement ratchet

**Files:**
- Create: `tests/test_access_policy_surface_ratchet.py`
- Modify: whichever surface the ratchet reveals as unwired.

**Interfaces:**
- Consumes: nothing new. This is the acceptance gate (§8, §23.2).

- [ ] **Step 1: Write the ratchet** — enumerate routes that call `can_access_table` / `get_accessible_tables` or open the analytics DB (AST scan of `app/api/`, same shape as `tests/test_backend_split_guard.py`), and assert that set equals a hardcoded `COVERED` set (the surfaces wired in Tasks 7–9) plus an explicit `EXEMPT` set (admin-only `query_hybrid`, the REST-proxy `foundation_tools`). Any un-classified route fails the test.

```python
def test_every_data_read_surface_is_policy_covered_or_exempt():
    routes = _routes_touching_table_data("app/api")   # AST scan
    unclassified = routes - COVERED - EXEMPT
    assert not unclassified, f"unwired policy surfaces: {sorted(unclassified)}"
```

- [ ] **Step 2: Run it.** Expected: it lists any surface Tasks 7–9 missed (broker replay, cowork shim, catalog profile, stdio MCP per §8).
- [ ] **Step 3: Wire each surface the ratchet names, or add it to `EXEMPT` with a one-line justification comment.** Re-run until green.
- [ ] **Step 4: Commit.** `git commit -am "test(access-policy): surface ratchet — every read path covered"`

---

## Phase 5 — Admin surface, CLI, docs, release

### Task 14: Admin API — attach / clear / preview

**Files:**
- Modify: `app/api/admin.py` (extend the `PUT /registry/{table_id}` body with `access_policy_sql` / `access_policy_note` / `policy_mapping`; add `POST /api/admin/registry/{table_id}/policy/preview`)
- Test: `tests/test_admin_access_policy_api.py`

**Interfaces:**
- Consumes: `validate_policy_sql`, `probe_policy`, `set_access_policy`, `policied_relation`. The preview endpoint runs the policy as a chosen persona (or ad-hoc group set) and returns `{columns, sample_rows, rows_visible, rows_total}` per §13.1, writing an audit row (§13.1 "audited").

- [ ] **Step 1–4: TDD** the setter validation (rejects invalid SQL with the `reason`), the preview matrix numbers, and the audit row.
- [ ] **Step 5: Commit.** `git commit -am "feat(access-policy): admin API attach/clear/preview"`

### Task 15: Admin UI — Access column + editor modal + inline interlock

**Files:**
- Modify: `app/web/templates/admin_tables.html` (Access column in `_renderPackageTableRows()` `:5847`; a policy modal opened from it; reuse `onEditBqAccessModeChange()` `:5110` for the inline interlock warning; history via `detail.version_timeline()`)
- Test: `tests/test_admin_tables_access_policy_ui.py` (route-render + the design-system contract guard)

**Interfaces:**
- Per the web-page playbook: `base_ds`/`base_page` shell, `--ds-*` tokens only, badge language `--ds-accent-{success,warn}-*` (contract-tested by `tests/test_design_system_contract.py`), dual-surface parity if a legacy twin exists.

- [ ] **Step 1: Failing render test** — the Access column shows `—` / `Policy` / `Policy · check`; the panel is disabled-with-reason on a distributed table.
- [ ] **Step 2–4: TDD** the render + the preview-matrix call + inline interlock.
- [ ] **Step 5: Commit.** `git commit -am "feat(access-policy): admin UI editor + Access column"`

### Task 16: CLI

**Files:**
- Modify: `cli/commands/admin.py` (`update-table --policy @file.sql --policy-note "..."` clearing on empty, per the `--query` precedent `:631-687`; `table-policy show|preview <id> [--json] [--as <user>] [--as-groups a,b]`)
- Modify: `cli/query_hints.py` (rejection hints name the next step)
- Test: `tests/test_cli_access_policy.py`

- [ ] **Step 1: Failing tests** — `--policy @file` sets; empty clears; `table-policy preview --as-groups` prints the matrix; `--json` clean on stdout; a 0-row preview distinguishes empty-slice / empty-mapping / unresolvable-identity.
- [ ] **Step 2–4: TDD.**
- [ ] **Step 5: Commit.** `git commit -am "feat(access-policy): CLI policy set/show/preview"`

### Task 17: Effective-access diagnosis + error contracts

**Files:**
- Modify: `app/api/access.py` (`GET /api/admin/users/{id}/effective-access` `:1133`, `GET /api/me/effective-access` `:1189` — add the `policy` block per §10.2)
- Modify: `cli/error_render.py` (render the §16 `reason`-keyed dicts)
- Test: `tests/test_access_policy_effective_access.py`

- [ ] **Step 1: Failing tests** — `/api/me/effective-access` reports `policy: {applies, rows_visible, reason}` per accessible table; each §16 error renders its hint.
- [ ] **Step 2–4: TDD.**
- [ ] **Step 5: Commit.** `git commit -am "feat(access-policy): effective-access diagnosis + error contracts"`

### Task 18: Snapshot fingerprint

**Files:**
- Modify: `app/api/v2_scan.py` (emit the policy fingerprint header), `cli/snapshot_meta.py` (`:22-40` store it), `cli/lib/pull.py` (`:102-106` block the view + list in `snapshot_views_blocked` on mismatch)
- Test: `tests/test_access_policy_snapshot_fingerprint.py`

- [ ] **Step 1: Failing test** — a snapshot of a policied table stores the fingerprint; after the policy changes, `agnes pull` blocks the view.
- [ ] **Step 2–4: TDD.**
- [ ] **Step 5: Commit.** `git commit -am "feat(access-policy): snapshot policy fingerprint"`

### Task 19: Docs, CLAUDE.md, CHANGELOG, release-cut

**Files:**
- Create: `docs/table-access-policies.md` (§20)
- Modify: `docs/RBAC.md` (`:23` — remove the "do not model row-level security on this" sentence, add the policy layer)
- Modify: `CLAUDE.md` (Access control section — third layer)
- Modify: `docs/feature-flags.md` (register `access_policies.enabled`)
- Modify: `CHANGELOG.md` (one `## [Unreleased]` bullet for the whole feature) + `pyproject.toml` version bump + release-cut per `docs/RELEASING.md` if this lands the only Unreleased content
- Modify: `tests/snapshots/openapi.json` (`make update-openapi-snapshot` — the new endpoints change it)

- [ ] **Step 1: Write the doc + corrections.**
- [ ] **Step 2: Regenerate the openapi snapshot.** `make update-openapi-snapshot`
- [ ] **Step 3: Add the CHANGELOG bullet + version bump.**
- [ ] **Step 4: Full targeted suite + openapi gate.**
  Run: `.venv/bin/pytest tests/ -k "access_policy or design_system_contract or openapi_snapshot or db_schema_version" --tb=short -q`
- [ ] **Step 5: Commit.** `git commit -am "docs(access-policy): reference doc, RBAC correction, changelog, release-cut"`

---

## Final verification (the PR acceptance bar, §23.2)

- [ ] `.venv/bin/python scripts/verify_syncmap.py` — clean.
- [ ] Surface ratchet green (Task 13).
- [ ] Fail-closed regression tests green (Tasks 7, 10, 12).
- [ ] E2E green (Task 7): two users see disjoint slices, admin sees all, `agnes pull` downloads nothing for the policied table, `agnes query` prints `[scope]`, `agnes schema` hides the excluded column.
- [ ] `/agnes-review` on the unified diff — advisory findings triaged.
- [ ] CI green on the draft PR (full suite runs there per the local workflow).

---

## Self-review notes (author)

- **Spec coverage:** §3→T4, §4→T1, §5→T5/T6, §6→T5, §7→T10, §8→T13, §9→T12, §10→T11, §11→T9, §12→T5, §13→T14/T15, §14→T3/T12, §15→T3/T4, §16→T17, §17→(fail-closed across T7/T10/T12), §18→T1/T2, §19→(tests in every task), §20→T19. All sections mapped.
- **Serialization:** T1 is the only migration task and must integrate last among schema-touching work (sync-map). Phase 1 serial; Phase 3 parallel; Phase 5 after Phase 3+4.
- **Type consistency:** `PoliciedRelation.relation_sql/params/policied/table_id`, `policied_relation`, `rewrite_sql`, `effective_schema`, `validate_policy_sql`, `probe_policy` — names used identically wherever referenced.
