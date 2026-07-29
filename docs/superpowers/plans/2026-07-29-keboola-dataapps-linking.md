# Linked Data Apps Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. In THIS repo, `/agnes-build` may drive the tasks (it respects the sync-map coupling below); each task still ends green + committed.

**Goal:** Let admins link externally-hosted data apps (Keboola-platform apps ingested via a Keboola MCP source) into Agnes as grantable `linked` resources — visible in the `/apps` UI/CLI/MCP and known to the user's LLM by description.

**Architecture:** A new `repo_mode="linked"` in the existing `data_apps` registry (no container, `external_url` opens the remote app). A Keboola MCP tool in *materialize* mode produces a `keboola_data_apps` table; a generic **projection reconciler** upserts `linked` rows keyed by `(source_ref, keboola_app_id)`. Everything else reuses existing machinery: `ResourceType.DATA_APP` grants, the `/apps` page, `data_apps_list` MCP.

**Tech Stack:** Python/FastAPI, DuckDB + Postgres (dual backend), DuckDB `_vN_to_v(N+1)` + Alembic migrations, Jinja2 web templates, Typer CLI, FastMCP.

**Spec:** `docs/superpowers/specs/2026-07-29-keboola-dataapps-linking-design.md`

## Global Constraints

- **Dual-backend parity (non-negotiable):** every new method on `src/repositories/data_apps.py` gets a matching method on `src/repositories/data_apps_pg.py` in the SAME task; extend `tests/db_pg/*data_apps*contract*` in the same task.
- **Migration ladder:** DuckDB current schema = **v107**; add `_v107_to_v108` + bump `SCHEMA_VERSION` to 108 AND Alembic `0055_dataapps_linked_v108.py`; both reach the same schema (`tests/test_db_schema_version.py`).
- **Quad-surface:** any new REST `/api/*` op needs a CLI command + an MCP tool + a `tests/test_documentation_api_triple_surface.py` classification; user-facing web too. Reuse `ResourceType.DATA_APP` — do NOT add a new ResourceType.
- **Command-UX:** `--linked` is a filter, not a new scope flag; keep `agnes app list` shape; positional term + `--limit`/`--json` vocabulary.
- **Vendor-agnostic public repo:** no customer-specific tokens/hostnames; "Keboola" is fine as the first named ingest adapter but keep the projection/registry generic; placeholders (`example.com`) in docs.
- **No AI attribution in commits/PR.** CHANGELOG bullet under `[Unreleased]` in the same PR; release-cut is the last commit.
- **Reach repos via factory** (`data_apps_repo()`), never instantiate repo classes at callsites.

## Sync-map coupling (task ordering)

1. **Migration first** (Task 1) — schema before repo methods.
2. **Parity siblings coupled** (Task 2) — duck + pg + contract in one task.
3. Projection/adapter (Task 3) and sync wiring (Task 4) depend on Task 2.
4. Surfaces (Tasks 5–8) depend on Task 2; independent of each other.
5. **Release-cut last** (Task 9).

## File Structure

- `src/db.py` — CREATE TABLE `data_apps` + `_v107_to_v108` + `SCHEMA_VERSION=108`.
- `migrations/versions/0055_dataapps_linked_v108.py` — Alembic step (mirror).
- `src/repositories/data_apps.py` / `data_apps_pg.py` — linked methods (parity).
- `src/data_apps/linked_projection.py` — generic reconciler (NEW).
- `src/data_apps/keboola_adapter.py` — Keboola table/column mapping (NEW, thin).
- `src/orchestrator.py` — call projection after an MCP-source rebuild.
- `app/api/data_apps.py` — `?kind=` filter + `PATCH /{slug}` description-override.
- `cli/commands/data_apps.py` — `--linked` filter + `set-description`.
- `app/api/mcp/foundation_tools.py` + `cli/mcp/server.py` — `data_app_set_description` tool (parity).
- `app/web/router.py` + `templates/data_apps.html` + `templates/data_app_detail.html` — linked rows + conditionals.
- Tests alongside each.

---

### Task 1: Schema migration — `data_apps` linked columns

**Files:**
- Modify: `src/db.py` (CREATE TABLE `data_apps` ~L58; add `_v107_to_v108`; `SCHEMA_VERSION` → 108; register in the migration ladder ~L7623)
- Create: `migrations/versions/0055_dataapps_linked_v108.py`
- Test: `tests/test_db_schema_version.py` (already parametrizes; assert v108 reachable both backends)

**Interfaces:**
- Produces: `data_apps` columns `external_url TEXT NULL`, `source_ref TEXT NULL`, `managed BOOLEAN NOT NULL DEFAULT FALSE`, `description_override TEXT NULL`; `repo_mode` now allows value `'linked'`.

- [ ] **Step 1:** Add the four columns to `CREATE TABLE data_apps` in `src/db.py` (fresh-install path).
- [ ] **Step 2:** Write `_v107_to_v108(conn)` issuing `ALTER TABLE data_apps ADD COLUMN …` for each (guard with try/except-exists per the existing migration idiom in this file); bump `SCHEMA_VERSION = 108`; call `_v107_to_v108(conn)` in the ladder after `_v106_to_v107`.
- [ ] **Step 3:** Alembic `0055_dataapps_linked_v108.py` `down_revision="0054_..."`; `op.add_column` for the four columns (Postgres types: `sa.Text()`, `sa.Boolean(server_default=sa.false())`).
- [ ] **Step 4:** Run `.venv/bin/pytest tests/test_db_schema_version.py -q` — both backends reach v108.
- [ ] **Step 5:** Commit `feat(db): data_apps linked columns (v108)`.

---

### Task 2: Repo methods — linked lifecycle (DuckDB + PG parity)

**Files:**
- Modify: `src/repositories/data_apps.py`, `src/repositories/data_apps_pg.py`
- Test: `tests/db_pg/test_data_apps_contract.py` (or the existing data_apps contract file)

**Interfaces:**
- Produces (both repos, identical signatures):
  - `create_linked(*, slug, name, description, external_url, source_ref, owner_user_id) -> dict`
  - `upsert_linked(*, source_ref, name, description, external_url) -> dict` — insert-or-update keyed by `source_ref`; on update refreshes name/description/external_url but NEVER `description_override`; returns the row.
  - `list_linked(source_ref: str | None = None, include_inactive=False) -> list[dict]`
  - `soft_delete_missing_linked(source_ref_prefix: str, keep_source_refs: list[str]) -> int` — mark rows for that connection whose `source_ref` isn't in `keep_source_refs` as `state='linked_hidden'`; returns count.
  - `set_description_override(slug, text: str | None) -> dict`
  - `effective_description(row) -> str` helper: `row["description_override"] or row["description"]`.
- Note: `source_ref` scheme = `"<connection_id>:<keboola_app_id>"`; `source_ref_prefix` = `"<connection_id>:"`.

- [ ] **Step 1:** Contract test: `upsert_linked` inserts then updates the same row on second call (same `source_ref`); `description_override` survives an `upsert_linked` that changes `description`.
- [ ] **Step 2:** Contract test: `soft_delete_missing_linked` hides rows of connection A not in keep-list, leaves connection B's rows untouched (per-source isolation).
- [ ] **Step 3:** Run tests → FAIL (methods missing) on both `[duck]` and `[pg]`.
- [ ] **Step 4:** Implement all methods on both repos (escape identifiers, parameterized SQL; PG uses `%s`, DuckDB `?`). `create`/`_serialize` accept `repo_mode='linked'` and surface `external_url`, `managed`, `description_override`, effective description.
- [ ] **Step 5:** Run `.venv/bin/pytest tests/db_pg/ -k data_apps -q` → PASS both backends.
- [ ] **Step 6:** Commit `feat(repos): data_apps linked lifecycle (duck+pg parity)`.

---

### Task 3: Projection reconciler + Keboola adapter

**Files:**
- Create: `src/data_apps/linked_projection.py`, `src/data_apps/keboola_adapter.py`
- Test: `tests/test_linked_projection.py`

**Interfaces:**
- Consumes: `data_apps_repo()` (Task 2 methods).
- Produces:
  - `keboola_adapter.MATERIALIZED_TABLE = "keboola_data_apps"`; `keboola_adapter.map_row(raw: dict) -> LinkedAppRecord` (fields: `keboola_app_id, name, description, external_url`); `keboola_adapter.source_ref(connection_id, keboola_app_id) -> str`.
  - `linked_projection.project(connection_id: str, records: list[LinkedAppRecord]) -> ProjectionResult` — upserts each via `upsert_linked`, then `soft_delete_missing_linked` for records absent this round; returns counts `{created, updated, hidden}`; logs hidden slugs (no silent drop).

- [ ] **Step 1:** Test: `project("conn1", [rec_a, rec_b])` creates 2 linked rows; a second `project("conn1", [rec_a])` hides `rec_b` (created=0, updated=1, hidden=1) and leaves a `conn2` row untouched.
- [ ] **Step 2:** Test: `description_override` set on `rec_a`'s slug survives a re-project with a changed `description`.
- [ ] **Step 3:** Run → FAIL.
- [ ] **Step 4:** Implement `keboola_adapter` (pure mapping) + `linked_projection.project` (loop upsert + soft-delete-missing, structured logging).
- [ ] **Step 5:** Run `.venv/bin/pytest tests/test_linked_projection.py -q` → PASS.
- [ ] **Step 6:** Commit `feat(data-apps): linked projection reconciler + keboola adapter`.

---

### Task 4: Wire projection into the post-sync step

**Files:**
- Modify: `src/orchestrator.py` (after an MCP source's materialize rebuild) OR the MCP-source sync driver that calls `connectors/mcp` extractor.
- Test: `tests/test_linked_projection_sync.py`

**Interfaces:**
- Consumes: `linked_projection.project`, the materialized `keboola_data_apps` view.
- Produces: after a rebuild of an MCP source that carries a `keboola_data_apps` materialized table, the projection runs for that source's `connection_id`, reading rows from the view via the adapter mapping.

- [ ] **Step 1:** Test: seed a fake `keboola_data_apps` view (2 rows) attached for a source; trigger the source rebuild; assert 2 `linked` rows now exist in `data_apps`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement: in the post-rebuild seam, detect the materialized `keboola_data_apps` table for the source, read rows, `keboola_adapter.map_row` each, call `linked_projection.project(connection_id, records)`. Guard: absent table → no-op (sources without the lister are unaffected).
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat(sync): run linked projection after MCP-source materialize`.

---

### Task 5: REST — `kind` filter + description-override endpoint

**Files:**
- Modify: `app/api/data_apps.py`
- Test: `tests/test_data_apps_linked_api.py`

**Interfaces:**
- Produces:
  - `GET /api/data-apps?kind=linked|hosted` — filters `_serialize`d list (kind derived: `linked` iff `repo_mode=='linked'`, else `hosted`). RBAC unchanged (`_can_view`).
  - `PATCH /api/data-apps/{slug}` body `{"description": str}` — sets `description_override`; gated `_require_owner_or_admin` (linked rows are admin-owned so effectively admin-gated); 404 if missing; 409 if not `managed` (hosted apps edit description via the normal create/update path).

- [ ] **Step 1:** Tests: viewer with a grant sees a linked app in `?kind=linked`; a non-granted user does not; `PATCH` by admin sets override and `GET` reflects effective description; `PATCH` by a non-admin viewer → 403.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement filter + PATCH handler (reuse `_serialize`, `effective_description`, `set_description_override`).
- [ ] **Step 4:** Add a triple-surface classification row for `PATCH /api/data-apps/{slug}` in `tests/test_documentation_api_triple_surface.py` (`_COHORT`, CLI+MCP verified in Tasks 6–7).
- [ ] **Step 5:** Run `.venv/bin/pytest tests/test_data_apps_linked_api.py -q` → PASS.
- [ ] **Step 6:** Commit `feat(api): data-apps kind filter + description override`.

---

### Task 6: CLI — `--linked` filter + `set-description`

**Files:**
- Modify: `cli/commands/data_apps.py`
- Test: `tests/test_cli_api_parity.py` (add the set-description parity case) + a focused CLI test.

**Interfaces:**
- Consumes: `GET /api/data-apps?kind=`, `PATCH /api/data-apps/{slug}`.
- Produces: `agnes app list [--linked] [--json]` (linked rows show a `[linked]` badge + URL); `agnes app set-description <slug> <text>`.

- [ ] **Step 1:** Test: `agnes app list --linked` calls `GET …?kind=linked`; `agnes app set-description s "d"` calls `PATCH /api/data-apps/s` with `{"description":"d"}`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement the two CLI paths via `cli/client.py`.
- [ ] **Step 4:** Run the CLI + parity tests → PASS.
- [ ] **Step 5:** Commit `feat(cli): agnes app --linked + set-description`.

---

### Task 7: MCP — `data_app_set_description` tool (foundation + stdio parity)

**Files:**
- Modify: `app/api/mcp/foundation_tools.py` (add to `FOUNDATION_TOOL_NAMES` + `DATA_APP_TOOL_NAMES` + register), `cli/mcp/server.py` (mirror)
- Test: `tests/test_mcp_tool_parity.py`

**Interfaces:**
- Produces: `data_app_set_description(slug: str, description: str) -> dict` on BOTH surfaces, calling `PATCH /api/data-apps/{slug}`.

- [ ] **Step 1:** Extend the parity guard: `data_app_set_description` ∈ `DATA_APP_TOOL_NAMES` and exposed by the stdio server.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement the tool on foundation (async, `base_url`/`headers_fn`) + stdio (`api_post_json`/PATCH helper — add `api_patch_json` to `cli/v2_client.py` if absent).
- [ ] **Step 4:** Run `.venv/bin/pytest tests/test_mcp_tool_parity.py -q` → PASS.
- [ ] **Step 5:** Commit `feat(mcp): data_app_set_description (foundation+stdio)`.

---

### Task 8: Web UI — linked rows in `/apps` + detail conditionals

**Files:**
- Modify: `app/web/router.py` (`data_apps_list_page`, `data_app_detail_page`: pass `kind`, source label, effective description), `app/web/templates/data_apps.html`, `app/web/templates/data_app_detail.html`
- Test: `tests/test_web_ui.py` (a linked-apps render case)

**Interfaces:**
- Consumes: `_serialize` (now carries `kind`, `external_url`, effective description), `_can_view`.

- [ ] **Step 1:** Test: `/apps` with a granted linked app renders a row with a `linked` badge and `Open ↗` → `external_url`; detail page for a linked app shows description + Open and NOT the Deploy/Stop controls.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Template conditionals on `a.kind == 'linked'`: badge, Owner→source label, `Open ↗` href = `external_url` (always enabled); detail: `{% if a.kind == 'hosted' %}`-gate the deploy/logs block, add an admin description-override `<form>` for linked. Broaden hero subtitle. Ensure `_chrome_ctx(request, user)` is spread (design-system requirement).
- [ ] **Step 4:** Run `.venv/bin/pytest tests/test_web_ui.py -k linked -q` → PASS; screenshot check if driving a live page.
- [ ] **Step 5:** Commit `feat(web): linked data-app rows in /apps + detail`.

---

### Task 9: CHANGELOG + release-cut

**Files:** `CHANGELOG.md`, `pyproject.toml`

- [ ] **Step 1:** Add an `[Unreleased] → Added` bullet describing linked data apps (ingest via Keboola MCP, `linked` kind, grantable resource, `/apps` + CLI + MCP + description override) — user-visible, vendor-neutral.
- [ ] **Step 2:** Run the full suite `.venv/bin/pytest tests/ --tb=short -n auto -q` (accept the known `-n auto` fixture-saturation flakes; verify touched-area green in isolation).
- [ ] **Step 3:** Release-cut as the LAST commit: bump `pyproject.toml` patch, rename `[Unreleased]` → `[X.Y.Z]`, fresh empty `[Unreleased]`. Rebase onto latest `origin/main` first (treadmill) and re-cut to the next free patch.
- [ ] **Step 4:** Commit `chore(release): cut vX.Y.Z`.

---

## Deferred (NOT in this PR)

- **Phase 2 — knowledge-item projection** (spec §LLM): project a grant-scoped
  corporate-memory knowledge item per linked app so `knowledge_search` surfaces
  it. Requires wiring knowledge-item visibility to `resource_grants` (no leak).
  Deferred behind a sub-flag; Phase 1 (`data_apps_list` carries description)
  already satisfies "the LLM knows the description".
- **Storage API supplement** in `keboola_adapter` if the Keboola MCP tool omits
  URL/description (confined to the adapter; projection contract unchanged).
- **Re-host a linked app** (promote linked → hosted) — the model allows it; not built.

## Self-review notes

- Spec coverage: data model (T1–2), ingest+projection (T3–4), REST/CLI/MCP/Web
  surfaces (T5–8), reconcile/isolation (T2–3), LLM Phase 1 (existing
  `data_apps_list`, verified surfaced via T2 `_serialize`), Phase 2 deferred.
- Parity: T2 couples duck+pg+contract; T7 couples foundation+stdio.
- Migration serialized first (T1); release-cut last (T9).
