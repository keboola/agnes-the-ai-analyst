# Agent Profiles & Agent-as-API — V1a Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the V1a foundation of the agent-as-API design: `agents` entity + dual-backend repos/migrations, agent PATs, `POST /api/v1/agents/{slug}/responses` one-shot (sync + background), broker extensions (model policy, usage ledger, budget), `chat_sessions.agent_id`, default-agent seeding, minimal builder UI, and CLI parity.

**Spec:** `docs/superpowers/specs/2026-07-21-agent-profiles-and-agent-api-design.md` (revision 2). Read §1 (data model), §2 (API + auth matrix), §3 (broker), §4 (runtime) before starting. This plan implements **V1a only** — sessions/SSE/webhooks/artifacts are V1b; agent memories are V1c.

**Architecture:** New entity rides the standard dual-backend repo pattern (DuckDB + PG + factory + contract tests). The runtime reuses ChatManager (`Surface.API`, fresh session per one-shot, headless sink awaiting the `done` frame). LLM policy/budget enforcement lives ONLY in the existing secret broker. Background one-shots ride the existing jobs runtime.

**Tech Stack:** FastAPI, DuckDB + Postgres (Alembic), existing chat runtime (E2B sandbox), existing jobs worker, pytest.

## Global Constraints

- **Dual-backend discipline** (CLAUDE.md): every repo method lands in `src/repositories/X.py` AND `X_pg.py` in the same task; factory entry in `src/repositories/__init__.py`; contract test in `tests/db_pg/`; DuckDB migration `_v95_to_v96` must pair with Alembic `0043_*`; both ladders reach the same endpoint (`tests/test_db_schema_version.py` is the gate).
- **NEVER add secondary indexes** on `chat_sessions` columns or other hot-write tables (DuckDB ART incident 2026-07-20, see `_v94_to_v95` in `src/db.py`). New tables get primary keys only unless a column is read-heavy and never rewritten.
- **Never instantiate repos directly** — always through the factory functions (`tests/test_backend_split_guard.py` is a static ratchet).
- **Every new `/api/*` route** must carry a recognized auth dependency (`tests/test_route_auth_guard.py` sweeps all routes).
- **Vendor-agnostic**: no customer names/hosts in code, comments, tests, or commit messages.
- **CHANGELOG**: one `## [Unreleased]` bullet per user-visible change, in the same PR (Task 12).
- **Full suite before push**: `.venv/bin/pytest tests/ --tb=short -n auto -q`.
- Spec terminology: "agent PAT" = JWT with `typ="agent_pat"` + `agent_id` claim. Plain user PATs (`typ="pat"`) keep today's behavior everywhere.

---

### Task 1: Schema migration v96 (DuckDB) + Alembic 0043

**Files:**
- Modify: `src/db.py` (SCHEMA_VERSION 95→96, `_SYSTEM_SCHEMA` additions, new `_v95_to_v96`, register in the migration ladder dict/list near the other `_vN_to_vN+1` entries)
- Create: `migrations/versions/0043_agents_v96.py`
- Test: `tests/test_db_schema_version.py` (existing gate), `tests/db_pg/test_alembic_roundtrip.py` (existing)

**Interfaces:**
- Produces: tables `agents`, `agent_scope`, `llm_usage`, `agent_scope_snapshots`, `idempotency_keys`; columns `personal_access_tokens.agent_id VARCHAR NULL`, `chat_sessions.agent_id VARCHAR NULL`. All later tasks depend on these exact names.

- [ ] **Step 1: Write the failing test**

Append to the existing schema-version test module a shape assertion (both engines are covered by the existing roundtrip tests once the DDL lands; this test pins the DuckDB side):

```python
# tests/test_agents_schema.py
"""v96: agents / agent_scope / llm_usage / agent_scope_snapshots /
idempotency_keys tables + agent_id columns exist after _ensure_schema."""
from src.db import _ensure_schema
from src.duckdb_conn import _open_duckdb


def _cols(conn, table):
    return {r[0] for r in conn.execute(f"PRAGMA table_info('{table}')").fetchall()}


def test_v96_tables_and_columns(tmp_path):
    conn = _open_duckdb(str(tmp_path / "d.duckdb"))
    _ensure_schema(conn)
    assert {"id", "owner_user_id", "name", "slug", "system_prompt", "model",
            "token_budget_monthly", "plugins_mode", "connections_mode",
            "tables_mode", "memory_mode", "memory_write_mode", "is_default",
            "created_at", "updated_at", "deleted_at"} <= _cols(conn, "agents")
    assert {"agent_id", "item_type", "item_id"} <= _cols(conn, "agent_scope")
    assert {"id", "agent_id", "user_id", "session_id", "model", "input_tokens",
            "output_tokens", "cache_read_tokens", "cache_creation_tokens",
            "created_at"} <= _cols(conn, "llm_usage")
    assert {"id", "session_id", "agent_id", "effective_scope", "created_at"} \
        <= _cols(conn, "agent_scope_snapshots")
    assert {"key", "owner_user_id", "agent_id", "request_hash", "response_body",
            "status_code", "created_at", "expires_at"} <= _cols(conn, "idempotency_keys")
    assert "agent_id" in _cols(conn, "personal_access_tokens")
    assert "agent_id" in _cols(conn, "chat_sessions")
    conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_agents_schema.py -q`
Expected: FAIL (tables missing).

- [ ] **Step 3: Implement DuckDB side**

In `src/db.py`: bump `SCHEMA_VERSION = 96`. Add to `_SYSTEM_SCHEMA` (fresh-install path) AND create `_v95_to_v96` (upgrade path) with the same DDL — follow the `_v57_to_v58` idempotent style:

```python
def _v95_to_v96(conn: duckdb.DuckDBPyConnection) -> None:
    """v96: agent profiles + agent-as-API foundation (spec
    docs/superpowers/specs/2026-07-21-agent-profiles-and-agent-api-design.md).
    No secondary indexes anywhere here — see the _v94_to_v95 ART-index
    incident note; chat_sessions.agent_id especially must stay unindexed."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agents (
            id                   VARCHAR PRIMARY KEY,
            owner_user_id        VARCHAR NOT NULL,
            name                 VARCHAR NOT NULL,
            slug                 VARCHAR NOT NULL,
            description          TEXT,
            system_prompt        TEXT,
            model                VARCHAR,
            token_budget_monthly BIGINT,
            plugins_mode         VARCHAR NOT NULL DEFAULT 'all',
            connections_mode     VARCHAR NOT NULL DEFAULT 'all',
            tables_mode          VARCHAR NOT NULL DEFAULT 'all',
            memory_mode          VARCHAR NOT NULL DEFAULT 'all',
            memory_write_mode    VARCHAR NOT NULL DEFAULT 'propose',
            is_default           BOOLEAN NOT NULL DEFAULT FALSE,
            created_at           TIMESTAMP DEFAULT current_timestamp,
            updated_at           TIMESTAMP DEFAULT current_timestamp,
            deleted_at           TIMESTAMP,
            UNIQUE (owner_user_id, slug)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_scope (
            agent_id  VARCHAR NOT NULL,
            item_type VARCHAR NOT NULL,
            item_id   VARCHAR NOT NULL,
            PRIMARY KEY (agent_id, item_type, item_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS llm_usage (
            id                    VARCHAR PRIMARY KEY,
            agent_id              VARCHAR,
            user_id               VARCHAR,
            session_id            VARCHAR,
            model                 VARCHAR,
            input_tokens          BIGINT DEFAULT 0,
            output_tokens         BIGINT DEFAULT 0,
            cache_read_tokens     BIGINT DEFAULT 0,
            cache_creation_tokens BIGINT DEFAULT 0,
            created_at            TIMESTAMP DEFAULT current_timestamp
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_scope_snapshots (
            id              VARCHAR PRIMARY KEY,
            session_id      VARCHAR NOT NULL,
            agent_id        VARCHAR NOT NULL,
            effective_scope TEXT NOT NULL,
            created_at      TIMESTAMP DEFAULT current_timestamp
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS idempotency_keys (
            key           VARCHAR NOT NULL,
            owner_user_id VARCHAR NOT NULL,
            agent_id      VARCHAR NOT NULL,
            request_hash  VARCHAR NOT NULL,
            response_body TEXT,
            status_code   INTEGER,
            created_at    TIMESTAMP DEFAULT current_timestamp,
            expires_at    TIMESTAMP,
            PRIMARY KEY (key, owner_user_id, agent_id)
        )
    """)
    conn.execute("ALTER TABLE personal_access_tokens ADD COLUMN IF NOT EXISTS agent_id VARCHAR")
    conn.execute("ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS agent_id VARCHAR")
    conn.execute("UPDATE schema_version SET version = 96")
```

Mirror the same DDL into `_SYSTEM_SCHEMA` (fresh installs) and register `_v95_to_v96` wherever `_v94_to_v95` is registered (search `src/db.py` for `_v94_to_v95` — there is a ladder mapping near the migration runner).

- [ ] **Step 4: Implement Alembic side**

`migrations/versions/0043_agents_v96.py`, modeled on `0041_jobs_v94.py` (use `sa.Column` defs mirroring the DuckDB DDL exactly; `down_revision = "0042_usage_summary_idx_fix_v95"`). `upgrade()` creates the five tables + `op.add_column("personal_access_tokens", sa.Column("agent_id", sa.String(), nullable=True))` + same for `chat_sessions`. `downgrade()` drops in reverse. No `op.create_index` calls.

- [ ] **Step 5: Run tests**

Run: `.venv/bin/pytest tests/test_agents_schema.py tests/test_db_schema_version.py tests/db_pg/test_alembic_roundtrip.py tests/db_pg/test_alembic_skeleton.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/db.py migrations/versions/0043_agents_v96.py tests/test_agents_schema.py
git commit -m "feat(db): v96 schema — agents, agent_scope, llm_usage, scope snapshots, idempotency keys"
```

---

### Task 2: AgentsRepository (DuckDB + PG) + factory + contract test

**Files:**
- Create: `src/repositories/agents.py`, `src/repositories/agents_pg.py`
- Modify: `src/repositories/__init__.py` (dispatch entry `"agents"` + `def agents_repo()`)
- Test: `tests/db_pg/test_agents_contract.py`

**Interfaces:**
- Produces (both backends, identical signatures):
  - `create(id, owner_user_id, name, slug, description=None, system_prompt=None, model=None, token_budget_monthly=None, plugins_mode="all", connections_mode="all", tables_mode="all", memory_mode="all", memory_write_mode="propose", is_default=False) -> None`
  - `get_by_id(agent_id) -> Optional[dict]` (includes soft-deleted rows so slug tombstoning works)
  - `get_by_slug(owner_user_id, slug) -> Optional[dict]` (only `deleted_at IS NULL`)
  - `list_for_user(owner_user_id) -> list[dict]` (only live rows, default first, then name)
  - `update(agent_id, **fields) -> None` — whitelist: `name, description, system_prompt, model, token_budget_monthly, plugins_mode, connections_mode, tables_mode, memory_mode, memory_write_mode`; always sets `updated_at`
  - `soft_delete(agent_id) -> None` (row kept → slug tombstone; UNIQUE(owner,slug) then blocks reuse by design)
  - `get_or_create_default(owner_user_id) -> dict` (idempotent; name "Default", slug "default", all modes `'all'`, `is_default=True`)
  - `set_scope(agent_id, items: list[tuple[str, str]]) -> None` (replace-all: DELETE then INSERT)
  - `get_scope(agent_id) -> list[dict]` (`[{item_type, item_id}]`)
  - `record_scope_snapshot(id, session_id, agent_id, effective_scope: str) -> None`
  - `list_scope_snapshots(session_id) -> list[dict]`

- [ ] **Step 1: Write the failing contract test**

Follow `tests/db_pg/test_glossary_contract.py` verbatim for the `_make_duckdb_repo` / `_make_pg_repo` / parametrized `repo` fixture scaffolding (swap in `AgentsRepository` / `AgentsPgRepository`). Cases:

```python
def test_create_get_roundtrip(repo):
    repo.create(id="a1", owner_user_id="u1", name="Sales reporter", slug="sales-reporter")
    row = repo.get_by_slug("u1", "sales-reporter")
    assert row["id"] == "a1" and row["plugins_mode"] == "all"
    assert row["memory_write_mode"] == "propose"

def test_slug_unique_per_owner(repo):
    repo.create(id="a1", owner_user_id="u1", name="A", slug="x")
    with pytest.raises(Exception):
        repo.create(id="a2", owner_user_id="u1", name="B", slug="x")
    repo.create(id="a3", owner_user_id="u2", name="C", slug="x")  # other owner OK

def test_soft_delete_tombstones_slug(repo):
    repo.create(id="a1", owner_user_id="u1", name="A", slug="x")
    repo.soft_delete("a1")
    assert repo.get_by_slug("u1", "x") is None          # invisible to runtime
    assert repo.get_by_id("a1")["deleted_at"] is not None
    with pytest.raises(Exception):                       # slug never reused
        repo.create(id="a2", owner_user_id="u1", name="B", slug="x")

def test_get_or_create_default_idempotent(repo):
    d1 = repo.get_or_create_default("u1")
    d2 = repo.get_or_create_default("u1")
    assert d1["id"] == d2["id"] and d1["is_default"] is True
    assert len(repo.list_for_user("u1")) == 1

def test_scope_replace_all(repo):
    repo.create(id="a1", owner_user_id="u1", name="A", slug="x")
    repo.set_scope("a1", [("plugin", "p1"), ("table", "t1")])
    repo.set_scope("a1", [("plugin", "p2")])
    assert repo.get_scope("a1") == [{"item_type": "plugin", "item_id": "p2"}]

def test_update_whitelist(repo):
    repo.create(id="a1", owner_user_id="u1", name="A", slug="x")
    repo.update("a1", name="B", model="claude-sonnet-5", plugins_mode="selected")
    row = repo.get_by_id("a1")
    assert row["name"] == "B" and row["plugins_mode"] == "selected"
    with pytest.raises(ValueError):
        repo.update("a1", owner_user_id="u2")   # not whitelisted

def test_scope_snapshot_roundtrip(repo):
    repo.create(id="a1", owner_user_id="u1", name="A", slug="x")
    repo.record_scope_snapshot(id="s1", session_id="c1", agent_id="a1",
                               effective_scope='{"tables": ["t1"]}')
    snaps = repo.list_scope_snapshots("c1")
    assert len(snaps) == 1 and snaps[0]["effective_scope"] == '{"tables": ["t1"]}'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/db_pg/test_agents_contract.py -q`
Expected: FAIL (import error — module doesn't exist).

- [ ] **Step 3: Implement DuckDB repo**

`src/repositories/agents.py`, same style as `src/repositories/access_tokens.py` (conn in ctor, `_row_to_dict`). `update` builds `SET` from a `_UPDATABLE = frozenset({...})` whitelist and raises `ValueError` on anything else. `get_or_create_default` = SELECT where `is_default AND deleted_at IS NULL`, else INSERT with `id=str(uuid.uuid4())`, slug `"default"`, then re-SELECT. `set_scope` = `DELETE FROM agent_scope WHERE agent_id=?` + executemany INSERT.

- [ ] **Step 4: Implement PG repo + factory entry**

`src/repositories/agents_pg.py` mirrors signatures over SQLAlchemy engine (copy the pattern from `src/repositories/memory_domains_pg.py`). In `src/repositories/__init__.py` add to the dispatch table:

```python
"agents": {
    DUCKDB: ("src.repositories.agents", "AgentsRepository"),
    PG: ("src.repositories.agents_pg", "AgentsPgRepository"),
},
```

and a builder next to the other builders:

```python
def agents_repo() -> Any:
    return _build("agents")
```

Export `"agents_repo"` in `__all__`.

- [ ] **Step 5: Run tests, then guards**

Run: `.venv/bin/pytest tests/db_pg/test_agents_contract.py tests/test_backend_split_guard.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/repositories/agents.py src/repositories/agents_pg.py src/repositories/__init__.py tests/db_pg/test_agents_contract.py
git commit -m "feat(repos): agents repository (dual-backend) with scope + snapshots"
```

---

### Task 3: LlmUsageRepository (dual-backend)

**Files:**
- Create: `src/repositories/llm_usage.py`, `src/repositories/llm_usage_pg.py`
- Modify: `src/repositories/__init__.py`
- Test: `tests/db_pg/test_llm_usage_contract.py`

**Interfaces:**
- Produces:
  - `insert_batch(rows: list[dict]) -> None` — each row: `{id, agent_id, user_id, session_id, model, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens}` (created_at defaults)
  - `month_total_tokens(agent_id: str, year_month: str) -> int` — `year_month` like `"2026-07"`; returns SUM(input+output+cache_creation) (cache reads are free-tier accounting, excluded)
  - `list_for_agent(agent_id, limit=100) -> list[dict]`

- [ ] **Step 1: Write the failing contract test** (same fixture scaffolding as Task 2):

```python
def test_batch_and_month_total(repo):
    repo.insert_batch([
        {"id": "r1", "agent_id": "a1", "user_id": "u1", "session_id": "c1",
         "model": "claude-sonnet-5", "input_tokens": 100, "output_tokens": 50,
         "cache_read_tokens": 10, "cache_creation_tokens": 5},
        {"id": "r2", "agent_id": "a1", "user_id": "u1", "session_id": "c1",
         "model": "claude-haiku-4-5-20251001", "input_tokens": 10, "output_tokens": 5,
         "cache_read_tokens": 0, "cache_creation_tokens": 0},
    ])
    from datetime import datetime, timezone
    ym = datetime.now(timezone.utc).strftime("%Y-%m")
    assert repo.month_total_tokens("a1", ym) == 100 + 50 + 5 + 10 + 5
    assert repo.month_total_tokens("a2", ym) == 0
    assert len(repo.list_for_agent("a1")) == 2

def test_empty_batch_noop(repo):
    repo.insert_batch([])
```

- [ ] **Step 2: Run to fail** — `.venv/bin/pytest tests/db_pg/test_llm_usage_contract.py -q` → FAIL.

- [ ] **Step 3: Implement both backends + factory** (`llm_usage_repo()`; month filter: `WHERE agent_id=? AND strftime(created_at, '%Y-%m') = ?` on DuckDB / `to_char(created_at, 'YYYY-MM') = :ym` on PG).

- [ ] **Step 4: Run to pass** — same command → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/repositories/llm_usage.py src/repositories/llm_usage_pg.py src/repositories/__init__.py tests/db_pg/test_llm_usage_contract.py
git commit -m "feat(repos): llm_usage ledger repository (dual-backend)"
```

---

### Task 4: Agent PAT — minting + resolver enforcement

**Files:**
- Modify: `src/repositories/access_tokens.py` + `access_tokens_pg.py` (`create(..., agent_id=None)`, `list_for_agent(agent_id)`, `revoke_for_agent(agent_id)`)
- Modify: `app/auth/pat_resolver.py` (reject `typ="agent_pat"` off-surface)
- Modify: `app/auth/dependencies.py` (map the new reason to a 401/403 detail)
- Test: `tests/test_agent_pat.py`, extend `tests/db_pg/test_*` only if an access-token contract test already exists (check first; if none exists, the repo change is covered by `tests/test_agent_pat.py` through the API)

**Interfaces:**
- Consumes: `agents_repo()` (Task 2), `create_access_token(...)` from `app/auth/jwt.py` (existing — supports `typ` + `extra_claims`).
- Produces: JWTs with `typ="agent_pat"`, claims `{agent_id: <id>}`; resolver reason `"agent_pat_wrong_surface"`; helper `agent_id_from_request(request) -> Optional[str]` in `app/auth/pat_resolver.py` for Task 8/9 to read the authenticated agent binding.

Allowed path prefixes for agent PATs (module constant in `pat_resolver.py`):

```python
_AGENT_PAT_ALLOWED_PREFIXES = ("/api/v1/agents/", "/api/v1/sessions/", "/api/v1/jobs/")
```

Everything else — including `/api/v1/agents` management verbs (those use session auth anyway), `/git/`, `/marketplace.zip`, all legacy `/api/*` — rejects with `agent_pat_wrong_surface`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_pat.py
"""Agent PATs: typ=agent_pat + agent_id claim; hard-rejected off-surface."""


def _mint_agent_pat(user, agent_id):
    from app.auth.jwt import create_access_token
    return create_access_token(
        user_id=user["id"], email=user["email"], token_id="tok-1",
        typ="agent_pat", extra_claims={"agent_id": agent_id},
    )


def test_agent_pat_rejected_on_legacy_api(client, seeded_user, seeded_agent):
    token = _mint_agent_pat(seeded_user, seeded_agent["id"])
    r = client.get("/api/catalog", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401

def test_agent_pat_rejected_on_marketplace_zip(client, seeded_user, seeded_agent):
    token = _mint_agent_pat(seeded_user, seeded_agent["id"])
    r = client.get("/marketplace.zip", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (401, 403)

def test_user_pat_unaffected(client, seeded_user_pat):
    r = client.get("/api/catalog", headers={"Authorization": f"Bearer {seeded_user_pat}"})
    assert r.status_code == 200
```

(`client` / seeded fixtures: reuse the app-level TestClient fixture used by `tests/test_route_auth_guard.py`'s neighborhood — check `tests/conftest.py` for the canonical app fixture and follow it; `seeded_agent` creates a row via `agents_repo()`.)

- [ ] **Step 2: Run to fail** — `.venv/bin/pytest tests/test_agent_pat.py -q` → FAIL (agent_pat currently resolves like any valid JWT or is rejected for the wrong reason — assert the right reason surfaces).

- [ ] **Step 3: Implement resolver enforcement**

In `app/auth/pat_resolver.py::resolve_token_to_user`, right after `verify_token` yields the payload and before DB checks, add:

```python
if payload.get("typ") == "agent_pat":
    path = request.url.path if request is not None else ""
    if not path.startswith(_AGENT_PAT_ALLOWED_PREFIXES):
        return None, "agent_pat_wrong_surface"
```

Add `"agent_pat_wrong_surface"` to `ResolutionReason`. Agent PATs then continue through the SAME DB-row validity chain as `typ="pat"` (they live in `personal_access_tokens` with `agent_id` set). Add module helper:

```python
def agent_id_from_request(request) -> Optional[str]:
    """agent_id claim of the presented agent PAT, or None for other creds."""
    payload = getattr(request.state, "token_payload", None)
    return payload.get("agent_id") if payload and payload.get("typ") == "agent_pat" else None
```

(Stash the verified payload on `request.state.token_payload` inside `resolve_token_to_user` — one line — so this helper and Task 9 don't re-verify.)

In `app/auth/dependencies.py::get_current_user`, map the new reason to `HTTPException(401, "Agent token not valid on this surface")`.

- [ ] **Step 4: Extend access-token repos** — `create(..., agent_id: Optional[str] = None)` (add the column to the INSERT in both backends), plus `revoke_for_agent(agent_id)` (`UPDATE ... SET revoked_at = now WHERE agent_id = ?`). Keep existing callers source-compatible (keyword-only with default).

- [ ] **Step 5: Run to pass** — `.venv/bin/pytest tests/test_agent_pat.py -q` → PASS. Also run `.venv/bin/pytest tests/ -k "token" -q` for regressions.

- [ ] **Step 6: Commit**

```bash
git add app/auth/pat_resolver.py app/auth/dependencies.py src/repositories/access_tokens.py src/repositories/access_tokens_pg.py tests/test_agent_pat.py
git commit -m "feat(auth): agent PATs — typ=agent_pat, surface allowlist, repo agent_id"
```

---

### Task 5: Management API `/api/v1/agents` (CRUD + scope + token issuance)

**Files:**
- Create: `app/api/agents_admin.py` (router `prefix="/api/v1/agents"`)
- Modify: `app/main.py` (register router — follow how `app/api/tokens.py`'s router is registered)
- Test: `tests/test_agents_management_api.py`

**Interfaces:**
- Consumes: `agents_repo()`, `access_token_repo()`, `require_session_token` from `app/auth/dependencies.py` (existing — already rejects all PATs), `audit_repo()`.
- Produces endpoints (all `Depends(require_session_token)`):
  - `GET    /api/v1/agents` → `{"data": [...], "has_more": false, "next_cursor": null}` (cursor envelope from day one, spec §2)
  - `POST   /api/v1/agents` `{name, slug, description?, system_prompt?, model?, token_budget_monthly?}` → 201; **API-created agents default all four modes to `'selected'`** (spec §1)
  - `GET/PUT/DELETE /api/v1/agents/{id}` — PUT whitelist mirrors repo; slug is IMMUTABLE (400 `slug_immutable` if present in PUT body); DELETE = soft-delete + `access_token_repo().revoke_for_agent(id)`; deleting `is_default` → 400 `default_agent_undeletable`
  - `PUT    /api/v1/agents/{id}/scope` `{items: [{item_type, item_id}]}` — validates `item_type ∈ {plugin, connection, table, memory_domain}`
  - `POST   /api/v1/agents/{id}/tokens` `{name, expires_in_days?}` → mints agent PAT. **403 `agent_not_selected_mode`** unless all four `*_mode == 'selected'` (spec §2: never for `'all'`-mode agents). JWT via `create_access_token(..., typ="agent_pat", extra_claims={"agent_id": id})`; DB row via `access_token_repo().create(..., agent_id=id)` — mirror the hashing/prefix logic of `app/api/tokens.py::create_token` exactly (jti prefix, sha256 hash, secret returned once).
  - Ownership: every `{id}` route 404s unless `row["owner_user_id"] == user["id"]` or the caller is admin (admin read-only: GET allowed, mutations + token minting 403 for non-owned agents — spec auth matrix).

- [ ] **Step 1: Write the failing tests** — TestClient suite covering: create defaults to selected-modes; slug immutability 400; default-agent delete 400; token minting 403 on `'all'`-mode agent and 200 on selected-mode; cross-user 404; admin can GET foreign agent but not POST tokens for it; delete revokes the agent's PATs (mint → delete agent → PAT rejected by resolver DB check).

```python
def test_create_defaults_selected(mgmt_client):
    r = mgmt_client.post("/api/v1/agents", json={"name": "Sales", "slug": "sales"})
    assert r.status_code == 201
    body = r.json()
    assert body["plugins_mode"] == "selected" and body["tables_mode"] == "selected"

def test_token_requires_selected_modes(mgmt_client, default_agent_id, selected_agent_id):
    r = mgmt_client.post(f"/api/v1/agents/{default_agent_id}/tokens", json={"name": "t"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "agent_not_selected_mode"
    r = mgmt_client.post(f"/api/v1/agents/{selected_agent_id}/tokens", json={"name": "t"})
    assert r.status_code == 200 and r.json()["token"].startswith("eyJ")
```

- [ ] **Step 2: Run to fail** — `.venv/bin/pytest tests/test_agents_management_api.py -q` → FAIL (404 route not found).

- [ ] **Step 3: Implement router + register in `app/main.py`.** Error bodies: `{"detail": {"code": "<machine_code>", "message": "..."}}`. Audit each mutation (`agent.create`, `agent.delete`, `agent.token.create`) via `audit_repo()` following `app/api/tokens.py::_audit`.

- [ ] **Step 4: Run to pass**, then the route-auth guard: `.venv/bin/pytest tests/test_agents_management_api.py tests/test_route_auth_guard.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add app/api/agents_admin.py app/main.py tests/test_agents_management_api.py
git commit -m "feat(api): /api/v1/agents management — CRUD, scope, selected-only agent PATs"
```

---

### Task 6: `chat_sessions.agent_id` threading + `Surface.API` + default-agent web spawn

**Files:**
- Modify: `app/chat/types.py` (`Surface.API = "api"`; `ChatSession.agent_id: Optional[str] = None`)
- Modify: `app/chat/persistence.py` (+ the PG sibling `src/repositories/chat_sessions_pg.py` — locate by `grep -rn "class.*ChatSession.*Pg\|chat_sessions" src/repositories/`) — `create_session(..., agent_id=None)` persists the column; row→dataclass hydration includes it
- Modify: `app/chat/manager.py::create_session` — accept `agent_id: Optional[str] = None`, pass to repo; skip the WEB-only `archive_empty_user_sessions` branch for `Surface.API` (it already runs only `if surface == Surface.WEB`, so just verify)
- Modify: `app/api/chat.py` session-create route — resolve `agents_repo().get_or_create_default(user_id)` and pass its id, so every web session is attributed to the default agent (behavior otherwise unchanged)
- Test: `tests/test_chat_agent_binding.py` + extend the existing chat-session parity/contract coverage (`tests/db_pg/test_chat_pg.py`)

**Interfaces:**
- Consumes: `agents_repo().get_or_create_default` (Task 2).
- Produces: every `ChatSession` row carries `agent_id`; `Surface.API` exists. Tasks 7–9 rely on `session.agent_id` and on `manager.create_session(..., surface=Surface.API, agent_id=...)`.

- [ ] **Step 1: Failing test** — repo-level roundtrip (create session with `agent_id="a1"`, read back; both backends via the existing chat-PG test scaffolding) + API-level: `POST` the web session-create endpoint, assert the persisted row's `agent_id` equals the caller's default agent id.

- [ ] **Step 2: Run to fail.** `.venv/bin/pytest tests/test_chat_agent_binding.py -q`

- [ ] **Step 3: Implement** (dataclass field + both repos' INSERT/SELECT + manager kwarg + web route wiring). **No index on the new column** (see Global Constraints).

- [ ] **Step 4: Run to pass** + `.venv/bin/pytest tests/db_pg/test_chat_pg.py -q`.

- [ ] **Step 5: Commit**

```bash
git add app/chat/types.py app/chat/persistence.py src/repositories/chat_sessions_pg.py app/chat/manager.py app/api/chat.py tests/test_chat_agent_binding.py
git commit -m "feat(chat): thread agent_id through chat sessions; Surface.API; default-agent web attribution"
```

---

### Task 7: Spawn-time agent profile (persona CLAUDE.md) + scope snapshot

**Files:**
- Create: `app/chat/agent_profile.py`
- Modify: `app/chat/manager.py` (where `_spawn_live` materializes profiles — search `_materialize_profile` / `WorkdirManager.prepare_session_dir` call sites) — when the session has `agent_id` whose agent has a non-empty `system_prompt`, build a dynamic `ChatProfile` from the DB row and pass it down the existing profile path; record a scope snapshot after spawn
- Test: `tests/test_agent_profile_spawn.py`

**Interfaces:**
- Consumes: `ChatProfile` dataclass (`app/chat/profiles.py` — frozen: `slug, claude_md, skill_name, skill_body`), `agents_repo()`.
- Produces:
  - `agent_profile.build_profile(agent_row: dict) -> Optional[ChatProfile]` — `None` when `system_prompt` is empty (default agent → today's generic rails, bit-for-bit); else `ChatProfile(slug=f"agent-{agent_row['slug']}", claude_md=agent_row["system_prompt"], skill_name="agnes-agent-context", skill_body=_context_skill(agent_row))` where `_context_skill` renders a small read-only SKILL.md stating the agent's name/description and that capability is scoped by the owner's config.
  - `agent_profile.compute_effective_scope(agent_row: dict) -> dict` — `{"plugins": [...]|"all", "connections": [...]|"all", "tables": [...]|"all", "memory_domains": [...]|"all"}` from `agents_repo().get_scope` + modes. V1a records it for audit (snapshot row via `agents_repo().record_scope_snapshot(id=uuid4, session_id, agent_id, effective_scope=json.dumps(...))`); live seam enforcement of plugin/connection subsetting is V1b — document that in the module docstring so nobody mistakes the snapshot for enforcement.

- [ ] **Step 1: Failing tests** — `build_profile` returns None for empty prompt; returns ChatProfile with `claude_md == system_prompt` otherwise; `compute_effective_scope` maps `'all'` modes to `"all"` and `'selected'` to the enumerated ids; spawn integration test (with the FAKE_AGENT harness if a lightweight spawn test exists — check `tests/` for existing `_spawn`/profile tests and imitate; otherwise unit-test the seam function the manager calls) asserts one `agent_scope_snapshots` row lands on session spawn.

- [ ] **Step 2–4: fail → implement → pass.** `.venv/bin/pytest tests/test_agent_profile_spawn.py -q`

- [ ] **Step 5: Commit**

```bash
git add app/chat/agent_profile.py app/chat/manager.py tests/test_agent_profile_spawn.py
git commit -m "feat(chat): DB-sourced agent persona profile + spawn scope snapshot"
```

---

### Task 8: Broker — model policy, usage ledger, budget enforcement

**Files:**
- Create: `app/api/broker_agent_policy.py` (pure logic: parse body model, parse usage, budget math — unit-testable without HTTP)
- Modify: `app/api/broker.py::anthropic_proxy` (three hook points: pre-forward policy+budget, post-response usage recording)
- Modify: `app/chat/config.py` (new config knobs: `agent_api_utility_models: list[str]` default `[]` = allow only agent/instance-default model; `agent_api_budget_cache_ttl_s: int = 60`)
- Test: `tests/test_broker_agent_policy.py`

**Interfaces:**
- Consumes: ticket row (`row["session_id"]`), `chat_session_repo().get_session(session_id)` → `.agent_id` (Task 6), `agents_repo()`, `llm_usage_repo()`, `coordination()` from `app.coordination.factory`.
- Produces (in `broker_agent_policy.py`):
  - `check_model(body_bytes: bytes, agent_row: dict, utility_models: list[str], instance_default: str) -> Optional[str]` — returns error code `"model_not_allowed"` or None. Allowed set = `{agent.model or instance_default} ∪ utility_models`. Non-JSON body or missing `model` → None (not a policy failure; upstream will reject).
  - `parse_usage(resp_body: bytes, content_type: str) -> Optional[dict]` — handles plain JSON (`{"usage": {...}}`) AND buffered SSE (scan `message_start` / `message_delta` events, sum `usage` fields). Returns `{model, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens}` or None.
  - `class UsageAccumulator` — `add(row: dict)`, flushes to `llm_usage_repo().insert_batch` when ≥20 rows or ≥30 s since last flush (call `maybe_flush()` after each add; `flush()` on app shutdown hook). Module-level singleton `usage_accumulator`. **Never one synchronous write per LLM call** (spec §3 hot-path discipline).
  - `check_budget(agent_row: dict, month_total: int) -> Optional[str]` — `"budget_exhausted"` when `token_budget_monthly` is set and `month_total >= token_budget_monthly`, else None.
  - Budget month-total caching: coordination key `agent-budget:{agent_id}:{YYYYMM}`; on cache miss read `llm_usage_repo().month_total_tokens` and `kv_set` with TTL `agent_api_budget_cache_ttl_s`; `UsageAccumulator.add` also `incr`s the key (best-effort, `CoordinationUnavailable` → log + skip, same pattern as `ChatManager._daily_token_totals`).

Wiring in `anthropic_proxy` (only on `POST` + `upstream_path == "/v1/messages"`):

```python
agent_row = _agent_for_ticket(row)           # session → agent_id → agents_repo(); None-safe
if agent_row is not None:
    err = check_model(raw_body, agent_row, cfg.agent_api_utility_models, cfg.model_default)
    if err:
        raise HTTPException(status_code=403, detail={"code": err})
    err = check_budget(agent_row, _cached_month_total(agent_row["id"]))
    if err:
        # NO retry-after header — SDKs must not auto-retry (spec §3)
        raise HTTPException(status_code=429, detail={"code": err})
# ... existing forward ...
if agent_row is not None and resp.status_code == 200:
    usage = parse_usage(resp.content, resp.headers.get("content-type", ""))
    if usage:
        usage_accumulator.add({**usage, "id": str(uuid.uuid4()),
                               "agent_id": agent_row["id"],
                               "user_id": agent_row["owner_user_id"],
                               "session_id": row["session_id"]})
```

Every response (allowed or not) gains headers `x-agnes-budget-limit` / `x-agnes-budget-used` when the agent has a budget. Enforcement applies to ALL upstream modes (static key / WIF / dispatcher) — the hooks sit before/after the mode fork, which the current function structure already allows (policy before credential injection, usage after `client.request`).

- [ ] **Step 1: Failing unit tests** for `check_model` (agent model allowed, utility allowed, foreign model rejected, malformed body passes), `parse_usage` (JSON body; SSE body with `message_start` + two `message_delta`s; garbage → None), `check_budget` (under/at/over; no budget → None), accumulator flush thresholds (monkeypatch repo, assert batch size/timing).

- [ ] **Step 2: Run to fail.** `.venv/bin/pytest tests/test_broker_agent_policy.py -q`

- [ ] **Step 3: Implement `broker_agent_policy.py`**, then wire `anthropic_proxy` (keep the wiring diff minimal — helpers live in the new module; `_agent_for_ticket` caches per-request only).

- [ ] **Step 4: Run to pass** + broker regression: `.venv/bin/pytest tests/test_broker_agent_policy.py tests/ -k "broker" -q`

- [ ] **Step 5: Commit**

```bash
git add app/api/broker_agent_policy.py app/api/broker.py app/chat/config.py tests/test_broker_agent_policy.py
git commit -m "feat(broker): per-agent model policy, batched llm_usage ledger, monthly budget"
```

---

### Task 9: `POST /api/v1/agents/{slug}/responses` — one-shot (sync + background + idempotency)

**Files:**
- Create: `app/api/agent_runtime.py` (router), `app/chat/headless.py` (HeadlessSink + run-to-completion helper), `src/repositories/idempotency.py` + `idempotency_pg.py` (+ factory entry `idempotency_repo()`)
- Modify: `app/main.py` (register router)
- Test: `tests/test_agent_responses_api.py`, `tests/db_pg/test_idempotency_contract.py`

**Interfaces:**
- Consumes: `manager.create_session(user_email, surface=Surface.API, agent_id=...)` (Task 6), `manager.attach(chat_id, sink)` + `manager.send_user_message(chat_id, text)` (existing), frame type `"done"` (see `app/chat/manager.py:1640`), `jobs_repo()` + the jobs worker registry (`app/worker/` — register kind `"agent_response"` following an existing worker registration), `agent_id_from_request` (Task 4), `agents_repo()`, `llm_usage_repo()`.
- Produces:
  - `POST /api/v1/agents/{slug}/responses` `{input: str, background?: bool, timeout_s?: int = 120, metadata?: dict}` →
    `200 {"answer", "session_id", "response_id", "usage", "agent_config_hash", "request_id"}` | `202 {"job_id"}`
  - `GET /api/v1/jobs/{id}` (public mapping of the existing jobs row: `queued→queued, running→in_progress, done→completed, failed→failed`)
  - Auth dependency `require_agent_runtime_principal` (in `agent_runtime.py`): resolves user via `get_current_user`; loads agent by slug for that owner (404 if none); if the credential is an agent PAT, 403 unless `agent_id_from_request(request) == agent.id`. Requires the existing `ResourceType.CHAT` grant — reuse the same check the chat WS route uses (`require_chat_access` / `can_access` in `app/auth/access.py`; copy the call, not the mechanism).
  - `app/chat/headless.py::run_one_shot(manager, *, user_email, agent_id, prompt, timeout_s) -> dict` — creates a fresh session, attaches a `HeadlessSink`, sends the prompt, awaits the sink's `done_event` with `asyncio.wait_for(timeout=timeout_s)`, returns `{"chat_id", "answer", "timed_out": bool}` where `answer` = the last `assistant_message` frame's text collected by the sink.

```python
# app/chat/headless.py (core shape)
class HeadlessSink:
    """Duck-typed frame sink (send_json/close) collecting a one-shot run."""
    def __init__(self) -> None:
        self.frames: list[dict] = []
        self.done_event = asyncio.Event()
        self.answer: str = ""
    async def send_json(self, frame: dict) -> None:
        self.frames.append(frame)
        if frame.get("type") == "assistant_message":
            self.answer = frame.get("text") or frame.get("content") or self.answer
        if frame.get("type") == "done":
            self.done_event.set()
    async def close(self) -> None:
        self.done_event.set()
```

(Verify the exact frame field names against `app/chat/manager.py`'s frame emission — `grep -n "assistant_message" app/chat/manager.py` — before finalizing; adjust `answer` extraction to the real shape.)

  - Sync path: `run_one_shot(...)`; on `timed_out` → enqueue an `agent_response` job that continues waiting on the SAME chat_id and store its result; respond `202 {"job_id"}` (spec: timeout bounds the wait, not the run).
  - Background path (`background: true`): enqueue immediately, worker calls `run_one_shot` with a long timeout, writes `{answer, session_id, usage}` into the job result.
  - `agent_config_hash` = `hashlib.sha256(json.dumps(effective_scope_sorted + relevant agent fields).encode()).hexdigest()[:16]` using `compute_effective_scope` (Task 7).
  - `usage` = sum of `llm_usage_repo().list_for_agent` rows for this `session_id` (flush the accumulator first: expose `usage_accumulator.flush()`).
  - Idempotency repo: `get(key, owner_user_id, agent_id) -> Optional[dict]`, `put(key, owner_user_id, agent_id, request_hash, response_body, status_code, ttl_s) -> None`, `purge_expired() -> int`. Handler: header present → look up; hit + same `request_hash` (sha256 of raw body) → replay stored body/status; hit + different hash → `409 {"code": "idempotency_key_reuse"}`; miss → run, then `put`.

- [ ] **Step 1: Failing tests** — contract test for the idempotency repo (roundtrip, hash mismatch visible, expiry); API tests with the chat manager faked at the `run_one_shot` seam (monkeypatch `app.api.agent_runtime.run_one_shot` to return a canned answer): 200 happy path returns answer+hash+request_id; agent-PAT for a different agent → 403; unknown slug → 404; `background: true` → 202 + job completes via worker (invoke the worker function directly); Idempotency-Key replay → identical body second time without invoking `run_one_shot` again (assert call count).

- [ ] **Step 2: Run to fail.** `.venv/bin/pytest tests/test_agent_responses_api.py tests/db_pg/test_idempotency_contract.py -q`

- [ ] **Step 3: Implement** (idempotency repos → headless → router → jobs worker registration → main.py).

- [ ] **Step 4: Run to pass** + guards: `.venv/bin/pytest tests/test_agent_responses_api.py tests/db_pg/test_idempotency_contract.py tests/test_route_auth_guard.py tests/test_backend_split_guard.py -q`

- [ ] **Step 5: Commit**

```bash
git add app/api/agent_runtime.py app/chat/headless.py src/repositories/idempotency.py src/repositories/idempotency_pg.py src/repositories/__init__.py app/main.py tests/test_agent_responses_api.py tests/db_pg/test_idempotency_contract.py
git commit -m "feat(api): POST /api/v1/agents/{slug}/responses — one-shot sync+background with idempotency"
```

---

### Task 10: Minimal builder UI — `/agents`

**Files:**
- Create: `app/web/agents_page.py`, `app/web/templates/agents.html`
- Modify: `app/web/__init__.py` or wherever sibling web routers register (mirror an existing simple page, e.g. the tokens/profile page routes in `app/web/`)
- Test: `tests/test_agents_page.py`

**Interfaces:**
- Consumes: `agents_repo()`, management API endpoints (the page's JS calls `/api/v1/agents`), design-system shell.
- Produces: `GET /agents` — list of my agents (name, slug, modes, budget), create form (name+slug+prompt), per-agent "Issue token" button (POSTs to the management API; disabled with tooltip when any mode is `'all'`), delete (except default).

House rules that WILL bite (from CLAUDE.md + memory):
- `{% extends "base_page.html" %}` (hero + toolbar + page blocks) — **never `base.html`**.
- Spread `_chrome_ctx(request, user)` into the template context or the page renders with no CSS/nav and tests stay green (`_SilentUndefined` hides it).
- Page CSS in `{% block head_extra %}`; only `var(--ds-*)` tokens, no raw hex (contract tests in `tests/test_design_system_contract.py` reject violations).

- [ ] **Step 1: Failing test** — route returns 200 for a logged-in user, contains the user's agent names, 401/redirect anonymous; design-contract suite still green.

- [ ] **Step 2: Run to fail.** `.venv/bin/pytest tests/test_agents_page.py -q`

- [ ] **Step 3: Implement page** (server-side render list; forms post to the JSON API with fetch; token modal shows the secret once).

- [ ] **Step 4: Run to pass** + `.venv/bin/pytest tests/test_design_system_contract.py -q`. Then **screenshot the page** via the local run harness before calling it done (memory: always screenshot new web pages).

- [ ] **Step 5: Commit**

```bash
git add app/web/agents_page.py app/web/templates/agents.html tests/test_agents_page.py
git commit -m "feat(web): minimal /agents builder page"
```

---

### Task 11: CLI parity — `agnes agent …`

**Files:**
- Create: `cli/commands/agent.py`
- Modify: `cli/main.py` (register subcommand group, following `cli/commands/tokens.py` registration)
- Test: `tests/test_cli_agent.py`; the API-coverage ratchet (`tests/` — find the ratchet test by `grep -rn "grandfathered" tests/ | head`) must not grow: new `/api/v1/agents*` routes need CLI/MCP coverage entries

**Interfaces:**
- Consumes: management + runtime endpoints (HTTP via the CLI's existing client — `cli/client.py` / `cli/v2_client.py` patterns), command-UX standard (`cli/query_hints.py`, `--json`, `--limit`).
- Produces:
  - `agnes agent list [--json]`
  - `agnes agent create <name> --slug <slug> [--prompt-file f] [--model m] [--budget N]`
  - `agnes agent show <slug> [--json]`
  - `agnes agent scope set <slug> --plugin p1 --table t1 ...` (repeatable flags → PUT scope)
  - `agnes agent token <slug> --name <n> [--expires-days N]` (prints secret once)
  - `agnes agent delete <slug>`
  - `agnes agent ask <slug> "<prompt>" [--timeout N] [--json]` → calls `/responses`, prints answer (this is the seed of the V1c terminal client)
- Follow the command-UX standard: positional term, `--json`, "not found" errors hint the next step via `cli/query_hints.py`.

- [ ] **Step 1: Failing tests** — mock the HTTP layer (same style as existing `tests/test_cli_*`), assert each subcommand hits the right endpoint/payload and renders; ratchet test still passes with the new routes mapped.

- [ ] **Step 2–4: fail → implement → pass.** `.venv/bin/pytest tests/test_cli_agent.py -q` plus the ratchet test module.

- [ ] **Step 5: Commit**

```bash
git add cli/commands/agent.py cli/main.py tests/test_cli_agent.py
git commit -m "feat(cli): agnes agent — manage profiles, mint tokens, one-shot ask"
```

---

### Task 12: MCP parity, CHANGELOG, docs

**Files:**
- Modify: `app/api/mcp/foundation_tools.py` (add `agent_list` / `agent_ask` foundation tools; `tests/test_mcp_tool_parity.py` guards the shape)
- Modify: `CHANGELOG.md` (`## [Unreleased]` → Added)
- Modify: `docs/README.md` (index the spec), `CLAUDE.md` — one short subsection under Architecture pointing at the spec (agents entity + `/api/v1/agents` + broker budget), no duplication

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: MCP tools + parity test green.** `.venv/bin/pytest tests/test_mcp_tool_parity.py -q`

- [ ] **Step 2: CHANGELOG bullets**

```markdown
### Added
- Agent profiles: named, scoped agents over the user's stack (`/api/v1/agents`,
  `/agents` builder page, `agnes agent` CLI). Every user gets an implicit
  default agent; web chat sessions are now attributed to it.
- Agent PATs (`typ=agent_pat`): agent-bound tokens valid only on the agent
  runtime API; issuable only for fully `selected`-scoped agents.
- Agent-as-API one-shot: `POST /api/v1/agents/{slug}/responses` (sync,
  background via jobs, Idempotency-Key support).
- LLM broker: per-agent model policy, `llm_usage` ledger, monthly token
  budgets (`429 budget_exhausted`, `x-agnes-budget-*` headers).
```

- [ ] **Step 3: Full suite**

Run: `.venv/bin/pytest tests/ --tb=short -n auto -q`
Expected: PASS (investigate any failure; unrelated pre-existing failures → verify via `git stash`, note in PR body).

- [ ] **Step 4: Commit**

```bash
git add app/api/mcp/foundation_tools.py CHANGELOG.md docs/README.md CLAUDE.md
git commit -m "feat(mcp,docs): agent tools parity + changelog for agent-api V1a"
```

---

## Execution notes

- Task order is the dependency order; Tasks 2–3 can run in parallel after 1; Tasks 10–11 in parallel after 9.
- Before the PR: run `/agnes-review` on the full diff (mandatory pre-merge gate), scan the diff for customer-specific tokens, and follow the release-cut decision tree from `docs/RELEASING.md`.
- Push the branch as lowercase `zs/...` (`git push -u origin HEAD:refs/heads/zs/agent-api-v1a`) — macOS case-collision gotcha.
