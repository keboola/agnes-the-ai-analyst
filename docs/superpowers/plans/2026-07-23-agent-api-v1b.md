# Agent-as-API V1b — Surface Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the agent-as-API surface: multi-message sessions with SSE streaming (AG-UI event vocabulary), cancel, artifact harvest + download, SSRF-hardened signed webhooks on the worker runtime, structured JSON output, and a usage-reporting endpoint — all on the public `/api/v1` surface built in V1a.

**Spec:** `docs/superpowers/specs/2026-07-21-agent-profiles-and-agent-api-design.md` (§2 runtime, §2 streaming wire format, §2 webhooks/artifacts, §5). This plan implements **V1b only** — agent memories + the `agnes chat` terminal client are V1c.

**Architecture:** Sessions reuse the existing ChatManager attach/detach + the `stamp_frame` seq/id envelope (every frame already carries `seq` int + `id` `{chat_id}:{seq}` — the per-session monotonic id the spec's SSE contract needs). A streaming sink adapts `manager.attach`'s frame fan-out into an SSE byte stream, mapping internal frame types → AG-UI events. Artifacts are harvested from the sandbox workdir into the existing object store on run completion. Webhooks are a new outbound-delivery job kind on the existing worker runtime with HMAC signing + SSRF-denied registration.

**Tech Stack:** FastAPI (`StreamingResponse`, `text/event-stream`), DuckDB + Postgres (Alembic), existing ChatManager + `app.chat.frame_seq` + `app.chat.replay`, existing worker runtime (`app/worker/`), existing `src/object_store.py`, pytest.

## Global Constraints

- **Dual-backend discipline** (CLAUDE.md): new repo method → both `src/repositories/X.py` (DuckDB) AND `X_pg.py` (PG) in the same task; factory entry in `src/repositories/__init__.py`; contract test in `tests/db_pg/`; DuckDB migration `_v96_to_v97` pairs with Alembic `0044_*`; both ladders reach the same endpoint (`tests/test_db_schema_version.py` is the gate). PG SQLAlchemy models in `src/models/` (see `src/models/agents.py` from V1a for the pattern).
- **NEVER add secondary indexes** on hot-write tables (DuckDB ART incident — see `_v94_to_v95` in `src/db.py`). New tables: primary keys / composite PKs / UNIQUE-in-CREATE only.
- **Never instantiate repos directly** — always via factory functions (`tests/test_backend_split_guard.py` static ratchet).
- **Every new `/api/*` route** carries a recognized auth dependency (`tests/test_route_auth_guard.py`).
- **Triple-surface ratchet** (`tests/test_documentation_api_triple_surface.py`): every new public endpoint is classified in `_COHORT` (CLI + MCP) or `_EXEMPT` (with a permanent, accurate reason). SSE/binary/streaming endpoints have no MCP analogue → `_EXEMPT` with reason.
- **Docs-coverage gate** (`tests/test_api_docs_coverage.py`): every new route documented in `docs/api-reference.md`.
- **Vendor-agnostic**: no customer names/hosts in code, comments, tests, commit messages. Frame SSRF motivation abstractly ("cloud metadata endpoints, private/link-local ranges").
- **CHANGELOG**: one `## [Unreleased]` bullet per user-visible change, same PR (Task 8).
- **Full suite before push**: `.venv/bin/pytest tests/ --tb=short -n auto -q` (known-environmental failure: `test_cli_init::test_shortcut_windows_writes_cmd_shim` on Python < 3.13 — not a regression).
- **Auth reuse**: session runtime endpoints use V1a's `require_agent_runtime_principal` pattern (`app/api/agent_runtime.py`) — `get_current_user` → agent by slug for owner → agent-PAT binding 403 → `ResourceType.CHAT` gate. Never `get_optional_user`.
- **V1a interfaces this plan builds on** (do not re-implement): `HeadlessSink` + `run_one_shot`/`await_completion` (`app/chat/headless.py`); `manager.attach(chat_id, sink, is_primary)` / `detach_sink` / `send_user_message` / `create_session(surface=Surface.API, agent_id=...)` (`app/chat/manager.py`); `stamp_frame` seq/id + `app.chat.replay.replay_since` (`app/chat/frame_seq.py`); `agents_repo()`, `llm_usage_repo()` (`list_for_session`, `month_total_tokens`), `idempotency_repo()`; `usage_accumulator_flush()` (`app/api/agent_runtime.py`); jobs runtime + `register_kind` (`app/worker/kinds.py`, `app/worker/registry.py`), gateway-affine registration pattern from `_run_agent_response`.

---

### Task 1: Schema v97 — `agent_webhooks` + `agent_artifacts`

**Files:**
- Modify: `src/db.py` (SCHEMA_VERSION 96→97, `_SYSTEM_SCHEMA` additions, new `_v96_to_v97`, register in the migration ladder next to `_v95_to_v96`)
- Create: `migrations/versions/0044_agent_webhooks_artifacts_v97.py`
- Modify: `src/models/agents.py` (add `AgentWebhook`, `AgentArtifact` SQLAlchemy models), `src/models/__init__.py` (`__all__` exports)
- Test: `tests/test_agent_v1b_schema.py`

**Interfaces:**
- Produces tables:
  - `agent_webhooks (id, agent_id, owner_user_id, url, secret, events, active, consecutive_failures, disabled_at, created_at, updated_at)` — `events` = comma-joined event names (e.g. `"job.completed,job.failed"`); `secret` = HMAC signing secret (random, shown once at create like a PAT).
  - `agent_artifacts (id, session_id, agent_id, owner_user_id, filename, object_key, size_bytes, content_type, md5, created_at)` — blob lives in object store under `object_key`; row is metadata only.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_v1b_schema.py
"""v97: agent_webhooks + agent_artifacts tables exist after _ensure_schema."""
from src.db import _ensure_schema
from src.duckdb_conn import _open_duckdb


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info('{table}')").fetchall()}


def test_v97_tables(tmp_path):
    conn = _open_duckdb(str(tmp_path / "d.duckdb"))
    _ensure_schema(conn)
    assert {"id", "agent_id", "owner_user_id", "url", "secret", "events",
            "active", "consecutive_failures", "disabled_at", "created_at",
            "updated_at"} <= _cols(conn, "agent_webhooks")
    assert {"id", "session_id", "agent_id", "owner_user_id", "filename",
            "object_key", "size_bytes", "content_type", "md5",
            "created_at"} <= _cols(conn, "agent_artifacts")
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 97
    conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_agent_v1b_schema.py -q`
Expected: FAIL (tables missing / version 96).

- [ ] **Step 3: Implement DuckDB side**

In `src/db.py`: bump `SCHEMA_VERSION = 97`. Add both tables to `_SYSTEM_SCHEMA` (fresh install) AND create `_v96_to_v97` (upgrade) with identical DDL (follow the `_v95_to_v96` idempotent style; no secondary indexes):

```python
def _v96_to_v97(conn: duckdb.DuckDBPyConnection) -> None:
    """v97: agent webhooks + artifacts (agent-api V1b). No secondary indexes
    (ART-index incident — see _v94_to_v95)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_webhooks (
            id                   VARCHAR PRIMARY KEY,
            agent_id             VARCHAR NOT NULL,
            owner_user_id        VARCHAR NOT NULL,
            url                  VARCHAR NOT NULL,
            secret               VARCHAR NOT NULL,
            events               VARCHAR NOT NULL DEFAULT 'job.completed,job.failed',
            active               BOOLEAN NOT NULL DEFAULT TRUE,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            disabled_at          TIMESTAMP,
            created_at           TIMESTAMP DEFAULT current_timestamp,
            updated_at           TIMESTAMP DEFAULT current_timestamp
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_artifacts (
            id            VARCHAR PRIMARY KEY,
            session_id    VARCHAR NOT NULL,
            agent_id      VARCHAR,
            owner_user_id VARCHAR NOT NULL,
            filename      VARCHAR NOT NULL,
            object_key    VARCHAR NOT NULL,
            size_bytes    BIGINT NOT NULL DEFAULT 0,
            content_type  VARCHAR,
            md5           VARCHAR,
            created_at    TIMESTAMP DEFAULT current_timestamp
        )
    """)
    conn.execute("UPDATE schema_version SET version = 97")
```

Register `_v96_to_v97` wherever `_v95_to_v96` is registered (search `src/db.py` for `_v95_to_v96`).

- [ ] **Step 4: Implement Alembic + PG models**

`migrations/versions/0044_agent_webhooks_artifacts_v97.py` modeled on `0043_agents_v96.py` (`down_revision = "0043_agents_v96"`), same DDL, no `op.create_index`. Add `AgentWebhook` + `AgentArtifact` to `src/models/agents.py` mirroring column types exactly (the drift test `tests/db_pg/test_alembic_roundtrip.py::test_no_model_migration_drift` requires them); export in `src/models/__init__.py`.

- [ ] **Step 5: Run tests**

Run: `.venv/bin/pytest tests/test_agent_v1b_schema.py tests/test_db_schema_version.py tests/db_pg/test_alembic_roundtrip.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/db.py migrations/versions/0044_agent_webhooks_artifacts_v97.py src/models/agents.py src/models/__init__.py tests/test_agent_v1b_schema.py
git commit -m "feat(db): v97 schema — agent_webhooks + agent_artifacts"
```

Also update the WAL-recovery runbook version assertion if `tests/test_runbook_wal_recovery.py` fails (it pins SCHEMA_VERSION) — include `docs/runbooks/wal-recovery.md` in the add if so.

---

### Task 2: Webhooks + artifacts repos (dual-backend)

**Files:**
- Create: `src/repositories/agent_webhooks.py`, `src/repositories/agent_webhooks_pg.py`, `src/repositories/agent_artifacts.py`, `src/repositories/agent_artifacts_pg.py`
- Modify: `src/repositories/__init__.py` (dispatch entries + `agent_webhooks_repo()` / `agent_artifacts_repo()` builders + `__all__`)
- Test: `tests/db_pg/test_agent_webhooks_contract.py`, `tests/db_pg/test_agent_artifacts_contract.py`

**Interfaces:**
- Consumes: v97 schema (Task 1).
- Produces (both backends, identical signatures):
  - webhooks: `create(id, agent_id, owner_user_id, url, secret, events) -> None`; `list_for_agent(agent_id) -> list[dict]`; `list_active_for_event(agent_id, event: str) -> list[dict]` (active=TRUE and `event` in the row's comma-split `events`); `get(id) -> Optional[dict]`; `delete(id) -> None`; `record_failure(id) -> int` (increments `consecutive_failures`, returns new count); `record_success(id) -> None` (resets failures to 0); `disable(id) -> None` (active=FALSE, sets `disabled_at`).
  - artifacts: `create(id, session_id, agent_id, owner_user_id, filename, object_key, size_bytes, content_type, md5) -> None`; `list_for_session(session_id) -> list[dict]`; `get(id) -> Optional[dict]`.

- [ ] **Step 1: Write the failing contract tests** (copy fixture scaffolding from `tests/db_pg/test_agents_contract.py` — the `_make_duckdb_repo`/`_make_pg_repo`/parametrized `repo` pattern).

```python
# tests/db_pg/test_agent_webhooks_contract.py — key cases
def test_create_and_list_for_agent(repo):
    repo.create(id="w1", agent_id="a1", owner_user_id="u1",
                url="https://hook.example.com/x", secret="s1",
                events="job.completed,job.failed")
    rows = repo.list_for_agent("a1")
    assert len(rows) == 1 and rows[0]["url"] == "https://hook.example.com/x"

def test_list_active_for_event_filters(repo):
    repo.create(id="w1", agent_id="a1", owner_user_id="u1", url="https://h/x",
                secret="s", events="job.completed")
    assert len(repo.list_active_for_event("a1", "job.completed")) == 1
    assert repo.list_active_for_event("a1", "job.failed") == []

def test_failure_tracking_and_disable(repo):
    repo.create(id="w1", agent_id="a1", owner_user_id="u1", url="https://h/x",
                secret="s", events="job.completed")
    assert repo.record_failure("w1") == 1
    assert repo.record_failure("w1") == 2
    repo.record_success("w1")
    assert repo.get("w1")["consecutive_failures"] == 0
    repo.disable("w1")
    assert repo.get("w1")["active"] is False
    assert repo.list_active_for_event("a1", "job.completed") == []
```

```python
# tests/db_pg/test_agent_artifacts_contract.py — key case
def test_create_and_list_for_session(repo):
    repo.create(id="ar1", session_id="c1", agent_id="a1", owner_user_id="u1",
                filename="report.csv", object_key="artifacts/c1/report.csv",
                size_bytes=1234, content_type="text/csv", md5="abc")
    rows = repo.list_for_session("c1")
    assert len(rows) == 1 and rows[0]["filename"] == "report.csv"
    assert repo.get("ar1")["object_key"] == "artifacts/c1/report.csv"
```

- [ ] **Step 2: Run to fail** — `.venv/bin/pytest tests/db_pg/test_agent_webhooks_contract.py tests/db_pg/test_agent_artifacts_contract.py -q` → FAIL (import errors).

- [ ] **Step 3: Implement both backends + factory** (style from `src/repositories/agents.py` + `agents_pg.py`; `list_active_for_event` filters `active` in SQL and does the comma-membership in Python for portability, or `WHERE ',' || events || ',' LIKE '%,'||?||',%'` — pick the SQL form and keep both backends identical in result). Factory entries `"agent_webhooks"` / `"agent_artifacts"` + builders + `__all__`.

- [ ] **Step 4: Run to pass + guard** — `.venv/bin/pytest tests/db_pg/test_agent_webhooks_contract.py tests/db_pg/test_agent_artifacts_contract.py tests/test_backend_split_guard.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/repositories/agent_webhooks.py src/repositories/agent_webhooks_pg.py src/repositories/agent_artifacts.py src/repositories/agent_artifacts_pg.py src/repositories/__init__.py tests/db_pg/test_agent_webhooks_contract.py tests/db_pg/test_agent_artifacts_contract.py
git commit -m "feat(repos): agent_webhooks + agent_artifacts repositories (dual-backend)"
```

---

### Task 3: AG-UI SSE event mapper (pure module)

**Files:**
- Create: `app/api/agent_sse.py`
- Test: `tests/test_agent_sse.py`

**Interfaces:**
- Consumes: internal frame dicts (from `manager.attach`'s sink; each carries `type`, optional `content`, `seq`, `id`, plus tool fields). Internal frame types observed in `app/chat/manager.py`: `ready`, `token` (streaming delta with `content`), `assistant_message` (full text in `content`), `tool_call` (`name`, `input`), `tool_result` (`result`), `done`, `error` (`message`), `cancelled`, `session_renamed`.
- Produces:
  - `frame_to_agui(frame: dict) -> Optional[dict]` — maps one internal frame to one AG-UI event dict, or `None` to drop (e.g. `session_renamed`). Mapping (spec §2 streaming — AG-UI vocabulary):
    - `ready` → `{"type": "RUN_STARTED"}`
    - `token` → `{"type": "TEXT_MESSAGE_CONTENT", "delta": frame["content"]}`
    - `assistant_message` → `{"type": "TEXT_MESSAGE_END", "content": frame["content"]}` (the full message boundary; deltas arrive via `token`)
    - `tool_call` → `{"type": "TOOL_CALL_START", "name": frame.get("name"), "args": frame.get("input")}`
    - `tool_result` → `{"type": "TOOL_CALL_END", "result": frame.get("result")}`
    - `done` → `{"type": "RUN_FINISHED"}`
    - `error` → `{"type": "RUN_ERROR", "message": frame.get("message")}`
    - `cancelled` → `{"type": "RUN_ERROR", "message": "cancelled", "code": "cancelled"}`
    - anything else → `None`
  - `sse_bytes(event: dict, frame_id: Optional[str]) -> bytes` — serialize one SSE record: `id: {frame_id}\n` (only when `frame_id` is not None — the per-session `{chat_id}:{seq}` from `stamp_frame`), `event: {event['type']}\n`, `data: {json.dumps(event)}\n\n`. Encodes utf-8.
  - `SSE_TERMINAL_TYPES = {"RUN_FINISHED", "RUN_ERROR"}` — the stream generator closes after emitting one of these.

- [ ] **Step 1: Write the failing tests** — each mapping case; `frame_to_agui` returns None for `session_renamed`; `sse_bytes` includes `id:` only when frame_id present, always ends `\n\n`, `event:` line matches type, `data:` is valid JSON round-tripping the event.

```python
def test_token_maps_to_text_content():
    assert frame_to_agui({"type": "token", "content": "he"}) == {
        "type": "TEXT_MESSAGE_CONTENT", "delta": "he"}

def test_done_maps_to_run_finished():
    assert frame_to_agui({"type": "done"}) == {"type": "RUN_FINISHED"}

def test_unknown_frame_dropped():
    assert frame_to_agui({"type": "session_renamed", "title": "x"}) is None

def test_sse_bytes_with_id():
    out = sse_bytes({"type": "RUN_FINISHED"}, "c1:5").decode()
    assert out.startswith("id: c1:5\n")
    assert "event: RUN_FINISHED\n" in out
    assert out.endswith("\n\n")
    import json
    data_line = [l for l in out.splitlines() if l.startswith("data: ")][0]
    assert json.loads(data_line[len("data: "):])["type"] == "RUN_FINISHED"

def test_sse_bytes_without_id_omits_id_line():
    out = sse_bytes({"type": "RUN_STARTED"}, None).decode()
    assert "id:" not in out
```

- [ ] **Step 2–4: fail → implement → pass** — `.venv/bin/pytest tests/test_agent_sse.py -q`.

- [ ] **Step 5: Commit**

```bash
git add app/api/agent_sse.py tests/test_agent_sse.py
git commit -m "feat(api): AG-UI SSE event vocabulary mapper"
```

---

### Task 4: Sessions endpoints + SSE streaming + cancel

**Files:**
- Create: `app/chat/streaming_sink.py` (StreamingSink: async-queue frame sink adapting `manager.attach` fan-out to an async iterator)
- Modify: `app/api/agent_runtime.py` (add session routes) — or create `app/api/agent_sessions.py` and register in `app/main.py` if `agent_runtime.py` grows past ~400 lines (check; keep files focused)
- Modify: `app/main.py` if a new router module is created
- Modify: `tests/test_documentation_api_triple_surface.py` (`_EXEMPT` the new routes — SSE/session control have no MCP analogue), `docs/api-reference.md`, `tests/db_pg/test_endpoints_smoke.py` if the ratchet trips
- Test: `tests/test_agent_sessions_api.py`

**Interfaces:**
- Consumes: `require_agent_runtime_principal` (V1a auth dep), `manager.create_session(surface=Surface.API, agent_id=...)`, `manager.attach`/`detach_sink`/`send_user_message`, `frame_to_agui`/`sse_bytes`/`SSE_TERMINAL_TYPES` (Task 3), `chat_session_repo()`.
- Produces:
  - `StreamingSink` (`app/chat/streaming_sink.py`): duck-typed `send_json(frame)` pushes onto an `asyncio.Queue`; `close()` pushes a sentinel; `async def __aiter__` yields frames until sentinel. Bounded queue (maxsize e.g. 1000) — on overflow, drop-oldest with a logged warning (never block the manager's broadcast).
  - `POST /api/v1/agents/{slug}/sessions` `{}` → `201 {"session_id"}` (creates an API-surface session bound to the agent; no prompt sent yet).
  - `POST /api/v1/sessions/{id}/messages` `{input: str, response_format?: {...}}` → `200` `StreamingResponse(media_type="text/event-stream")`. Ownership: session's `agent_id` must resolve to an agent the caller owns / the agent PAT is bound to (reuse the auth dep's principal, then verify `chat_session_repo().get_session(id).agent_id` matches). Attaches a `StreamingSink`, sends the message, streams AG-UI events (with per-frame `id:` from `frame["id"]`) until a terminal event, then detaches. Sets headers `x-request-id`.
  - `GET /api/v1/sessions/{id}` → `{session_id, agent_id, state, messages: [...]}` (history from `chat_session_repo` / message repo; owner-scoped).
  - `POST /api/v1/sessions/{id}/cancel` → `202 {}` — calls the manager's existing cancel path (find it: `grep -n "cancel" app/chat/manager.py` — V1a Task 9 review referenced a cancel seam; use `manager.cancel_turn`/equivalent). Preserves session (≠ delete).
  - `DELETE /api/v1/sessions/{id}` → `204` — archive/kill via manager + repo (owner-scoped).
- All session routes are owner/agent-PAT scoped: a session created under agent A is reachable only by A's owner or an A-bound agent PAT (404 otherwise — never 403-leak existence across owners).

- [ ] **Step 1: Write the failing tests** — with the manager faked at the attach/stream seam (monkeypatch a fake manager that, on `attach`, feeds the StreamingSink a canned frame sequence `ready→token→assistant_message→done`): POST sessions → 201 with session_id; POST messages → 200, `content-type: text/event-stream`, body contains `event: RUN_STARTED`, `event: TEXT_MESSAGE_CONTENT`, `event: RUN_FINISHED`, and `id: {session}:N` lines; GET session → history; cancel → 202; DELETE → 204; cross-owner session id → 404; wrong-agent PAT → 404. Structured-output field accepted (full behavior in Task 7 — here just assert it's threaded to send_user_message without error).

- [ ] **Step 2: Run to fail** — `.venv/bin/pytest tests/test_agent_sessions_api.py -q`.

- [ ] **Step 3: Implement** StreamingSink + routes. The SSE generator:

```python
async def _event_stream(manager, chat_id, sink, request_format):
    try:
        async for frame in sink:
            event = frame_to_agui(frame)
            if event is None:
                continue
            yield sse_bytes(event, frame.get("id"))
            if event["type"] in SSE_TERMINAL_TYPES:
                break
    finally:
        await manager.detach_sink(chat_id, sink)
```

- [ ] **Step 4: Run to pass + guards** — `.venv/bin/pytest tests/test_agent_sessions_api.py tests/test_route_auth_guard.py tests/test_documentation_api_triple_surface.py -q`.

- [ ] **Step 5: Commit**

```bash
git add app/chat/streaming_sink.py app/api/agent_runtime.py app/main.py tests/test_agent_sessions_api.py tests/test_documentation_api_triple_surface.py docs/api-reference.md
git commit -m "feat(api): agent sessions — SSE streaming (AG-UI events), cancel, history"
```

---

### Task 5: Artifact harvest + download

**Files:**
- Create: `app/chat/artifact_harvest.py`
- Modify: `app/chat/headless.py` (harvest hook on run completion), `app/api/agent_runtime.py` (artifact routes)
- Modify: `docs/api-reference.md`, ratchet files if tripped
- Test: `tests/test_agent_artifacts_api.py`

**Interfaces:**
- Consumes: `object_store()` (`src/object_store.py` — `put_bytes(key, data, md5)` / `presign_get(key, ttl_s)` / `get_bytes(key)`), `agent_artifacts_repo()` (Task 2), the sandbox workdir path (find how headless/manager expose the session workdir — `grep -n "workdir\|prepare_session_dir" app/chat/*.py`; the harvest reads files the run produced under a designated output subdir).
- Produces:
  - `app/chat/artifact_harvest.py::harvest_session_artifacts(session_id, agent_id, owner_user_id, workdir: Path) -> list[dict]` — scans `workdir / "outputs"` (the agreed artifact drop dir; document it), for each file: compute md5, `object_store().put_bytes(key=f"agent-artifacts/{session_id}/{filename}", ...)`, insert an `agent_artifacts` row, return the metadata list. Never raises into the run path (try/except per file + logger). If `object_store()` is None (not configured), log-and-skip (artifacts are best-effort in V1b; note in docstring).
  - Hook: `run_one_shot` (and the session message path) call `harvest_session_artifacts(...)` after the turn's `done`, before sandbox teardown/detach.
  - `GET /api/v1/sessions/{id}/artifacts` → `{data: [{id, filename, size_bytes, content_type, created_at}], has_more, next_cursor}` (owner/agent-PAT scoped; cursor envelope).
  - `GET /api/v1/sessions/{id}/artifacts/{artifact_id}` → `307` redirect to `object_store().presign_get(object_key)` when the store supports presign, else streams `get_bytes` with `content-type` + `content-disposition: attachment; filename=`. Authorization: owner, or the agent PAT bound to the session's agent — never another agent's PAT.

- [ ] **Step 1: Write the failing tests** — `harvest_session_artifacts` with a tmp workdir containing 2 output files + a monkeypatched in-memory object store → 2 rows created, keys present, md5 correct; empty/absent outputs dir → no rows, no error; object_store None → no rows, no raise. API: GET artifacts lists them (owner); download returns bytes/redirect; cross-agent PAT → 404; unknown artifact → 404.

- [ ] **Step 2–4: fail → implement → pass** — `.venv/bin/pytest tests/test_agent_artifacts_api.py tests/test_route_auth_guard.py -q`.

- [ ] **Step 5: Commit**

```bash
git add app/chat/artifact_harvest.py app/chat/headless.py app/api/agent_runtime.py tests/test_agent_artifacts_api.py docs/api-reference.md tests/test_documentation_api_triple_surface.py
git commit -m "feat(api): harvest sandbox artifacts to object store + download endpoints"
```

---

### Task 6: Webhooks — SSRF-hardened registration + signed worker delivery

**Files:**
- Create: `app/api/agent_webhooks.py` (registration router), `app/chat/webhook_delivery.py` (SSRF guard + HMAC signer + delivery), register a `webhook-deliver` job kind in `app/worker/kinds.py`
- Modify: `app/main.py` (router), `app/api/agent_runtime.py` (enqueue delivery on job terminal state), `docs/api-reference.md`, ratchet files
- Test: `tests/test_agent_webhooks_api.py`, `tests/test_webhook_delivery.py`

**Interfaces:**
- Consumes: `agent_webhooks_repo()` (Task 2), jobs runtime (`register_kind`, `jobs_repo().enqueue`), `require_session_token` (registration is owner-auth only — mutation of standing config).
- Produces:
  - SSRF guard `webhook_delivery.validate_webhook_url(url: str) -> None` — raises `ValueError` unless: scheme is `https` (http rejected); host resolves (via `socket.getaddrinfo`) to NO address in a private/loopback/link-local/ULA/metadata range (`ipaddress.ip_address(...).is_private / is_loopback / is_link_local / is_reserved` plus explicit `169.254.169.254` and `fd00::/8`); reject on any resolution error. Comment the motivation abstractly (cloud metadata + internal ranges), vendor-neutral.
  - HMAC signer `webhook_delivery.sign(secret: str, body: bytes) -> str` → `"sha256=" + hmac_hex`. Delivery adds header `x-agnes-signature`.
  - `webhook_delivery.deliver(webhook_row: dict, payload: dict) -> bool` — re-validate URL at send time (DNS-rebind defense), POST JSON with signature header, short timeout, NO redirect following; returns success. On failure `record_failure`; after N (config `agent_api.webhook_max_failures`, default 5) consecutive → `disable`. On success `record_success`.
  - `webhook-deliver` job kind (gateway-affine not required — pure HTTP; register unconditionally) with bounded retries (`retry_in_seconds` backoff) — the worker calls `deliver`.
  - Registration endpoints (owner auth, per agent):
    - `GET  /api/v1/agents/{slug}/webhooks` → cursor envelope (secret NOT returned after create).
    - `POST /api/v1/agents/{slug}/webhooks` `{url, events?}` → `201 {id, secret}` (secret shown once; `validate_webhook_url` at create → 400 `webhook_url_forbidden` on SSRF-denied).
    - `DELETE /api/v1/agents/{slug}/webhooks/{id}` → `204`.
  - Enqueue: when an `agent_response` job reaches `completed`/`failed`, enqueue a `webhook-deliver` job per `list_active_for_event(agent_id, f"job.{state}")` with the job result as payload.

- [ ] **Step 1: Write the failing tests** — `validate_webhook_url`: rejects `http://`, `https://169.254.169.254/...`, `https://localhost/...`, a hostname resolving to `127.0.0.1` (monkeypatch getaddrinfo), `https://10.x`; accepts a public host (monkeypatch getaddrinfo → public IP). `sign` deterministic + verifiable. `deliver`: success path records success (fake httpx); failure increments and disables at threshold (monkeypatch repo). API: POST webhook returns secret once; SSRF URL → 400; GET omits secret; DELETE 204; cross-owner → 404. Enqueue: monkeypatch jobs_repo.enqueue, assert a `webhook-deliver` job enqueued per active webhook on job completion.

- [ ] **Step 2–4: fail → implement → pass** — `.venv/bin/pytest tests/test_agent_webhooks_api.py tests/test_webhook_delivery.py tests/test_route_auth_guard.py -q`.

- [ ] **Step 5: Commit**

```bash
git add app/api/agent_webhooks.py app/chat/webhook_delivery.py app/worker/kinds.py app/main.py app/api/agent_runtime.py tests/test_agent_webhooks_api.py tests/test_webhook_delivery.py docs/api-reference.md tests/test_documentation_api_triple_surface.py
git commit -m "feat(api): SSRF-hardened signed webhooks with worker-backed delivery"
```

---

### Task 7: Structured output (`response_format: json_schema`)

**Files:**
- Modify: `app/api/agent_runtime.py` (`AgentResponseRequest` already has a `response_format` field accepted-but-unenforced from V1a — wire enforcement; if absent, add it), `app/chat/headless.py` (thread schema into the run + validate final answer)
- Create: `app/chat/structured_output.py` (schema validation helper)
- Test: `tests/test_agent_structured_output.py`

**Interfaces:**
- Consumes: the run's final `answer` string (from `HeadlessSink.answer`), the caller's `response_format`.
- Produces:
  - `structured_output.validate(answer: str, response_format: dict) -> tuple[bool, Any, Optional[str]]` — when `response_format == {"type": "json_schema", "schema": {...}}`: parse `answer` as JSON, validate against the JSON Schema (use `jsonschema` if already a dep — `grep jsonschema pyproject.toml`; else validate structurally with a minimal check and note the limitation). Returns `(ok, parsed_or_none, error_or_none)`.
  - `/responses` and `/sessions/{id}/messages`: when `response_format` is present and validation fails → `422 {"code": "schema_validation_failed", "message": ...}`; when it succeeds, the response's `answer` stays the raw string but a `parsed` field carries the validated object. When absent, behavior unchanged.
  - The system prompt / run is nudged to emit JSON (append a directive to the message: "Respond ONLY with JSON matching this schema: <schema>") — document that V1b relies on prompt-steering + post-validation, not constrained decoding (a V2 note).

- [ ] **Step 1: Write the failing tests** — `validate`: valid JSON matching schema → `(True, obj, None)`; malformed JSON → `(False, None, "...")`; JSON violating schema → `(False, None, "...")`; no response_format → helper not called. API (fake run returning a canned answer): matching schema → 200 with `parsed`; violating → 422 `schema_validation_failed`.

- [ ] **Step 2–4: fail → implement → pass** — `.venv/bin/pytest tests/test_agent_structured_output.py -q`.

- [ ] **Step 5: Commit**

```bash
git add app/chat/structured_output.py app/api/agent_runtime.py app/chat/headless.py tests/test_agent_structured_output.py
git commit -m "feat(api): structured JSON output (response_format json_schema) with server-side validation"
```

---

### Task 8: Usage endpoint + CLI/MCP parity + CHANGELOG + docs

**Files:**
- Modify: `app/api/agent_runtime.py` (`GET /api/v1/agents/{slug}/usage`), `cli/commands/agent.py` (`agnes agent usage`, `agnes agent webhooks` subcommands, `agnes agent session` for interactive-ish send if trivial — otherwise defer session CLI to V1c), `app/api/mcp/foundation_tools.py` (`agent_usage` MCP tool), `tests/test_documentation_api_triple_surface.py` (classify new routes), `CHANGELOG.md`, `docs/api-reference.md`, `CLAUDE.md`
- Test: `tests/test_agent_usage_api.py`, extend `tests/test_cli_agent.py`, `tests/test_mcp_tool_parity.py`

**Interfaces:**
- Consumes: `llm_usage_repo()` (`month_total_tokens`, `list_for_agent`), everything above.
- Produces:
  - `GET /api/v1/agents/{slug}/usage?period=YYYY-MM` → `{period, agent_slug, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, total_tokens, budget_limit, budget_remaining}` (owner/agent-PAT scoped; default period = current month). Usage object shape mirrors Anthropic (spec §3).
  - CLI: `agnes agent usage <slug> [--period YYYY-MM] [--json]`; `agnes agent webhooks list|add|delete <slug> ...`.
  - MCP: `agent_usage` foundation tool (maps the usage endpoint).
  - Triple-surface: move `/api/v1/agents/{slug}/usage` into `_COHORT` (CLI + MCP both exist); session/SSE/webhook/artifact routes stay `_EXEMPT` with permanent reasons (streaming/binary/standing-config have no MCP analogue).

- [ ] **Step 1: Write failing tests** — usage endpoint returns the summed shape from seeded `llm_usage` rows + budget fields; period filter; owner scoping. CLI usage/webhooks render + `--json`. MCP parity green with `agent_usage` added.

- [ ] **Step 2–4: fail → implement → pass** — `.venv/bin/pytest tests/test_agent_usage_api.py tests/test_cli_agent.py tests/test_mcp_tool_parity.py tests/test_documentation_api_triple_surface.py tests/test_api_docs_coverage.py -q`.

- [ ] **Step 5: CHANGELOG bullets** (`## [Unreleased]` → Added):

```markdown
- Agent sessions: multi-message conversations over `POST /api/v1/agents/{slug}/sessions`
  + `POST /api/v1/sessions/{id}/messages` (SSE, AG-UI event vocabulary),
  `cancel`, history, and `DELETE`.
- Agent runs can emit downloadable artifacts: files produced in the sandbox
  are harvested to the object store (`GET /api/v1/sessions/{id}/artifacts`).
- Agent webhooks: SSRF-hardened, HMAC-signed delivery of job-completion
  events via the worker runtime, auto-disabled after repeated failures.
- Structured output: `response_format: {type: json_schema}` validates the
  answer server-side (`422 schema_validation_failed` on mismatch).
- `GET /api/v1/agents/{slug}/usage` + `agnes agent usage` + `agent_usage`
  MCP tool report per-agent monthly token usage against budget.
```

- [ ] **Step 6: Full suite**

Run: `.venv/bin/pytest tests/ --tb=short -n auto -q`
Expected: PASS (known-environmental `test_cli_init` excepted; verify any other failure via `git stash`).

- [ ] **Step 7: Commit**

```bash
git add app/api/agent_runtime.py cli/commands/agent.py app/api/mcp/foundation_tools.py tests/test_agent_usage_api.py tests/test_cli_agent.py tests/test_mcp_tool_parity.py tests/test_documentation_api_triple_surface.py CHANGELOG.md docs/api-reference.md CLAUDE.md
git commit -m "feat(api,cli,mcp): agent usage endpoint + parity + V1b changelog"
```

---

## Execution notes

- Task order is dependency order: 1→2 (schema→repos), 3 (SSE mapper, independent), 4 (sessions, needs 3), 5 (artifacts, needs 2), 6 (webhooks, needs 2), 7 (structured output, independent of 4-6), 8 (parity+docs, last). Tasks 3 and 7 can run in parallel with others.
- Before the PR: `/agnes-review` on the full diff, scan for customer-specific tokens, release-cut per `docs/RELEASING.md`.
- This stacks on the V1a branch; the release-cut decision (one PR for V1a+V1b+V1c, or split) is the user's at merge time.
