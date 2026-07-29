# Agent Profiles & Agent-as-API — Design

Date: 2026-07-21 (revision 2, 2026-07-22 — post codebase-feasibility + adversarial review)
Status: validated design (brainstorm complete; API shape reviewed against 2025–26
industry practice; revision 2 folds in a codebase-grounded feasibility review and
an adversarial security/consistency review)

Revision 3 (post-implementation, 2026-07-23): amends §1's `idempotency_keys`
column list and the `model` semantics (§1 + §3) to match what actually
shipped — see the inline notes below.

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
API; a local-Claude-Code "power mode" is a planned follow-up on the same proxy.

## Core decisions (settled during brainstorm)

- **Agent-as-API is the core deliverable**, not an LLM passthrough proxy.
- **Named agent profiles** are a first-class entity; the user's existing
  stack becomes their implicit **default agent** ("my stack is just my
  default agent"). Web chat behavior is unchanged — it spawns the default
  agent.
- **An agent can never exceed its owner**: effective capability =
  owner's RBAC grants ∩ agent scope. The intersection is applied live at the
  authorization seams (see §4), so it can only shrink relative to spawn.
- **No new runtime**: the API reuses ChatManager → sandbox execution as a new
  `"api"` surface. Latency is mitigated by an optional warm pool and the
  existing pause/resume, never by sharing state across callers.
- **Single LLM-policy enforcement point**: the existing secret broker
  (`app/api/broker.py`) is the *only* enforcer of LLM model policy and token
  budget (other concerns — concurrency caps, schema validation, idempotency —
  are enforced at the API/ChatManager layer where they belong). The API layer
  reads/reports budgets, never enforces them.
- **Reuse over reinvention**: the existing durable jobs runtime
  (`app/api/jobs.py`, `src/repositories/jobs*.py`, `app/worker/`) backs
  background execution and webhook delivery; existing `require_session_token`
  backs management-endpoint auth; existing object store backs artifacts.

## 1. Data model

New tables (DuckDB migration step `_v95_to_v96` + Alembic sibling `0043_*`,
dual repos `src/repositories/agents.py` + `agents_pg.py`, factory entries in
`src/repositories/__init__.py`, cross-engine contract tests
`tests/db_pg/test_agents_contract.py` — standard dual-backend discipline;
must pass `tests/test_backend_split_guard.py` and
`tests/test_route_auth_guard.py`):

```
agents
├── id, owner_user_id, name, slug, description
│      -- slug: unique per owner, immutable after creation, tombstoned on
│      -- delete (never reused) — integrations and PATs must not silently
│      -- rebind to a new agent under an old slug
├── system_prompt          TEXT       -- persona; materializes as session CLAUDE.md
├── model                  TEXT NULL  -- NULL = no model policy (revision 3: there is
│                                     -- no instance-wide default model anywhere in
│                                     -- the codebase — the sandbox's Claude Code CLI
│                                     -- picks its own; see §3 mechanics #1)
├── token_budget_monthly   BIGINT NULL-- NULL = no per-agent cap (instance policy applies)
├── plugins_mode           TEXT       -- 'all' | 'selected'
├── connections_mode       TEXT       -- 'all' | 'selected'
├── tables_mode            TEXT       -- 'all' | 'selected'
├── memory_mode            TEXT       -- 'all' | 'selected'  (corporate-memory domains)
├── memory_write_mode      TEXT       -- 'off' | 'propose' | 'auto' (default 'propose')
├── is_default             BOOL       -- exactly one per user (lazily seeded, undeletable)
└── created_at, updated_at, deleted_at

agent_scope (agent_id, item_type, item_id)
    item_type ∈ ('plugin', 'connection', 'table', 'memory_domain')

agent_memories (id, agent_id, content, source_session_id, status,
                created_at, activated_at, archived_at)
    status ∈ ('pending', 'active', 'archived')

llm_usage (id, agent_id, user_id, session_id, model,
           input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
           created_at)

agent_scope_snapshots (id, session_id, agent_id, effective_scope JSON, created_at)
    -- append-only: one row at spawn + one per recompute that differs

idempotency_keys (key, owner_user_id, agent_id, request_hash,
                  response_body TEXT, status_code INTEGER, created_at, expires_at)
    -- revision 3: shipped as the response's raw body + HTTP status code,
    -- not an object-store `response_ref` — the response is small enough
    -- (a single agent-API answer) that storing it inline in the row is
    -- simpler than harvesting it into the object store like session
    -- artifacts.
```

Changes to existing tables:

- `personal_access_tokens` gains a nullable `agent_id` column. NULL = the
  existing user PAT, behavior unchanged. Non-NULL = **agent PAT**. Note: PATs
  are signed JWTs (`typ="pat"`), so there is no distinctive wire prefix —
  agent PATs are minted with `typ="agent_pat"` + an `agent_id` claim, and the
  existing `prefix` column keeps serving UI identification. The existing
  unused `scopes` column is left alone (future fine-grained scopes).
- `chat_sessions` gains a nullable `agent_id` column (+ `ChatSession`
  dataclass + both repos + both migration ladders). This is mandatory, not
  optional: the current profile-per-session map is in-memory only and does
  not survive restart/resume, and the broker resolves identity from
  `chat_sessions` — `agent_id` must ride there to reach enforcement.
  **DuckDB index hazard**: no secondary index on `chat_sessions.agent_id`
  (documented DuckDB FK+index bug; the 2026-07 ART-index incident applies) —
  index design for all new tables follows the same conservative rule.

Modes: `'all'` mirrors the owner's *live* stack (installs propagate);
`'selected'` restricts to the enumerated `agent_scope` rows.
**API-created agents default to `'selected'`** (reproducibility for
integrations); the lazily-seeded default agent uses `'all'` everywhere with an
empty system prompt — bit-for-bit today's chat behavior. The default agent
cannot be deleted; if missing it is re-seeded lazily.

An `agent_config_hash` derived from the **spawn-time** resolved config is
returned on every response. Sessions pin their spawn config (persona, memory
materialization, hash stay stable for the session's lifetime); live scope
recomputation acts purely as a *security floor* — capability can only shrink
mid-session, never drift.

### Per-agent memory

Two halves:

- **Corporate-memory read scoping**: `memory_mode` + `agent_scope`
  `item_type='memory_domain'` restrict which knowledge domains flow into the
  session bundle.
- **Private agent memory**: `agent_memories` is the agent's own notebook —
  outside the corporate-memory governance pipeline (private to owner+agent).
  Materialized into the session workdir at spawn (newest-first within a token
  budget) alongside the persona CLAUDE.md. The agent writes via an API-backed
  "remember" tool (size-capped, rate-limited) — never directly to the DB.

**Memory poisoning is a real attack channel** (prompt injection in queried
data → poisoned memory → persistent cross-session injection into every future
run, including other integrations' one-shots), and an owner-inspection UI is
not an effective control for cron-driven agents. Therefore `memory_write_mode`:

- `off` — no remember tool exposed.
- `propose` (default) — writes land as `status='pending'`; only
  owner-approved (`active`) rows materialize into future sessions.
- `auto` — writes are immediately active; the owner opts into this
  explicitly per agent.

Every memory row carries provenance (`source_session_id` → surface/channel),
shown in the inspection UI. Promotion of an agent memory into governed
corporate memory is an explicit follow-up; the provenance columns leave the
seam.

## 2. API surface

Single versioned root `/api/v1`. (Naming rationale: the repo's existing
`/api/v2/*` is the Arrow *data-plane* namespace, unrelated to this surface;
versioned roots are per-plane. `/api/v1` is the agent-runtime plane and does
not imply a predecessor.)

Runtime endpoints accept **Bearer agent PAT** — and also the owner's other
credentials (session auth, plain user PAT), since the owner may address any
of their own agents; the agent PAT is an authorization *constraint*
("this token may only act as agent X") validated against the path.
Management endpoints require interactive owner auth (reuse the existing
`require_session_token`, which already rejects all PATs) — agent PATs are
additionally **hard-rejected on every non-agent-API surface**, implemented in
the shared PAT resolver, explicitly including the git smart-HTTP marketplace
channel and `/marketplace.zip` (V1: rejected there; the local power mode may
later allow them with agent-filtered content).

Auth matrix (normative):

| Credential | Runtime `/agents/{slug}/…` | Management | Other API surfaces |
|---|---|---|---|
| Agent PAT (this agent) | ✓ | ✗ (hard reject) | ✗ (hard reject) |
| Agent PAT (other agent) | ✗ 403 | ✗ | ✗ |
| User PAT (agent_id NULL) | ✓ (any own agent) | ✗ (PATs rejected today) | as today |
| Owner session | ✓ | ✓ | as today |
| Admin (god-mode) | ✗ (not their agent) | ✓ read/inspect for governance; ✗ cannot mint PATs for others' agents | as today |

**Agent PATs cannot be issued for `'all'`-mode agents** (including the
default agent) — a token for an all-mirroring agent is a full-user credential
wearing a "constrained" badge, and it silently gains capability with every
plugin the owner installs. Token issuance requires all four modes
`'selected'`. Slug resolution is per-owner (unambiguous under every allowed
credential). Runtime paths accept slug or id.

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

GET/POST/DELETE /api/v1/agents/{slug}/webhooks   (owner auth; see webhook rules)
```

- **One-shot is stateless by default** — every `/responses` call without
  continuation gets a fresh sandbox. Continuity is explicit opt-in.
  **Continuation semantics (normative)**: `session_id` is the continuation
  mechanism; `previous_response_id` is pure sugar that resolves to the
  session that produced it (404 if the session is expired/deleted; 409 if
  both are supplied and disagree). Warm-sandbox reuse across unrelated calls
  is forbidden (cross-caller data leakage, prompt-injection persistence).
- **`timeout_s` bounds the wait, not the run**: on expiry the same
  still-running execution degrades to `202 {job_id}`.
- **Jobs reuse the existing durable jobs runtime** (`jobs` table, worker
  registry, its `idempotency_key` dedup) — no parallel queue. The public
  state machine `queued → in_progress → (completed | failed | cancelled |
  requires_action)` maps onto the existing internal statuses at the API edge.
  Documented result-retention TTL. `requires_action` and
  `POST /api/v1/sessions/{id}/submit` are **reserved in V1**; additionally an
  agent can be marked `on_approval_required='fail'` (hard-fail instead of
  auto-approve on approval-class actions) for owners who don't want headless
  auto-approval. A mid-run `429 budget_exhausted` from the broker surfaces as
  job `failed` with `error.code = "budget_exhausted"`.
- **Webhooks**: registration is owner-auth only, per agent. SSRF hardening is
  mandatory: https-only, deny-by-default for private/link-local/metadata
  address ranges (resolve-and-pin DNS, no redirect following), HMAC-signed
  payloads with per-registration secret. Delivery rides the worker runtime:
  exponential-backoff retries, max attempts, auto-disable after N consecutive
  failures (surfaced on the registration resource), no ordering guarantee.
  A dead endpoint never blocks job finalization.
- **`Idempotency-Key`**: accepted on `/responses` and `/sessions`. Store =
  `idempotency_keys` table (works across replicas/restarts), key scoped to
  (owner, agent) and **bound to a request-body hash** — same key + different
  body → 409 (Stripe's actual semantics); replay returns the stored
  response within the TTL; expired rows are reaped. Not accepted on the SSE
  `/messages` endpoint (replaying a stream is ill-defined; recorded history
  is available via `GET /sessions/{id}`).
- **Structured output**: `response_format: {type: "json_schema", schema}`
  validated server-side; failure surfaces `schema_validation_failed`.
- **Artifacts**: files produced in the sandbox workdir are harvested into the
  existing object store when a run completes (before the idle/paused reapers
  tear the sandbox down), with per-session size cap and retention TTL from
  instance config. Download authorization: owner credentials, or the agent
  PAT bound to the same agent that owns the session — never another agent's
  PAT.
- **Cursor pagination** everywhere from day one:
  `{data, has_more, next_cursor}` + `limit`/`after`/`order`.
- `request_id` on every response (header + error body); client-supplied
  `metadata` echoed back and logged into the audit trail.

### Streaming wire format

SSE with a **typed event vocabulary aligned with AG-UI**
(`RUN_STARTED/FINISHED/ERROR`, `TEXT_MESSAGE_START/CONTENT/END`,
`TOOL_CALL_START/ARGS/END`, artifact event, terminal event carrying usage) —
not a bespoke schema. Every event carries a monotonic `id:`; **monotonicity
scope is pinned now: per-session** (so `Last-Event-ID` resume across a
reconnect works mid-message when it ships in V1.1) — the wire format does not
change later.

### Management (owner auth only)

```
GET/POST         /api/v1/agents
GET/PUT/DELETE   /api/v1/agents/{id}
PUT              /api/v1/agents/{id}/scope
POST             /api/v1/agents/{id}/tokens        (issue agent PAT; 'selected'-mode agents only)
GET/PATCH/DELETE /api/v1/agents/{id}/memories      (+ approve pending → active)
```

Every endpoint gets `agnes agent …` CLI and MCP-tool parity per the API
coverage ratchet.

## 3. Token triage (LLM proxy)

Extends the existing secret broker (`app/api/broker.py`,
`POST /api/broker/anthropic`) — already the single point where the real
credential is injected server-side while sandboxes hold only a dummy key +
opaque ticket. The broker is the only enforcer **of LLM model policy and
token budget**; the API layer reads/reports.

Reality constraints folded in from code review:

- The broker **buffers** upstream responses (httpx reads the full body before
  returning), so usage extraction parses the buffered (SSE) body — trivially
  feasible today; if true streaming passthrough ever lands, parsing must
  become incremental. Model-policy checks parse the agent-supplied request
  body (currently treated as opaque bytes).
- Enforcement must cover **all three upstream credential modes**: static key,
  workload-identity federation, and the external LLM dispatcher. In
  dispatcher mode the dispatcher keeps its own upstream ledger; Agnes's
  `llm_usage` remains **authoritative for agent budgets** and the two are
  never summed.
- Broker tickets stay `(session_id, scope, expires_at)`; `agent_id` is
  resolved from the persisted `chat_sessions.agent_id` (see §1), not carried
  in the ticket.

Mechanics:

1. **Per-agent model policy** (revision 3: shipped semantics — there is no
   instance-wide default model anywhere in the codebase; the sandbox's
   Claude Code CLI picks its own unless a session profile overrides it):
   `NULL` means **no model policy at all** — `check_model` allows the
   request without even inspecting the body. Enforcement only activates
   once the owner pins a model on the agent, in which case the allowed set
   is `{agent.model} ∪ instance-wide utility-model allowlist` (Claude Code
   legitimately uses helper models for auto-title/background tasks).
   Violation → 403 `model_not_allowed`. **Utility-model usage is
   attributed to the agent's budget** (so allowlisted models are not free
   tokens; a compromised run
   can at worst burn the agent's own budget).
2. **Usage ledger**: parse usage from Anthropic responses (streaming bodies:
   `message_start`/`message_delta` events) and record `llm_usage` rows.
   **Hot-path discipline (DuckDB single-writer reality)**: rows are
   accumulated in memory and flushed in batches (periodic + on session end),
   never one synchronous app-state write per LLM call. Retention: raw rows
   for a configurable window, then monthly rollups. **Bookkeeping split**:
   `llm_usage` is authoritative for budget enforcement (real-time);
   the existing `usage_session_summary` (session-jsonl pipeline) remains the
   analytics ledger — dashboards must consume one or the other, never both.
3. **Budget enforcement**: pre-forward check against a cached monthly
   aggregate; cache invalidation rides the existing coordination layer
   (memory/redis backends), and the documented overshoot bound is
   (staleness window × per-agent concurrency cap) — acceptable for a
   soft-budget product; hard-real-time budgets are a non-goal. Exceeded →
   `429 budget_exhausted` **without** `retry-after` (so SDKs don't
   auto-retry); transient limits → `429 rate_limit_exceeded` **with**
   `retry-after`. `x-agnes-budget-*` headers on every response. Cascading
   limits (agent ∧ instance) — first hit wins, error names it.
4. **Revocation reachability for long runs**: because every LLM call passes
   the broker, grant/PAT revocation and budget exhaustion are re-checked
   there — a background job cannot outlive revocation by more than one LLM
   call, bounding the "in-flight runs drain" window.
5. **Local power-mode readiness**: a second authenticator (agent PAT) on the
   same enforcement path enables local Claude Code
   (`ANTHROPIC_BASE_URL=<agnes>/api/v1/llm` + agent PAT). Designed now,
   enabled in the power-mode phase.

Usage object shape mirrors Anthropic:
`{input_tokens, output_tokens, cache_read_input_tokens,
cache_creation_input_tokens, total_tokens}` — in sync bodies and in the
terminal SSE event.

## 4. Runtime & scope enforcement

No new runtime. The API is a new ChatManager surface (`"api"` added to the
`Surface` enum — review the WEB-only stale-session dedupe branch in
`create_session` and per-surface telemetry when adding it).

> **V1d note (2026-07-25).** V1a–V1c shipped the *computation* of an agent's
> effective scope (`agents.*_mode` columns + `agent_scope` rows +
> `compute_effective_scope` + `agent_scope_snapshots` as an audit trail) but
> — despite this section's original claim — did **not** enforce it: the
> broker minted every brokered request under the owner's full identity
> regardless of agent scope, so a `'selected'`-scoped agent's PAT could
> reach its owner's entire table/plugin/connection surface. This was the
> HIGH finding from the V1 `/agnes-review`. **V1d** (design:
> [`2026-07-25-agent-scope-live-enforcement-design.md`](2026-07-25-agent-scope-live-enforcement-design.md))
> closed it by routing agent-scoped sessions through the same restricted-
> principal choke point co-sessions already used. The rest of this section
> is rewritten to describe what V1d actually built — the original
> (unimplemented) per-seam "sandbox threads `agent_id` through its own JWT"
> design this section used to describe is gone; see the design doc linked
> above for that history if needed.

**Scope enforcement is NOT a spawn-time materialization.** What a session
can reach is controlled at three live seams, all keyed off a single
`AgentPrincipal` the broker mints and the resolver rebuilds fresh on every
request:

- `app/api/broker.py::_mint_identity_jwt` mints an `agent_session` JWT
  (`mint_agent_session_jwt`) for a solo session bound to an agent that
  actually narrows something (any of its four `*_mode` columns is
  `'selected'`). Like the co-session JWT, it carries **no baked-in
  grants** — a synthetic `sub=f"agent-session:{session_id}"`, nothing else
  identity-bearing. A session bound to the default all-`'all'` agent (or no
  agent at all) keeps today's plain owner-identity JWT — the two paths are
  authority-identical, so this is purely a fast path, not a security
  exception.
- `app/auth/pat_resolver.py` resolves `typ="agent_session"` by looking up
  the session → its `agent_id` → the agent row → the owner, then returns an
  `AgentPrincipal(intersection=compute_agent_intersection(owner_id,
  agent_row))` (`src/agent_scope_intersection.py`) — **recomputed live, per
  request**, from current grants and current scope rows. Revoking a grant or
  narrowing the agent takes effect on the very next request; there is no
  stale-replay window because nothing authorization-relevant was ever baked
  into the token.
- The three seams each branch on `isinstance(user, PRINCIPAL_TYPES)`
  (`SessionPrincipal | AgentPrincipal`) and substitute the intersection for
  the owner's full grant set:
  1. **Tables/data** — `src/rbac.py::get_accessible_tables` /
     `can_access_table` return the intersection's `TABLE` set (plus internal
     tables); every `/api/query`, catalog, and data-read caller inherits
     this for free.
  2. **Plugins/skills** — `src/marketplace_filter.py::resolve_user_marketplace`
     filters admin-curated grants to `intersection[MARKETPLACE_PLUGIN]`; a
     user's personal Store installs live outside `resource_grants` entirely
     (`user_store_installs`), so they are filtered separately via
     `agent_scope_filter(item_type='plugin')` when `plugins_mode` narrows.
  3. **Connections (MCP tools)** — server-side MCP tool resolution keeps
     only tools whose source id is in the agent's `connection` scope rows
     when `connections_mode='selected'` (`agent_scope_filter`); this axis
     has no `ResourceType`, so it is filtered at its own seam rather than
     through the intersection map.
- **`require_admin` hard-denies any `AgentPrincipal` before any
  `is_user_admin` lookup** — an agent never inherits its owner's admin
  authority, even when the owner is an admin. This is the single most
  important line in the whole change.

Because all three seams re-evaluate on every request against a freshly
rebuilt intersection, "recompute at each message boundary" holds for the
per-message HTTP endpoints of this surface too — not just spawn.
`agent_scope_snapshots` still records the effective scope at spawn time (and
appends a row whenever a recompute yields a different result); as of V1d
that snapshot describes what is actually enforced, not merely what was
computed.

Spawn-time materialization still covers what it covers today: persona
CLAUDE.md + agent-memory notebook + corporate-memory bundle (domain-filtered).

Latency:

- Stateless `/responses` = always a fresh sandbox (isolation over latency).
- **Warm pool** of pre-booted generic sandboxes (claimed → agent context
  poured in — feasible because workspaces upload post-boot). Internal only;
  config `agent_api.warm_pool_size` (default 0 = off). Pooled sandboxes hold
  **no broker tickets until claim** (tickets minted per-claim); a claimed
  sandbox is always fresh, never returned. V1 pool is single-replica scoped;
  multi-replica pool coordination is out of scope.
- **Sessions** stay warm between messages via existing pause/resume
  (`sandbox_paused_at`); the existing idle + paused reapers already implement
  TTL. Artifact harvest hooks in before reaper teardown (§2).
- **Concurrency**: existing `ConcurrencyCapHit` (per-user config cap)
  extended with a per-PAT / per-agent cap (default ~2 concurrent runs) →
  `429 rate_limit_exceeded` with `retry-after`.

## 5. Security & audit

- **Agent ⊆ owner** enforced live at the three seams of §4 plus at every
  broker LLM call; a grant revoked mid-session stops applying at the next
  request the session makes anywhere. Shipped by V1d (see the note in §4) —
  never confers admin authority: an `AgentPrincipal` is hard-denied by
  `require_admin` before any `is_user_admin` lookup, even when its owner is
  an admin.
- **Agent-PAT hygiene**: JWT `typ="agent_pat"` + `agent_id` claim, secret
  shown once, `expires_at` / `last_used_at`, hard-rejected on management
  endpoints and every non-agent-API surface (including git smart-HTTP and
  `/marketplace.zip`), revoked in cascade on agent delete, issuable only for
  `'selected'`-mode agents.
- **Audit**: every action logged as "user X via agent Y";
  `agent_scope_snapshots` per session; `agent_config_hash` (spawn config) on
  every response.
- **Runtime gate**: agent-API inherits the existing `ResourceType.CHAT`
  resource grant — no chat access, no agent runs.
- **Webhooks**: SSRF-hardened registration + signed delivery (§2).
- **Agent memory**: `propose`-by-default write mode, provenance surfaced,
  size-capped and rate-limited writes (§1).
- Idempotency store is body-hash-bound and owner/agent-scoped (§2).

## 6. Testing

- Dual-backend contract tests for all new repos (`agents`, `agent_scope`,
  `agent_memories`, `llm_usage`, `agent_scope_snapshots`,
  `idempotency_keys`) + both migration ladders
  (`tests/test_db_schema_version.py` gate) + the backend-split and
  route-auth guards.
- Auth matrix tests mirroring the normative table in §2 (incl. agent PAT on
  git/zip marketplace channels, admin cells, `'all'`-mode token-issuance
  rejection).
- Scope intersection at each of the three seams incl. mid-session
  revocation; snapshot append-on-change.
- Budget/rate-limit semantics (429 variants, headers, dispatcher mode),
  idempotency replay + body-hash 409, webhook SSRF denial + retry/disable.
- **Golden tests for the SSE event schema** — the wire format is a contract.
- E2E via the existing `AGNES_E2E` + `FAKE_AGENT` harness: one-shot, session,
  artifacts; plus a real user-path session against a live instance.
- API coverage ratchet: CLI + MCP parity for every endpoint.

## Phasing (mandated split — this is three deliverables, not one)

**V1a — foundation**: `agents` entity + migrations/repos + builder UI
(minimal) + agent PATs + `/responses` one-shot (sync + background via
existing jobs runtime) + broker extensions (model policy, ledger, budget) +
`chat_sessions.agent_id` + default-agent seeding (web chat unchanged).

**V1b — surface completion**: sessions endpoints + SSE (AG-UI vocabulary) +
cancel + artifacts harvest/store + webhooks (SSRF-hardened, worker-backed) +
structured output + usage endpoint + CLI/MCP parity.

**V1c — memory + terminal**: `agent_memories` (propose/auto/off) + builder
memory UI + `agnes chat` terminal thin client over the public API (no
privileged backchannel).

**V1d — live scope enforcement** (added 2026-07-25, unplanned in the
original phasing above): V1a–V1c shipped the *computation* of agent scope
(the `*_mode` columns, `agent_scope` rows, and the `agent_scope_snapshots`
audit trail) but not its *enforcement* — every brokered request still ran
under the owner's full identity regardless of agent scope, the HIGH finding
from the V1 `/agnes-review`. V1d added `AgentPrincipal` (a restricted
principal sibling of the co-session `SessionPrincipal`),
`compute_agent_intersection` (fail-closed owner-grants ∩ agent-scope), the
broker's `agent_session` JWT branch, and widened the tables/marketplace/MCP
seams (plus `require_admin`) to honor it — see the design doc linked in the
§4 note; the end-to-end proof that a scoped agent is actually denied (and
its owner isn't) lives in `tests/test_agent_scope_e2e.py`. This is the
deliverable that makes the "Agent ⊆ owner" invariant in §5 actually true.

**V1.1**: SSE resume (`Last-Event-ID`), usage dashboards polish.

**V2**: local Claude Code "power mode" (`agnes agent up`: bootstrap workspace
from the agent profile, LLM via PAT-authenticated proxy, agent-filtered
marketplace channels), agent sharing across users/groups, `requires_action`
human-in-the-loop approval flow, agent-memory promotion into governed
corporate memory, A2A agent card (`/.well-known/agent-card.json`),
OpenAI-compatible shim (`model: "agnes/<slug>"`).

## Open questions (deliberately deferred)

- Per-agent memory token budget within the session bundle (fixed vs
  configurable).
- Warm-pool sizing heuristics beyond a static config value; multi-replica
  pool ownership.
- Whether the default agent should be editable (persona on "me") or locked.
- `llm_usage` rollup cadence and raw-row retention default.
