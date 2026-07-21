# Agent Profiles & Agent-as-API — Design

Date: 2026-07-21
Status: validated design (brainstorm complete, externally reviewed API shape)

## Motivation

Users compose a personal "stack" in Agnes today — marketplace plugins/skills,
per-user MCP connections, RBAC-scoped data tables, corporate memory — but the
only ways to *use* that composition are the interactive surfaces (web chat,
Slack, Telegram, MCP). There is no way to:

1. name and scope a specialized agent ("Sales reporter") distinct from "me",
2. hand a single credential to an automation (cron, n8n, another system) and
   get answers from that agent programmatically, or
3. run the composed agent from a terminal with LLM billing handled centrally
   (the user never holds an Anthropic key).

This design adds **named agent profiles**, an **agent-as-API** runtime
surface, and extends the existing LLM broker into a per-agent **token
triage** layer (model policy + budget). A terminal thin client rides the same
API; a local-Claude-Code "power mode" is a planned V2 on the same proxy.

## Core decisions (settled during brainstorm)

- **Agent-as-API is the core deliverable**, not an LLM passthrough proxy.
- **Named agent profiles** are a first-class entity; the user's existing
  stack becomes their implicit **default agent** ("my stack is just my
  default agent"). Web chat behavior is unchanged — it spawns the default
  agent.
- **An agent can never exceed its owner**: effective capability =
  owner's RBAC grants ∩ agent scope, recomputed at spawn and at each message
  boundary.
- **No new runtime**: the API reuses ChatManager → sandbox execution as a new
  `"api"` surface. Latency is mitigated by an optional warm pool and the
  existing pause/resume, never by sharing state across callers.
- **Single LLM enforcement point**: the existing secret broker
  (`app/api/broker.py`) is extended with per-agent model policy, usage
  ledger, and budget enforcement. The API layer only reads/reports.
- The API surface was reviewed against 2025–26 industry practice (OpenAI
  Responses, A2A, AG-UI, Anthropic API conventions); all MUST-FIX findings
  are incorporated below.

## 1. Data model

New tables (DuckDB migration step + Alembic sibling, dual repos
`src/repositories/agents.py` + `agents_pg.py`, factory entries, cross-engine
contract tests — standard dual-backend discipline):

```
agents
├── id, owner_user_id, name, slug, description
├── system_prompt          TEXT       -- persona; materializes as session CLAUDE.md
├── model                  TEXT NULL  -- NULL = instance default
├── token_budget_monthly   BIGINT NULL-- NULL = no per-agent cap (instance policy applies)
├── plugins_mode           TEXT       -- 'all' | 'selected'
├── connections_mode       TEXT       -- 'all' | 'selected'
├── tables_mode            TEXT       -- 'all' | 'selected'
├── memory_mode            TEXT       -- 'all' | 'selected'  (corporate-memory domains)
├── is_default             BOOL       -- exactly one per user (lazily seeded)
└── created_at, updated_at, deleted_at

agent_scope (agent_id, item_type, item_id)
    item_type ∈ ('plugin', 'connection', 'table', 'memory_domain')

agent_memories (id, agent_id, content, source_session_id, created_at, archived_at)

llm_usage (id, agent_id, user_id, session_id, model,
           input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
           created_at)
```

- `'all'` mode mirrors the owner's *live* stack (installs propagate);
  `'selected'` restricts to the enumerated `agent_scope` rows.
  **API-created agents default to `'selected'`** (reproducibility for
  integrations); the lazily-seeded default agent uses `'all'` everywhere with
  an empty system prompt — bit-for-bit today's chat behavior.
- `personal_access_tokens` gains a nullable `agent_id` column. NULL = the
  existing user PAT, behavior unchanged. Non-NULL = **agent PAT**:
  authenticates as the owner but is constrained to that agent.
- Effective composition is computed at session spawn as
  (owner RBAC grants) ∩ (agent scope), and **persisted as a per-session
  effective-scope snapshot** for audit ("what could this run see?"). An
  `agent_config_hash` derived from the resolved config is returned on every
  response so integrators can detect drift.

### Per-agent memory (V1)

Two halves:

- **Corporate-memory read scoping**: `memory_mode` + `agent_scope`
  `item_type='memory_domain'` restrict which knowledge domains flow into the
  session bundle.
- **Private agent memory**: `agent_memories` is the agent's own notebook —
  outside the corporate-memory governance pipeline (private to owner+agent;
  nothing to approve). Materialized into the session workdir at spawn
  (newest-first within a token budget) alongside the persona CLAUDE.md. The
  agent writes via an API-backed "remember" tool (size-capped,
  rate-limited) — never directly to the DB. The owner inspects/deletes via
  builder UI and API. Promotion of an agent memory into governed corporate
  memory is an explicit V2 follow-up; the provenance columns leave the seam.

Security note: agent memories are agent-written content re-materialized into
future prompts — an injection surface. The owner-inspection UI is a security
control, not a convenience.

## 2. API surface

Single versioned root `/api/v1`. Runtime endpoints accept **Bearer agent-PAT**
(and owner session auth for testing); management endpoints require owner
session auth and **hard-reject agent PATs**. The agent is **explicit in the
path**; the agent PAT is an authorization *constraint* validated against the
path ("this token may only act as agent X") — never the addressing mechanism.

### Runtime

```
POST /api/v1/agents/{slug}/responses
     {input, response_format?, session_id?, previous_response_id?,
      background?, timeout_s?, metadata?}
  → 200 {answer, session_id, response_id, usage, agent_config_hash, request_id}
  → 202 {job_id}                       (background, or timeout_s elapsed)

POST /api/v1/agents/{slug}/sessions        → {session_id}
POST /api/v1/sessions/{id}/messages        → SSE stream
POST /api/v1/sessions/{id}/cancel          (≠ DELETE — preserves history)
GET  /api/v1/sessions/{id}                 → state + history
GET  /api/v1/sessions/{id}/artifacts       (+ per-artifact download)
DELETE /api/v1/sessions/{id}

GET  /api/v1/jobs/{id}                     → job state machine
POST /api/v1/jobs/{id}/cancel
GET  /api/v1/agents/{slug}/usage?period=
```

- **One-shot is stateless by default** — every `/responses` call without
  `session_id`/`previous_response_id` gets a fresh sandbox. Continuity is
  explicit opt-in (OpenAI Responses conversation-state pattern). Warm-sandbox
  reuse across unrelated calls is forbidden (cross-caller data leakage,
  prompt-injection persistence).
- **`timeout_s` bounds the wait, not the run**: on expiry the same
  still-running execution degrades to `202 {job_id}`.
- **Job lifecycle** is an explicit state machine:
  `queued → in_progress → (completed | failed | cancelled | requires_action)`
  with documented result-retention TTL. `requires_action` and
  `POST /api/v1/sessions/{id}/submit` are **reserved in V1** (auto-approve)
  so human-in-the-loop approval can land without breaking clients.
- **Webhooks** (HMAC-signed, per-registration secret) fire on job terminal
  states alongside polling.
- **`Idempotency-Key`** header accepted on every side-effectful POST
  (`/responses`, `/sessions`, `/messages`); stored result replayed within a
  TTL (Stripe semantics).
- **Structured output**: `response_format: {type: "json_schema", schema}`
  validated server-side; failure surfaces `schema_validation_failed`.
- **Cursor pagination** everywhere from day one:
  `{data, has_more, next_cursor}` + `limit`/`after`/`order`.
- `request_id` on every response (header + error body); client-supplied
  `metadata` echoed back and logged into the audit trail.

### Streaming wire format

SSE with a **typed event vocabulary aligned with AG-UI**
(`RUN_STARTED/FINISHED/ERROR`, `TEXT_MESSAGE_START/CONTENT/END`,
`TOOL_CALL_START/ARGS/END`, artifact event, terminal event carrying usage) —
not a bespoke schema. Every event carries a monotonic `id:` so
`Last-Event-ID` resume can ship later (V1.1) without a wire-format break.

### Management (owner auth only)

```
GET/POST         /api/v1/agents
GET/PUT/DELETE   /api/v1/agents/{id}
PUT              /api/v1/agents/{id}/scope
POST             /api/v1/agents/{id}/tokens        (issue agent PAT)
GET/PATCH/DELETE /api/v1/agents/{id}/memories
```

Every endpoint gets `agnes agent …` CLI and MCP-tool parity per the API
coverage ratchet.

## 3. Token triage (LLM proxy)

Extends the existing secret broker (`app/api/broker.py`,
`POST /api/broker/anthropic`) — already the single point where the real
`ANTHROPIC_API_KEY` is injected server-side while sandboxes hold only a dummy
key + opaque ticket. The broker is the **only enforcer**; the API layer
reads/reports.

1. **Per-agent model policy**: sessions carry `agent_id`; the broker checks
   the request's `model` against (agent's main model + instance-wide utility
   model allowlist — Claude Code legitimately uses helper models for
   auto-title/background tasks). Violation → 403 `model_not_allowed`.
2. **Usage ledger**: the broker parses usage from Anthropic responses
   (streaming: `message_start`/`message_delta`) and writes `llm_usage` rows.
   Feeds `GET …/usage`, admin dashboards, and budget checks. Align with the
   existing `usage_repo` — no second bookkeeping system.
3. **Budget enforcement**: cheap pre-forward check against a cached monthly
   aggregate. Exceeded → `429 budget_exhausted` **without** `retry-after`
   (so SDKs don't auto-retry); transient limits → `429 rate_limit_exceeded`
   **with** `retry-after`. `x-agnes-budget-*` headers on every response.
   Cascading limits (agent ∧ instance) — first hit wins, error names it.
4. **V2 readiness**: a second authenticator (agent PAT) on the same
   enforcement path enables local Claude Code
   (`ANTHROPIC_BASE_URL=<agnes>/api/v1/llm` + agent PAT). Designed now,
   enabled in V2.

Usage object shape mirrors Anthropic:
`{input_tokens, output_tokens, cache_read_input_tokens,
cache_creation_input_tokens, total_tokens}` — in sync bodies and in the
terminal SSE event.

## 4. Runtime & latency

No new runtime. The API is a new ChatManager surface (`"api"`); spawn is
parameterized by the agent profile — persona CLAUDE.md + agent-memory
materialization + scope-filtered skills/connections/tables — mechanically the
same as today's spawn-time `ChatProfile`s, sourced from the DB instead of
constants.

- Stateless `/responses` = always a fresh sandbox (isolation over latency).
- **Warm pool** of pre-booted generic sandboxes (claimed → agent context
  poured in → first token much faster). Internal only; config
  `agent_api.warm_pool_size` (default 0 = off so small instances don't pay
  for idle sandboxes). A claimed sandbox is always fresh, never returned.
- **Sessions** stay warm between messages via existing pause/resume
  (`sandbox_paused_at`); TTL + max lifetime from instance config.
- **Concurrency**: existing `ConcurrencyCapHit` extended with a per-PAT /
  per-agent cap (default ~2 concurrent runs) → `429 rate_limit_exceeded`
  with `retry-after`.

## 5. Security & audit

- **Agent ⊆ owner** enforced at spawn *and* at each message boundary; a
  grant revoked mid-session stops applying at the next message (in-flight
  runs drain).
- **Agent-PAT hygiene**: distinctive prefix `agnes_agt_`, secret shown once,
  `expires_at` / `last_used_at`, hard-rejected on management endpoints and
  every non-agent-API surface, revoked in cascade on agent delete.
- **Audit**: every action logged as "user X via agent Y"; per-session
  effective-scope snapshot persisted; `agent_config_hash` on every response.
- **Runtime gate**: agent-API inherits the existing `ResourceType.CHAT`
  resource grant — no chat access, no agent runs.
- Webhooks HMAC-signed; idempotency replay store has a TTL; agent-memory
  writes size-capped and rate-limited.

## 6. Testing

- Dual-backend contract tests for all new repos (`agents`, `agent_scope`,
  `agent_memories`, `llm_usage`) + both migration ladders
  (`tests/test_db_schema_version.py` gate).
- Auth matrix: agent PAT × user PAT × session × wrong-agent-in-path ×
  management rejection.
- Scope intersection incl. message-boundary revocation.
- Budget/rate-limit semantics (429 variants, headers), idempotency replay.
- **Golden tests for the SSE event schema** — the wire format is a contract.
- E2E via the existing `AGNES_E2E` + `FAKE_AGENT` harness: one-shot, session,
  artifacts; plus a real user-path session against a live instance.
- API coverage ratchet: CLI + MCP parity for every endpoint.

## Phasing

**V1**: `agents` entity + builder UI + agent PATs + agent-API (one-shot +
sessions + jobs + webhooks + artifacts + structured output) + broker
extensions (model policy, ledger, budget) + `agnes chat` terminal thin
client over the public API (no privileged backchannel) + per-agent private
memory.

**V1.1**: SSE resume (`Last-Event-ID`), usage dashboards polish.

**V2**: local Claude Code "power mode" (`agnes agent up`: bootstrap workspace
from the agent profile, LLM via PAT-authenticated proxy), agent sharing
across users/groups, `requires_action` human-in-the-loop approval flow,
agent-memory promotion into governed corporate memory, A2A agent card
(`/.well-known/agent-card.json`), OpenAI-compatible shim
(`model: "agnes/<slug>"`).

## Open questions (deliberately deferred)

- Per-agent memory token budget within the session bundle (fixed vs
  configurable).
- Warm-pool sizing heuristics beyond a static config value.
- Whether the default agent should be editable (persona on "me") or locked.
