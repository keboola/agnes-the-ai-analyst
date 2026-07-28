# Agnes Dev-Agent Kit — Slice C (Builder + agnes-conventions) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A disciplined `agnes-builder` feature-implementer agent backed by an `agnes-conventions` skill whose five reference playbooks (connector, endpoint+RBAC, web page, repo+parity, migration) are verified against the current codebase.

**Architecture:** The builder agent is thin — it carries the non-negotiables (TDD, dual-backend parity, migration-ladder sync, CHANGELOG, vendor-agnostic, scope discipline) and routes to one of five playbooks. Knowledge lives in `agnes-conventions/SKILL.md` + `references/*.md`, loaded on demand. A structural test (extending `tests/test_dev_agent_kit.py`) guards that the agent, skill, and all five references exist and cross-reference correctly.

**Tech Stack:** Claude Code agent/skill markdown, pytest (structural guards). The playbook content was verified against v0.66.1 by reading the cited `file:line` anchors.

Spec: `docs/superpowers/specs/2026-06-05-agnes-dev-agent-kit-design.md` §4, §8.

---

## File Structure

- Create: `.claude/agents/agnes-builder.md`
- Create: `.claude/skills/agnes-conventions/SKILL.md`
- Create: `.claude/skills/agnes-conventions/references/connector.md`
- Create: `.claude/skills/agnes-conventions/references/repo-parity.md`
- Create: `.claude/skills/agnes-conventions/references/migration.md`
- Create: `.claude/skills/agnes-conventions/references/endpoint-rbac.md`
- Create: `.claude/skills/agnes-conventions/references/web-page.md`
- Modify: `tests/test_dev_agent_kit.py` (append structural tests)
- Modify: `CHANGELOG.md`

`.claude/skills/` is already whitelisted in `.gitignore` (`!.claude/skills/`), so the new `agnes-conventions/` subdirectory and its files are tracked automatically — no gitignore change needed (verify with `git check-ignore` if in doubt).

---

## Task 1: Builder agent + conventions skill + structural guard

**Files:**
- Modify: `tests/test_dev_agent_kit.py` (append)
- Create: `.claude/agents/agnes-builder.md`
- Create: `.claude/skills/agnes-conventions/SKILL.md`

- [ ] **Step 1: Append the failing tests**

Append to the END of `tests/test_dev_agent_kit.py`:

```python
SKILLS_DIR = REPO_ROOT / ".claude" / "skills"
CONVENTIONS = SKILLS_DIR / "agnes-conventions"
PLAYBOOKS = ["connector", "repo-parity", "migration", "endpoint-rbac", "web-page"]


def test_builder_agent_has_valid_frontmatter():
    path = AGENTS_DIR / "agnes-builder.md"
    assert path.exists(), "agnes-builder.md must exist"
    fm = read_frontmatter(path)
    assert fm.get("name") == "agnes-builder", "name must match filename"
    assert "description" in fm, "agent must declare a description"
    assert "tools" in fm, "agent must declare its tools"


def test_builder_references_conventions_and_sync_map():
    text = (AGENTS_DIR / "agnes-builder.md").read_text(encoding="utf-8")
    assert "agnes-conventions" in text, "builder must route to agnes-conventions"
    assert "CONTRIBUTING.md" in text, "builder must point at the sync-map"


def test_conventions_skill_exists_and_lists_playbooks():
    skill = CONVENTIONS / "SKILL.md"
    assert skill.exists(), "agnes-conventions/SKILL.md must exist"
    fm = read_frontmatter(skill)
    assert fm.get("name") == "agnes-conventions", "skill name must match dir"
    text = skill.read_text(encoding="utf-8")
    for pb in PLAYBOOKS:
        assert f"references/{pb}.md" in text, f"SKILL.md must list references/{pb}.md"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_dev_agent_kit.py -k "builder or conventions" -v`
Expected: FAIL — `agnes-builder.md must exist`.

- [ ] **Step 3: Create `.claude/agents/agnes-builder.md`**

```markdown
---
name: agnes-builder
description: Disciplined Agnes feature implementer. Use when adding a data-source connector, REST API endpoint, web page, repository (method), or schema migration. Enforces the non-negotiables (TDD-first, DuckDB↔Postgres parity in the same change, migration-ladder sync, CHANGELOG, vendor-agnostic, scope discipline) and routes to the agnes-conventions playbooks. Writes code — it does not review (use /agnes-review for that).
tools: Read, Write, Edit, Bash, Grep, Glob, TodoWrite
model: sonnet
---

You implement features in the Agnes repo with strict, predictable discipline.
Read the `agnes-conventions` skill and the `CONTRIBUTING.md` sync-map before
writing any code. Respond in the parent's language; code, comments, commit
messages, and CHANGELOG stay English.

## Non-negotiable rules (check before every change)

1. **TDD-first.** Write the failing test, watch it fail, then the minimal
   implementation. Before claiming done, run the full suite:
   `.venv/bin/pytest tests/ --tb=short -n auto -q`.
2. **Dual-backend parity in the SAME change.** Touch `src/repositories/X.py` →
   also touch `src/repositories/X_pg.py`, register both in
   `src/repositories/__init__.py` `_REGISTRY`, and extend the contract test.
   Never "PG later". Reach repos via the `*_repo()` factory, never instantiate.
3. **Migration ladder.** An Alembic revision under `migrations/versions/` must
   have a matching `_vN_to_v(N+1)` in `src/db.py` (bump `SCHEMA_VERSION`), update
   `src/db_pg.py` `Base.metadata`, and both ladders reach the same endpoint.
4. **CHANGELOG.** Add a `## [Unreleased]` bullet for any user-visible behavior.
5. **Vendor-agnostic.** No customer-specific tokens (deployments, project IDs,
   hostnames, private-repo references) in code, config, comments, or docs.
6. **Scope discipline + issue economy.** Don't refactor unrelated code; fix or
   close, don't spawn issues.
7. **Web pages** extend `base_page.html` / `base_ds.html`, never `base.html`.

## Routing — load the matching playbook

Read the one `agnes-conventions/references/*.md` that fits the task:

| Task | Playbook |
|---|---|
| New data source | `connector.md` |
| New REST endpoint | `endpoint-rbac.md` |
| New dashboard page | `web-page.md` |
| New repository / method | `repo-parity.md` |
| Schema change | `migration.md` |

## Output contract

Report, in a compact block: what changed · parity sibling touched? (repos) ·
migration ladders both updated? · CHANGELOG bullet added? · tests run + result ·
next step. If you could not keep parity or the migration ladder in sync, STOP
and say so — never ship a one-sided change.
```

- [ ] **Step 4: Create `.claude/skills/agnes-conventions/SKILL.md`**

```markdown
---
name: agnes-conventions
description: Agnes implementation playbooks + non-negotiables. Use when implementing a feature in this repo — adding a data-source connector, REST API endpoint, HTML dashboard page, repository method/repo, or schema migration. Routes to per-task reference playbooks verified against the codebase.
---

# Agnes conventions

The non-negotiables (what must change together) live in `CONTRIBUTING.md` →
**Sync-map**. This skill holds the step-by-step playbooks. Read `CONTRIBUTING.md`
first, then load the one playbook matching your task:

- `references/connector.md` — new data-source connector (the `extract.duckdb` contract)
- `references/endpoint-rbac.md` — new REST endpoint + the correct RBAC gate
- `references/web-page.md` — new HTML dashboard page (design-system page shell)
- `references/repo-parity.md` — new repository / method with DuckDB↔Postgres parity
- `references/migration.md` — schema migration on both the DuckDB and Alembic ladders

Each playbook cites `file:line` anchors verified against the current codebase.
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_dev_agent_kit.py -k "builder or conventions" -v`
Expected: PASS (3 new tests).

- [ ] **Step 6: Commit**

```bash
git add tests/test_dev_agent_kit.py .claude/agents/agnes-builder.md .claude/skills/agnes-conventions/SKILL.md
git commit -m "feat(dev-kit): add agnes-builder agent + agnes-conventions skill"
```

---

## Task 2: Backend playbooks (connector, repo-parity, migration)

**Files:**
- Modify: `tests/test_dev_agent_kit.py` (append)
- Create: `.claude/skills/agnes-conventions/references/connector.md`
- Create: `.claude/skills/agnes-conventions/references/repo-parity.md`
- Create: `.claude/skills/agnes-conventions/references/migration.md`

- [ ] **Step 1: Append the failing test**

Append to the END of `tests/test_dev_agent_kit.py`:

```python
@pytest.mark.parametrize("pb", ["connector", "repo-parity", "migration"])
def test_backend_playbooks_exist(pb):
    path = CONVENTIONS / "references" / f"{pb}.md"
    assert path.exists(), f"references/{pb}.md must exist"
    assert path.read_text(encoding="utf-8").strip(), f"{pb}.md must not be empty"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_dev_agent_kit.py -k backend_playbooks -v`
Expected: FAIL — `references/connector.md must exist`.

- [ ] **Step 3: Create `.claude/skills/agnes-conventions/references/connector.md`**

````markdown
# Playbook: new data-source connector

The orchestrator is **filesystem-driven** — there is no registration step. Write
`$DATA_DIR/extracts/<name>/extract.duckdb` and `SyncOrchestrator.rebuild()`
(`src/orchestrator.py:364`) discovers it, ATTACHes it, and creates master views.

## Files to create

```
connectors/<name>/__init__.py      # empty / package docstring
connectors/<name>/extractor.py     # writes extract.duckdb (+ data/*.parquet for local)
```

Do **not** modify `src/orchestrator.py` — the scan is path-driven.

## The `_meta` table (required)

Every extract.duckdb must contain `_meta`, one row per table. Exact shape
(`connectors/keboola/extractor.py:398`):

```sql
CREATE TABLE _meta (
    table_name   VARCHAR NOT NULL,            -- becomes the master view name; must match ^[a-zA-Z_][a-zA-Z0-9_]{0,63}$
    description  VARCHAR,
    rows         BIGINT,                       -- 0 for remote
    size_bytes   BIGINT,                       -- 0 for remote
    extracted_at TIMESTAMP,                     -- datetime.now(timezone.utc)
    query_mode   VARCHAR DEFAULT 'local'        -- 'local' | 'remote' | 'materialized'
)
```

The orchestrator skips any `_meta` row whose `table_name` lacks a matching
view/table object in the extract (`src/orchestrator.py:459`).

## query_mode

- **local** — write `data/<table_name>.parquet` and a view
  `CREATE OR REPLACE VIEW "<table_name>" AS SELECT * FROM read_parquet(...)`.
- **remote** — the view references an external ATTACH alias; requires
  `_remote_attach` (below).
- **materialized** — written by the scheduled sync pass, not the extractor; the
  extractor skips these rows.

## `_remote_attach` (remote mode only)

Shape (`connectors/keboola/extractor.py:411`):

```sql
CREATE TABLE _remote_attach (alias VARCHAR, extension VARCHAR, url VARCHAR, token_env VARCHAR)
```

`token_env` is the env-var holding the token (`''` for an extension-specific auth
path, e.g. BigQuery's GCE metadata server). **Gotcha:** the `extension` must be in
`_COMMUNITY_EXTENSIONS` in `src/orchestrator_security.py:24` (currently
`{"keboola", "bigquery"}`) or the ATTACH is silently refused at rebuild.

## Steps

1. `connectors/<name>/__init__.py` (empty).
2. `connectors/<name>/extractor.py`: open `extract.duckdb.tmp`, create `_meta`
   (verbatim DDL), per table write parquet + view + insert `_meta` row; for remote
   add `_remote_attach` (and register the extension). Atomic `shutil.move` the tmp
   over `extract.duckdb` (`connectors/keboola/extractor.py:753`).
3. Register tables in `table_registry` (`source_type='<name>'`) via the admin API
   / `TableRegistryRepository` — read by the sync trigger, not by `rebuild()`.
4. TDD: a test that runs the extractor against a fixture and asserts the
   extract.duckdb `_meta` shape + that `rebuild()` creates the master views.

## Anchors

- `_meta` DDL: `connectors/keboola/extractor.py:398`
- `_remote_attach` + extension allowlist: `connectors/keboola/extractor.py:411`, `src/orchestrator_security.py:24`
- discovery + ATTACH: `src/orchestrator.py:364`
````

- [ ] **Step 4: Create `.claude/skills/agnes-conventions/references/repo-parity.md`**

````markdown
# Playbook: repository / method with DuckDB↔Postgres parity

Both backends are first-class. A one-sided change is a BLOCKING parity gap caught
by guards (below). Reach repos via the `*_repo()` factory, never instantiate a
repo class directly.

## Files (a new repo touches all four)

1. `src/repositories/<name>.py` — DuckDB impl: `class <Name>Repository` taking a
   `duckdb.DuckDBPyConnection` in `__init__`; positional `?` bindings; returns
   plain `dict`/`list[dict]`/`None`. Shape: `src/repositories/sync_state.py:10`.
2. `src/repositories/<name>_pg.py` — Postgres impl: `class <Name>PgRepository`
   taking a SQLAlchemy `Engine`; `sa.text(...)` with `:named` binds; reads under
   `with self._engine.connect()`, writes under `with self._engine.begin()`. Shape:
   `src/repositories/sync_state_pg.py:17`.
3. `src/repositories/__init__.py` — THREE edits:
   - add `"<name>_repo"` to `__all__`;
   - add a `_REGISTRY` entry: `"<name>": {DUCKDB: ("src.repositories.<name>", "<Name>Repository"), PG: ("src.repositories.<name>_pg", "<Name>PgRepository")}`;
   - add the factory fn `def <name>_repo() -> Any: return _build("<name>")`.
4. `tests/db_pg/test_<name>_contract.py` — parametrize `["duckdb", "pg"]` through
   the same assertions. Model it on `tests/db_pg/test_mcp_sources_contract.py`.

## Method-mirroring rule

Every public method on the DuckDB class must exist on the PG class with identical
parameter names (PG may add defaulted params, never drop). This is an AST check —
no DB needed.

## Guards that fail if you skip a step

| Skipped | Failing test |
|---|---|
| `_REGISTRY` entry / asymmetric backends | `tests/test_repository_registry.py::test_registry_backends_are_symmetric` |
| `__all__` / factory fn | `tests/test_repository_registry.py::test_every_public_factory_has_a_registry_entry` |
| PG missing a public method | `tests/test_repo_method_parity.py` |
| Direct `XRepository(conn)` instead of factory | `tests/test_backend_split_guard.py` |
| `get_system_db()` in a handler | `tests/test_backend_split_guard.py` |
| Semantic drift (e.g. JSON dict vs str) | your `tests/db_pg/test_<name>_contract.py` |

## Steps

1. TDD: write `tests/db_pg/test_<name>_contract.py` first (it fails — no repo).
2. Write the DuckDB repo, then the PG repo (mirror signatures).
3. Make the three `__init__.py` edits.
4. Green the contract test + registry/parity guards.

## Anchors

- factory `_build` / `_REGISTRY` / `_ARG_PROVIDERS`: `src/repositories/__init__.py`
- paired example: `src/repositories/sync_state.py` + `src/repositories/sync_state_pg.py`
- contract example: `tests/db_pg/test_mcp_sources_contract.py`
````

- [ ] **Step 5: Create `.claude/skills/agnes-conventions/references/migration.md`**

````markdown
# Playbook: schema migration (both ladders)

Two ladders must reach the SAME endpoint: the DuckDB ladder in `src/db.py` and the
Alembic ladder in `migrations/versions/`. The Postgres model in `src/db_pg.py`
(`Base.metadata`) must also match, or autogenerate-drift fails.

## DuckDB side (`src/db.py`)

1. Bump `SCHEMA_VERSION` (currently 72 at `src/db.py:50`) → next integer.
2. Write `def _v72_to_v73(conn): ...` ending with
   `conn.execute("UPDATE schema_version SET version = 73")`. Use idempotent
   `CREATE ... IF NOT EXISTS`. Worked example: `_v71_to_v72` at `src/db.py:4905`.
3. Wire it into `_ensure_schema` in BOTH places:
   - the `current == 0` fresh-install block (after the prior `_vNN` call);
   - the upgrade block: `if current < 73: _v72_to_v73(conn)` after the `< 72` guard.

## Alembic side (`migrations/versions/`)

Create `migrations/versions/00NN_<desc>_v73.py` (naming: `NNNN_<desc>_v<duckdb_version>.py`):

```python
revision = "00NN_<desc>_v73"
down_revision = "<previous revision id>"   # chain to the current head

def upgrade() -> None: ...   # op.create_table / op.add_column
def downgrade() -> None: ...  # exact inverse
```

Then update `src/db_pg.py` `Base.metadata` (the SQLAlchemy models) to match the
new structural change.

## Integration gates

- `tests/test_db_schema_version.py` — drives old DuckDB files up the ladder and
  asserts they reach `SCHEMA_VERSION`. Fails if a `_vN_to_v(N+1)` fn or its
  dispatch guard is missing.
- `tests/db_pg/test_alembic_roundtrip.py` — upgrade/downgrade roundtrips +
  `test_no_model_migration_drift` (autogenerate diff vs `Base.metadata` must be
  empty → this is why you update `src/db_pg.py`).

## Steps

1. TDD: add the schema-version test expectation / a test for the new table.
2. DuckDB: bump version, write `_vN_to_v(N+1)`, wire both dispatch sites.
3. Alembic: new revision (up + down), chained to head.
4. Update `src/db_pg.py` `Base.metadata`.
5. Green `test_db_schema_version.py` + `test_alembic_roundtrip.py`.

## Anchors

- `SCHEMA_VERSION`: `src/db.py:50`; recent migration fn: `src/db.py:4905`
- recent revision: `migrations/versions/0019_system_secrets_v72.py`
- gates: `tests/test_db_schema_version.py`, `tests/db_pg/test_alembic_roundtrip.py`
````

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_dev_agent_kit.py -k backend_playbooks -v`
Expected: PASS (3 parametrized cases).

- [ ] **Step 7: Commit**

```bash
git add tests/test_dev_agent_kit.py .claude/skills/agnes-conventions/references/connector.md .claude/skills/agnes-conventions/references/repo-parity.md .claude/skills/agnes-conventions/references/migration.md
git commit -m "feat(dev-kit): add connector/repo-parity/migration playbooks"
```

---

## Task 3: App-surface playbooks (endpoint-rbac, web-page)

**Files:**
- Modify: `tests/test_dev_agent_kit.py` (append)
- Create: `.claude/skills/agnes-conventions/references/endpoint-rbac.md`
- Create: `.claude/skills/agnes-conventions/references/web-page.md`

- [ ] **Step 1: Append the failing test**

Append to the END of `tests/test_dev_agent_kit.py`:

```python
@pytest.mark.parametrize("pb", ["endpoint-rbac", "web-page"])
def test_app_playbooks_exist(pb):
    path = CONVENTIONS / "references" / f"{pb}.md"
    assert path.exists(), f"references/{pb}.md must exist"
    assert path.read_text(encoding="utf-8").strip(), f"{pb}.md must not be empty"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_dev_agent_kit.py -k app_playbooks -v`
Expected: FAIL — `references/endpoint-rbac.md must exist`.

- [ ] **Step 3: Create `.claude/skills/agnes-conventions/references/endpoint-rbac.md`**

````markdown
# Playbook: new REST endpoint + RBAC gate

## Files

1. `app/api/<feature>.py` — define `router = APIRouter(prefix="/api/<feature>", tags=["<feature>"])`.
2. `app/main.py` — import + `app.include_router(<feature>_router)` (imports block
   ~`app/main.py:211`, include block ~`app/main.py:1130`).

## Choose the gate (both in `app/auth/access.py`)

- **`require_admin`** (`app/auth/access.py:235`) — app-level mutations only admins
  may do (create/delete, run sync, configure). Denies session-principal tokens.
  Use: `_user: dict = Depends(require_admin)`. Example: `app/api/admin_bigquery_test.py:33`.
- **`require_resource_access(ResourceType.X, "{path_param}")`**
  (`app/auth/access.py:262`) — entity-scoped access. It's a FACTORY returning a
  dependency; the 2nd arg is a format string resolved against the route's
  path params at request time; admins short-circuit. Examples:
  `app/api/marketplace.py:1603` (interpolated), `app/api/chat.py:27` (fixed id).

```python
from app.auth.access import require_resource_access
from app.resource_types import ResourceType

@router.get("/{plugin_id}")
async def detail(plugin_id: str,
    _u: dict = Depends(require_resource_access(ResourceType.MARKETPLACE_PLUGIN, "{plugin_id}"))):
    ...
```

## New resource type? (one file: `app/resource_types.py`)

1. Add a `ResourceType` StrEnum member (`app/resource_types.py:36`) — value is
   persisted in `resource_grants.resource_type`; never rename an existing member.
2. Write a `list_blocks` delegate `(conn) -> list[Block]` returning
   `[{id, name, items:[{resource_id, name, ...}]}]` where `resource_id` matches
   what's stored in grants.
3. Register a `ResourceTypeSpec` in `RESOURCE_TYPES` (`app/resource_types.py:424`)
   with `key`, `display_name`, `description`, `id_format`, `list_blocks`.
   No DB migration — the admin `/access` page picks it up automatically.

## Steps

1. TDD: write an API test (auth'd + unauth'd) asserting the gate (403 without
   access, 200 with). 
2. Create the router, register it in `app/main.py`, add the gate, (new resource
   type if needed).
3. Green the test.

## Anchors

- gates: `app/auth/access.py:235`, `app/auth/access.py:262`
- gated examples: `app/api/admin_bigquery_test.py:33`, `app/api/marketplace.py:1603`, `app/api/chat.py:27`
- resource types: `app/resource_types.py:36`, `app/resource_types.py:424`
- router registration: `app/main.py:211` (imports), `app/main.py:1130` (includes)
````

- [ ] **Step 4: Create `.claude/skills/agnes-conventions/references/web-page.md`**

````markdown
# Playbook: new HTML dashboard page (design-system shell)

## Which base

- **Extend `base_page.html`** for a standard page (hero strip + toolbar + body).
  It extends `base_ds.html` and gives you the three-section shell.
- Extend `base_ds.html` directly only for bespoke full-width layout (override
  `{% block layout %}`).
- **Never `base.html`** — legacy. `ds.*` macros are auto-imported
  (`app/web/templates/base_ds.html:78`) — no `{% import %}` needed.

## Files

1. `app/web/templates/<page>.html`
2. `app/web/router.py` — a route handler.

## Template skeleton

```html
{% extends "base_page.html" %}
{% block title %}My Page — {{ config.INSTANCE_NAME }}{% endblock %}
{% set page_hero_eyebrow = "Section" %}
{% set page_hero_title = "My Page" %}
{% set page_hero_subtitle = "One line." %}
{% block head_extra %}<style>/* page-local CSS, see rules */</style>{% endblock %}
{% block toolbar %}{{ ds.button('+ Add', variant='primary') }}{% endblock %}
{% block page %}<table class="data-table">…</table>{% endblock %}
{% block scripts %}<script>/* page JS */</script>{% endblock %}
```

Wider shell: `{% block container_modifier %}container--wide{% endblock %}`.

## Route (`app/web/router.py`)

```python
@router.get("/my-page", response_class=HTMLResponse)
async def my_page(request: Request, user: dict = Depends(require_admin),
                  conn: duckdb.DuckDBPyConnection = Depends(_get_db)):
    ctx = _build_context(request, user=user, conn=conn, my_data=...)
    return templates.TemplateResponse(request, "my_page.html", ctx)
```

Real pattern: `app/web/router.py` `admin_users_page` (~`:2409`).

## CSS rules (enforced by `tests/test_design_system_contract.py`)

Use canonical classes (`.btn`, `.btn-primary`, `.search-input`, `.data-table`,
`.empty-state`, …). **Banned:** `var(--primary)` → use `var(--ds-primary)`; raw
`#RRGGBB` hex → use `var(--ds-*)`; `.container:has(.X-page)` opt-out → use the
`container--wide`/`--narrow` modifier block; bare `:root{}` in a leaf template
(only base/theme files may); deprecated aliases (`.modal-btn`, `.users-table`,
`.btn-warning`).

## Steps

1. TDD: a route test asserting 200 + the page renders a key element; the
   design-system contract test will also run against the new template.
2. Create the template (extend `base_page.html`) + the route.
3. Green both.

## Anchors

- bases: `app/web/templates/base_ds.html:78`, `app/web/templates/base_page.html:33`
- real page: `app/web/templates/admin_users.html:1`
- route: `app/web/router.py:2409`
- contract: `tests/test_design_system_contract.py:397`
````

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_dev_agent_kit.py -k app_playbooks -v`
Expected: PASS (2 parametrized cases).

- [ ] **Step 6: Commit**

```bash
git add tests/test_dev_agent_kit.py .claude/skills/agnes-conventions/references/endpoint-rbac.md .claude/skills/agnes-conventions/references/web-page.md
git commit -m "feat(dev-kit): add endpoint-rbac/web-page playbooks"
```

---

## Task 4: Full-suite check + CHANGELOG

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Run the kit tests**

Run: `.venv/bin/pytest tests/test_dev_agent_kit.py -v`
Expected: PASS (all structural guards green).

- [ ] **Step 2: Run the full suite**

Run: `.venv/bin/pytest tests/ --tb=short -n auto -q`
Expected: baseline (no NEW failures; this slice adds only markdown + a test). The
known pre-existing/flaky cases (`test_install_page_uses_versioned_wheel_url`,
`test_server_info_in_initialize_response`) are unrelated — do not fix them.

- [ ] **Step 3: Add a CHANGELOG bullet**

Under `## [Unreleased]` → `### Added` in `CHANGELOG.md`:

```markdown
- Dev-agent kit (builder): an `agnes-builder` feature-implementer agent + an `agnes-conventions` skill with five code-verified playbooks (connector, endpoint+RBAC, web page, repo+parity, migration).
```

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): dev-agent kit builder + conventions"
```

---

## Self-review notes

- **Spec coverage (§8):** builder non-negotiables + output contract → Task 1
  agent; five playbooks → Tasks 2–3; thin-agent/fat-skill (knowledge in
  references) → skill structure; CHANGELOG → Task 4.
- **Accuracy:** every playbook's `file:line` anchors were verified against v0.66.1
  (connector discovery, factory `_REGISTRY` three-edit rule + `test_repo_method_parity`
  + `test_repository_registry`, migration dual-ladder + `db_pg.py` Base.metadata,
  RBAC gates, design-system bans).
- **No placeholders:** every artifact's full content is in the steps.
- **Type/name consistency:** test helpers (`CONVENTIONS`, `PLAYBOOKS`,
  `read_frontmatter`, `agent_names`) reused from earlier slices; `PLAYBOOKS` list
  matches the five reference filenames created in Tasks 2–3.
- **Out of scope (later slices):** router section + thin/fat refactor of the
  *existing* skills (D), build team (E).
```
