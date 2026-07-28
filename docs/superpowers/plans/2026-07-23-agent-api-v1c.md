# Agent-as-API V1c — Per-Agent Memory + Terminal Client Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give each agent a private, owner-governed memory notebook that persists across runs (`off`/`propose`/`auto` write modes, materialized into the sandbox at spawn), and ship `agnes chat` — a terminal thin client that holds a real streaming conversation with a composed agent over the public V1b session API.

**Spec:** `docs/superpowers/specs/2026-07-21-agent-profiles-and-agent-api-design.md` (§1 per-agent memory, §2 management memory endpoints, §V1c phasing). This is the **final V1 phase** — it delivers the user's headline use case: "spustím terminál appku pod tokenem a dostanu chat se vším, co jsem si poskládal."

**Architecture:** `agent_memories` (new v98 table) is the agent's private notebook, outside the corporate-memory governance pipeline. Writes flow through an API-backed "remember" tool the in-sandbox agent calls via the existing broker `agnes-api` channel; `memory_write_mode` decides whether a write lands `pending` (owner must approve) or `active`. At spawn, `agent_profile.py` (V1a) is extended to materialize active memories into the workdir alongside the persona. `agnes chat` is a pure client of the V1b session endpoints (`POST /sessions`, `POST /sessions/{id}/messages` SSE) — no privileged backchannel.

**Tech Stack:** DuckDB + Postgres (Alembic), existing ChatManager spawn/materialize seam (`app/chat/agent_profile.py`, `app/chat/workdir.py`), existing broker `agnes-api` replay channel (`app/api/broker.py`), V1b SSE session API, `cli/client.py` (+ a new streaming helper), pytest.

## Global Constraints

- **Dual-backend discipline** (CLAUDE.md): DuckDB repo method ↔ `_pg.py` sibling in the same task; factory entry; contract test in `tests/db_pg/`; DuckDB migration `_v97_to_v98` pairs with Alembic `0045_*`; both ladders converge (`tests/test_db_schema_version.py`). PG model in `src/models/agents.py`.
- **NEVER add secondary indexes** on hot-write tables (ART incident — `_v94_to_v95`). PKs / UNIQUE-in-CREATE only.
- **Factory-only repo access** (`tests/test_backend_split_guard.py`).
- **Route-auth guard** + **triple-surface ratchet** + **docs-coverage** (`tests/test_route_auth_guard.py`, `tests/test_documentation_api_triple_surface.py`, `tests/test_api_docs_coverage.py`) — every new route classified + documented.
- **Vendor-agnostic**; **CHANGELOG** bullet same PR (Task 7); **full suite before push** (known-environmental `test_cli_init` on Python < 3.13 excepted).
- **Memory is a prompt-injection surface** (spec §1): memories are agent-written content re-materialized into future prompts. `propose` is the default write mode — writes land `pending` and only owner-approved (`active`) rows materialize. The owner-inspection UI is a security control, not a convenience. Writes are size-capped + rate-limited.
- **V1c DEPENDS on V1b** for the terminal client (Task 6 consumes `POST /api/v1/agents/{slug}/sessions` + `POST /api/v1/sessions/{id}/messages` SSE + the AG-UI event vocabulary from `app/api/agent_sse.py`). Tasks 1-5 (memory) are independent of V1b and can land first.
- **V1a/V1b interfaces this plan builds on** (do not re-implement): `agent_profile.build_profile` + `record_snapshot` + the workdir materialization seam in `ChatManager._spawn_live` (`app/chat/agent_profile.py`, `app/chat/manager.py`); the broker `agnes-api` in-process replay channel that gives the sandbox authenticated access to `/api/*` under the owner identity (`app/api/broker.py`); `agents_repo()` (`get_by_id`, `get_by_slug`); `require_agent_runtime_principal` + `require_session_token` auth deps; `cli/client.py` (`api_get`/`api_post`); V1b's `frame_to_agui`/AG-UI SSE stream.

---

### Task 1: Schema v98 — `agent_memories`

**Files:**
- Modify: `src/db.py` (SCHEMA_VERSION 97→98, `_SYSTEM_SCHEMA`, new `_v97_to_v98`, register in ladder), `src/models/agents.py` (`AgentMemory` model), `src/models/__init__.py`
- Create: `migrations/versions/0045_agent_memories_v98.py`
- Test: `tests/test_agent_memories_schema.py`

**Interfaces:**
- Produces table `agent_memories (id, agent_id, owner_user_id, content, source_session_id, status, created_at, activated_at, archived_at)` — `status ∈ ('pending','active','archived')` (spec §1). `owner_user_id` denormalized for cheap owner-scoped listing.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_memories_schema.py
from src.db import _ensure_schema
from src.duckdb_conn import _open_duckdb


def test_v98_agent_memories(tmp_path):
    conn = _open_duckdb(str(tmp_path / "d.duckdb"))
    _ensure_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info('agent_memories')").fetchall()}
    assert {"id", "agent_id", "owner_user_id", "content", "source_session_id",
            "status", "created_at", "activated_at", "archived_at"} <= cols
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 98
    conn.close()
```

- [ ] **Step 2: Run to fail** — `.venv/bin/pytest tests/test_agent_memories_schema.py -q` → FAIL.

- [ ] **Step 3: Implement DuckDB** — bump `SCHEMA_VERSION = 98`; add to `_SYSTEM_SCHEMA` + `_v97_to_v98` (identical DDL, no secondary indexes):

```python
def _v97_to_v98(conn: duckdb.DuckDBPyConnection) -> None:
    """v98: per-agent private memory notebook (agent-api V1c). No secondary
    indexes (ART incident — see _v94_to_v95)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_memories (
            id                VARCHAR PRIMARY KEY,
            agent_id          VARCHAR NOT NULL,
            owner_user_id     VARCHAR NOT NULL,
            content           TEXT NOT NULL,
            source_session_id VARCHAR,
            status            VARCHAR NOT NULL DEFAULT 'pending',
            created_at        TIMESTAMP DEFAULT current_timestamp,
            activated_at      TIMESTAMP,
            archived_at       TIMESTAMP
        )
    """)
    conn.execute("UPDATE schema_version SET version = 98")
```

- [ ] **Step 4: Alembic + PG model** — `0045_agent_memories_v98.py` (`down_revision = "0044_webhooks_artifacts_v97"` — NOTE: V1b shortened the in-file revision id to ≤32 chars for the Postgres `alembic_version.version_num VARCHAR(32)` limit; the file is named `0044_agent_webhooks_artifacts_v97.py` but its `revision` string is `0044_webhooks_artifacts_v97` — chain against that), `AgentMemory` model in `src/models/agents.py`, export.

- [ ] **Step 5: Run** — `.venv/bin/pytest tests/test_agent_memories_schema.py tests/test_db_schema_version.py tests/db_pg/test_alembic_roundtrip.py -q` → PASS.

- [ ] **Step 6: Commit**

```bash
git add src/db.py migrations/versions/0045_agent_memories_v98.py src/models/agents.py src/models/__init__.py tests/test_agent_memories_schema.py
git commit -m "feat(db): v98 schema — agent_memories notebook"
```

(Include `docs/runbooks/wal-recovery.md` if `tests/test_runbook_wal_recovery.py` pins the version.)

---

### Task 2: AgentMemoriesRepository (dual-backend)

**Files:**
- Create: `src/repositories/agent_memories.py`, `src/repositories/agent_memories_pg.py`
- Modify: `src/repositories/__init__.py` (dispatch + `agent_memories_repo()` + `__all__`)
- Test: `tests/db_pg/test_agent_memories_contract.py`

**Interfaces:**
- Produces (both backends):
  - `create(id, agent_id, owner_user_id, content, source_session_id, status="pending") -> None`
  - `list_active(agent_id) -> list[dict]` — `status='active' AND archived_at IS NULL`, newest first
  - `list_for_agent(agent_id, status=None) -> list[dict]` — all (optionally status-filtered), newest first, for owner inspection
  - `get(id) -> Optional[dict]`
  - `approve(id) -> None` — `status='pending' → 'active'`, set `activated_at` (no-op if not pending)
  - `archive(id) -> None` — set `status='archived'`, `archived_at`
  - `delete(id) -> None`
  - `count_recent(agent_id, since) -> int` — for the write rate-limit (rows created after `since`)

- [ ] **Step 1: Write the failing contract test** (fixture scaffolding from `tests/db_pg/test_agents_contract.py`):

```python
def test_create_pending_then_approve(repo):
    repo.create(id="m1", agent_id="a1", owner_user_id="u1",
                content="report in CZK", source_session_id="c1")
    assert repo.list_active("a1") == []          # pending not active
    assert len(repo.list_for_agent("a1", status="pending")) == 1
    repo.approve("m1")
    active = repo.list_active("a1")
    assert len(active) == 1 and active[0]["activated_at"] is not None

def test_auto_write_is_active_immediately(repo):
    repo.create(id="m1", agent_id="a1", owner_user_id="u1",
                content="x", source_session_id="c1", status="active")
    assert len(repo.list_active("a1")) == 1

def test_archive_removes_from_active(repo):
    repo.create(id="m1", agent_id="a1", owner_user_id="u1", content="x",
                source_session_id="c1", status="active")
    repo.archive("m1")
    assert repo.list_active("a1") == []
    assert repo.get("m1")["status"] == "archived"

def test_count_recent(repo):
    from datetime import datetime, timezone, timedelta
    repo.create(id="m1", agent_id="a1", owner_user_id="u1", content="x",
                source_session_id="c1")
    since = datetime.now(timezone.utc) - timedelta(minutes=1)
    assert repo.count_recent("a1", since) == 1
```

- [ ] **Step 2: Run to fail** — `.venv/bin/pytest tests/db_pg/test_agent_memories_contract.py -q` → FAIL.

- [ ] **Step 3: Implement both backends + factory.**

- [ ] **Step 4: Run to pass + guard** — `.venv/bin/pytest tests/db_pg/test_agent_memories_contract.py tests/test_backend_split_guard.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/repositories/agent_memories.py src/repositories/agent_memories_pg.py src/repositories/__init__.py tests/db_pg/test_agent_memories_contract.py
git commit -m "feat(repos): agent_memories repository (dual-backend)"
```

---

### Task 3: Memory materialization at spawn

**Files:**
- Modify: `app/chat/agent_profile.py` (materialize active memories into the workdir)
- Modify: `app/chat/manager.py` (call the materialization at the same spawn seam that already calls `build_profile`/`record_snapshot`)
- Test: `tests/test_agent_memory_materialize.py`

**Interfaces:**
- Consumes: `agent_memories_repo().list_active(agent_id)`, the session workdir path (same seam `build_profile`'s result is written through).
- Produces:
  - `agent_profile.materialize_memories(agent_row: dict, workdir: Path) -> int` — writes active memories (newest-first, capped by `_MEMORY_TOKEN_BUDGET` chars ≈ tokens×4, e.g. 6000 tokens) to `workdir / ".claude" / "agent-memory.md"` as a simple dated list. Returns the count written. Empty/None → writes nothing, returns 0. Never raises into spawn (try/except + logger.exception — mirrors `record_snapshot`). Docstring: this is the read side; the write side is the remember tool (Task 4).
  - Manager hook: called during `_spawn_live` when the session has an `agent_id`, right after persona materialization. Default agent (no memories) → no-op, web chat unchanged.

- [ ] **Step 1: Failing tests** — `materialize_memories` writes a file containing two active memories' content; respects the char budget (only newest fit when over budget); no active memories → no file, returns 0; repo raising → returns 0, no exception. Manager seam: monkeypatch a fake repo + tmp workdir, assert the file lands when agent has memories and is absent for the empty default agent.

- [ ] **Step 2–4: fail → implement → pass** — `.venv/bin/pytest tests/test_agent_memory_materialize.py -q`.

- [ ] **Step 5: Commit**

```bash
git add app/chat/agent_profile.py app/chat/manager.py tests/test_agent_memory_materialize.py
git commit -m "feat(chat): materialize active agent memories into session workdir"
```

---

### Task 4: The "remember" tool — write endpoint honoring `memory_write_mode`

**Files:**
- Modify: `app/api/agent_runtime.py` (or a focused `app/api/agent_memory.py` router registered in `app/main.py`) — the in-sandbox write endpoint
- Modify: `app/chat/agent_profile.py::_context_skill` (advertise the remember tool to the agent when write mode ≠ off)
- Modify: `app/chat/config.py` (config knobs `agent_memory_max_chars` default 2000, `agent_memory_writes_per_hour` default 20)
- Test: `tests/test_agent_memory_write_api.py`

**Interfaces:**
- Consumes: the broker `agnes-api` channel resolves the sandbox's calls to the owner identity + the session's `agent_id` (the endpoint reads the calling session → agent, same way other in-sandbox `/api/*` calls are identity-resolved). `agent_memories_repo()`, `agents_repo()`.
- Produces:
  - `POST /api/v1/sessions/{id}/memories` `{content: str}` → `201 {id, status}` | `403 memory_writes_disabled` | `413 memory_too_large` | `429 memory_rate_limited`. Behavior by the agent's `memory_write_mode`:
    - `off` → `403 memory_writes_disabled` (and the tool is not advertised in `_context_skill`, so a well-behaved agent never calls it — but the endpoint enforces regardless).
    - `propose` → create with `status='pending'` → `201 {status: "pending"}`.
    - `auto` → create with `status='active'` (+ activated_at) → `201 {status: "active"}`.
  - Guards (all modes): `len(content) > agent_memory_max_chars` → 413; `count_recent(agent_id, now - 1h) >= agent_memory_writes_per_hour` → 429. `content` non-empty.
  - Auth: this route is reachable by the in-sandbox agent via the broker channel (which authenticates as the owner) AND by the owner's session/agent-PAT; scope to the session's agent.

- [ ] **Step 1: Failing tests** — with a session bound to an agent per write mode: `propose` → 201 pending, row visible in `list_for_agent(status="pending")`, NOT in `list_active`; `auto` → 201 active, in `list_active`; `off` → 403; oversize content → 413; exceeding the hourly cap (seed N recent rows) → 429; empty content → 422. `_context_skill` includes the remember-tool description only when mode ≠ off.

- [ ] **Step 2–4: fail → implement → pass** — `.venv/bin/pytest tests/test_agent_memory_write_api.py tests/test_route_auth_guard.py -q`.

- [ ] **Step 5: Commit**

```bash
git add app/api/agent_runtime.py app/chat/agent_profile.py app/chat/config.py tests/test_agent_memory_write_api.py docs/api-reference.md tests/test_documentation_api_triple_surface.py
git commit -m "feat(api): agent remember-tool write endpoint honoring memory_write_mode"
```

---

### Task 5: Memory management (inspect/approve/delete) + builder UI panel

**Files:**
- Modify: `app/api/agents_admin.py` (management memory routes — owner auth)
- Modify: `app/web/templates/agents.html` + `app/web/agents_page.py` (memory panel per agent)
- Modify: `docs/api-reference.md`, ratchet files
- Test: `tests/test_agent_memory_admin_api.py`, extend `tests/test_agents_page.py`

**Interfaces:**
- Consumes: `agent_memories_repo()`, `require_session_token` (owner-only), the `_load_agent(require_owner=True)` helper from V1a `agents_admin.py`.
- Produces (spec §2 management: `GET/PATCH/DELETE /api/v1/agents/{id}/memories`):
  - `GET /api/v1/agents/{id}/memories?status=` → cursor envelope of the agent's memories (owner-scoped; default all statuses).
  - `PATCH /api/v1/agents/{id}/memories/{memory_id}` `{action: "approve"|"archive"}` → `200` (approve: pending→active; archive: →archived). Invalid action → 400.
  - `DELETE /api/v1/agents/{id}/memories/{memory_id}` → `204`.
  - Builder UI: on `/agents`, a per-agent "Memory" panel listing memories with status badges; "Approve" button on pending rows (PATCH), "Delete" (DELETE). Server-rendered list + fetch()-driven actions, same DS-token / `_chrome_ctx` rules as V1a Task 10.

- [ ] **Step 1: Failing tests** — GET lists owner's memories (status filter); PATCH approve flips pending→active; PATCH archive; DELETE 204; cross-owner agent → 404; invalid action → 400. Page test: memory panel renders a seeded pending memory with an approve control; design-contract suite stays green.

- [ ] **Step 2–4: fail → implement → pass** — `.venv/bin/pytest tests/test_agent_memory_admin_api.py tests/test_agents_page.py tests/test_design_system_contract.py -q`. Screenshot the memory panel (house rule for web changes) — save rendered HTML to `.superpowers/sdd/v1c-memory-panel.html` if a live browser shot is impractical, note in report.

- [ ] **Step 5: Commit**

```bash
git add app/api/agents_admin.py app/web/templates/agents.html app/web/agents_page.py tests/test_agent_memory_admin_api.py tests/test_agents_page.py docs/api-reference.md tests/test_documentation_api_triple_surface.py
git commit -m "feat(api,web): agent memory inspection/approval management + builder panel"
```

---

### Task 6: `agnes chat` — terminal thin client (streaming, over V1b sessions)

**Files:**
- Modify: `cli/client.py` (add `api_post_sse(path, json) -> Iterator[dict]` streaming helper)
- Create: `cli/commands/chat.py` (`agnes chat <slug>` interactive REPL)
- Modify: `cli/main.py` (register `chat`)
- Test: `tests/test_cli_chat.py`

**PREREQUISITE:** V1b Task 4 (`POST /api/v1/agents/{slug}/sessions`, `POST /api/v1/sessions/{id}/messages` SSE, AG-UI events) must be landed. If executing V1c before V1b, STOP and report BLOCKED.

**Interfaces:**
- Consumes: V1b session endpoints + AG-UI SSE event stream; `cli/client.py` auth (the CLI's configured PAT — a user PAT or an agent PAT).
- Produces:
  - `cli/client.py::api_post_sse(path: str, json: dict) -> Iterator[dict]` — POSTs, reads the `text/event-stream` response line-by-line, yields parsed event dicts (`{type, ...}`) from `data:` lines. Uses the streaming HTTP client (httpx `stream`), bounded by a connect/read timeout; surfaces HTTP errors via the existing `render_error`.
  - `agnes chat <slug>`: `POST /api/v1/agents/{slug}/sessions` → session_id; then a REPL loop — read a line from stdin, `POST /api/v1/sessions/{id}/messages` (SSE), render events live: `TEXT_MESSAGE_CONTENT` deltas stream to stdout, `TOOL_CALL_START` prints a dim `⚙ <name>` line, `RUN_FINISHED` ends the turn, `RUN_ERROR` prints the error and ends the turn. `/exit` or EOF quits (best-effort `DELETE /sessions/{id}`). `--once "<prompt>"` sends a single turn and exits (non-interactive/scriptable). Ctrl-C cancels the in-flight turn via `POST /sessions/{id}/cancel` then returns to the prompt.
  - Follows the command-UX standard (`--json` on `--once` dumps the full event list; errors hint next step).

- [ ] **Step 1: Failing tests** — mock `api_post_sse` to yield a canned AG-UI event sequence: `agnes chat <slug> --once "hi"` prints the assembled answer (concatenated `TEXT_MESSAGE_CONTENT` deltas), exits 0; `RUN_ERROR` event → non-zero exit + rendered message; `--once --json` dumps the event list; session-create failure (mock 404) → helpful error. Interactive loop can be tested by feeding stdin lines + an `/exit`.

- [ ] **Step 2–4: fail → implement → pass** — `.venv/bin/pytest tests/test_cli_chat.py -q`.

- [ ] **Step 5: Commit**

```bash
git add cli/client.py cli/commands/chat.py cli/main.py tests/test_cli_chat.py
git commit -m "feat(cli): agnes chat — streaming terminal client over the agent session API"
```

---

### Task 7: MCP note, CHANGELOG, docs, CLAUDE.md

**Files:**
- Modify: `tests/test_documentation_api_triple_surface.py` (classify the new memory routes — management memory CRUD is CLI-reachable via a new `agnes agent memory …` group if added, else `_EXEMPT` with reason; the in-sandbox write endpoint and SSE chat have no MCP analogue → `_EXEMPT`), `CHANGELOG.md`, `docs/api-reference.md`, `docs/README.md`, `CLAUDE.md`
- Modify (optional): `cli/commands/agent.py` — add `agnes agent memory list|approve|delete <slug>` so the management memory routes have a CLI surface (recommended for ratchet cohort membership)
- Test: `tests/test_cli_agent.py` (if the memory subcommands are added), `tests/test_mcp_tool_parity.py`

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: CLI memory subcommands** (if adding) + tests green.

- [ ] **Step 2: CHANGELOG** (`## [Unreleased]` → Added):

```markdown
- Per-agent memory: agents keep a private notebook that persists across runs.
  A sandbox "remember" tool writes memories (size-capped, rate-limited);
  `memory_write_mode` (`off`/`propose`/`auto`, default `propose`) decides
  whether a write needs owner approval before it re-materializes into future
  runs. Owners inspect/approve/delete via `/api/v1/agents/{id}/memories`, the
  `agnes agent memory` CLI, and the `/agents` builder panel.
- `agnes chat <slug>`: an interactive terminal client that streams a real
  conversation with a composed agent over the public session API (AG-UI SSE),
  with a scriptable `--once` mode. No privileged backchannel — it is a pure
  client of `/api/v1`.
```

- [ ] **Step 3: CLAUDE.md** — extend the V1a "Agent profiles & agent-as-API" subsection with 2 lines: per-agent memory (`memory_write_mode`, approval flow) + `agnes chat` terminal client. `docs/README.md` — add the terminal client to the surfaces list if one exists.

- [ ] **Step 4: Full suite**

Run: `.venv/bin/pytest tests/ --tb=short -n auto -q`
Expected: PASS (known-environmental `test_cli_init` excepted).

- [ ] **Step 5: Commit**

```bash
git add tests/test_documentation_api_triple_surface.py cli/commands/agent.py tests/test_cli_agent.py tests/test_mcp_tool_parity.py CHANGELOG.md docs/api-reference.md docs/README.md CLAUDE.md
git commit -m "feat(cli,docs): agent memory CLI + V1c changelog and docs"
```

---

## Execution notes

- Task order: 1→2 (schema→repo); 3, 4 build on 2 (materialize read side, remember write side); 5 (management + UI) builds on 2; 6 (terminal client) requires **V1b Task 4** landed — sequence V1c Task 6 after V1b if running both waves; 7 last. Tasks 3/4/5 can proceed in parallel after 2.
- **Delivers the headline use case**: after V1c, a user composes an agent, mints an agent PAT, runs `agnes chat <slug>` in a terminal, and holds a streaming conversation with everything they configured — skills, data scope, memory, token triage — the server holding the LLM key.
- Before the PR: `/agnes-review` on the full diff, customer-token scan, release-cut per `docs/RELEASING.md`. Release/PR-split across V1a+V1b+V1c is the user's decision at merge time.

## Deferred to V2 (explicitly out of V1c scope)

Local Claude Code "power mode" (`agnes agent up`), agent sharing across users/groups, `requires_action` human-in-the-loop approval, agent-memory promotion into governed corporate memory, A2A agent card, OpenAI-compatible shim. See the spec's V2 section.

---

## Review corrections — BINDING (2026-07-23, post codebase + adversarial review)

These override the task text above wherever they conflict. Verified against the real tree.

**C1 (Task 3 — materialize at the PRE-spawn seam, not post-spawn).** `build_profile` runs pre-spawn feeding `prepare_session_dir` (`app/chat/manager.py:894/902`); `record_snapshot` runs POST-spawn, AFTER `upload_workspace` (`manager.py:945`). A memory file written at the `record_snapshot` site is never uploaded into the VM. `materialize_memories(agent_row, session_dir)` MUST be called at the `build_profile`/`prepare_session_dir` point, before `_spawn_runner` (`manager.py:904`) — same host-dir-then-uploaded path as the persona. (Same remote-VM fact as V1b C1.)

**C2 (Task 4 — bind the remember write to the CALLER's session, never the path `{id}`).** This is the memory-poisoning hole. The broker mints the sandbox's JWT with `chat_session_id` = the caller's real session and stashes it on `request.state.chat_session_id` (`app/api/broker.py:254`, consumed in `app/api/query.py:74`). The write endpoint MUST derive the target session/agent from `request.state.chat_session_id` (fall back to the `AGNES_SESSION_ID` claim), and REJECT (`403 session_mismatch`) if a path `{id}` is supplied that differs. Otherwise a prompt-injected `auto`-mode agent A can POST to a session of agent B (whose mode is `off`) and poison B's notebook. Enforce the CALLING agent's `memory_write_mode`, not the path agent's.

**C3 (Task 4 — total pending cap, not just rate limit).** The 20/hr rate limit doesn't bound total `pending` rows — a propose-mode agent accrues unbounded pending memories (DB growth + owner-UI DoS). Add `agent_memory_max_pending` (default 100): at the cap, new writes → `429 memory_pending_full`. Reap `pending` rows older than `agent_memory_pending_ttl_days` (default 30).

**C4 (Task 3 — active-memory eviction is explicit, and surfaced).** The ~6000-token materialize budget can be exceeded by many `active` memories; newest-first means a freshly-approved memory may silently never materialize ("active" ≠ "in effect"). Document the newest-first precedence, and the management list (Task 5) must show which active memories are IN-BUDGET vs shadowed, so an owner who just approved one isn't misled.

**C5 (Task 1/2 — cascade on agent delete).** `agent_memories` must be cascade-deleted when the agent is deleted (with the V1b webhooks/artifacts cascade — see V1b C14). Add `agent_memories_repo().delete_for_agent(agent_id)` (both backends) and wire it into `delete_agent`; test the cascade.

**C6 (Task 6 — session-scoped auth dep + reaper keepalive).** `agnes chat` consumes V1b's session endpoints, which require the NEW `require_session_principal` dep (V1b C7), not the slug-keyed one. Also: a long interactive chat with think-pauses must not have its sandbox reaped mid-conversation — each `POST /messages` must extend the session's idle deadline (the attach already cancels linger; confirm a message resets the paused-TTL clock), else the next turn 404s. Document the interactive keepalive.

**C7 (Task 6 — Ctrl-C cancel race).** Interrupting the httpx SSE iterator while POSTing `/sessions/{id}/cancel` on a separate connection and returning cleanly to the REPL: install a SIGINT handler that (a) stops consuming the stream, (b) fires cancel best-effort, (c) returns to the prompt — never leaves a half-read stream or a wedged terminal. Test the handler in isolation (simulate KeyboardInterrupt mid-iteration).

**C8 (Task 6 — disconnect ≠ cancel).** Same as V1b C9: abandoning the stream does NOT stop the run or refund budget; only `/cancel` does. `agnes chat` `/exit` should best-effort `DELETE` the session (frees the sandbox); document that a bare disconnect leaves it to the reaper.

**C9 (cross-plan sequencing).** V1c Task 6 is BLOCKED until V1b Task 4 lands. If executing V1b then V1c as one wave, V1c's `manager.py`/`agent_profile.py` edits (Task 3 materialize) merge-couple with V1b's harvest edits (V1b C1) — integrate those two files' changes together, don't build them in isolated parallel worktrees.
