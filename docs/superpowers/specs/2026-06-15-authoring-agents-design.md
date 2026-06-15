# Authoring Agents — conversational web assistants for building harness content

- **Status:** Draft, revised after review (brainstorming output, pre-implementation-plan)
- **Date:** 2026-06-15
- **Revision note:** §§3.1, 4.4, 5, 6, 7, 9, 10 + new §12 reflect a three-reviewer pass
  (product/red-team, technical-feasibility, conventions). Security and privacy holes and the
  runtime-reuse over-claim were the load-bearing findings.
- **Topic:** Four specialized, web-based conversational agents that help users author the
  things Agnes distributes to AI chats — marketplace content, data packages, MCP
  connections, and corporate memory — each with deep, grounded context about how Agnes works.

---

## 1. Context & problem

Agnes distributes "harnesses" to analysts' AI chats: marketplace skills/agents/plugins,
data packages (tables + metrics), MCP tool connections, and corporate memory. Today **all of
this is authored by hand or assembled through multi-step admin operations**:

- Marketplace content is hand-written markdown in a git repo, registered by an admin.
- A data package is three separate admin calls (create → add-table → grant).
- An MCP connection is a 3–4 step admin flow (register → introspect → classify → grant).
- Corporate memory is hand-curated domains and knowledge items.

The friction is not only syntax — it is **knowing what good looks like, grounding the
artifact in the instance's real state, and not producing redundant or stale content**. The
goal is to put a knowledgeable assistant in front of each of these flows.

### What already exists (de-risk)

Exploration of the codebase established that the infrastructure is ~80% present:

- **Web chat runtime.** `app/api/chat.py` (REST + WebSocket stream), `app/chat/manager.py`
  (`LiveSession` state machine, token budget, concurrency cap, multi-sink co-drive),
  `app/chat/runner.py` (a Claude Agent SDK loop running inside an **E2B sandbox** with the
  `agnes` CLI installed), `app/chat/persistence.py` (`chat_sessions`, `chat_messages`,
  `chat_session_participants`). Anthropic + E2B keys are configured at `/admin/chat/secrets`.
- **All four domains have admin REST endpoints already**, and via the REST × CLI × MCP
  coverage ratchet, the same operations are already exposed as MCP tools:
  - Marketplace / store: `app/api/marketplace.py`, `app/api/store.py` (`/api/store/entities`
    upload + `/entities/dryrun` validation + guardrails in `src/store_guardrails/`).
  - Data packages: `app/api/data_packages.py` (`/api/admin/data-packages` CRUD +
    `/{id}/tables`).
  - MCP: `app/api/admin_mcp.py` (`/api/admin/mcp-sources` + `/introspect` + `/classify` +
    `/api/admin/mcp-tools` + grants), heuristic classifier in `connectors/mcp/classifier.py`.
  - Corporate memory: `app/api/memory_domains.py`, `app/api/memory.py`, and an existing
    **suggestion-and-approval queue** `memory_domain_suggestions` (analyst suggests, admin
    approves).
- **Sessions are captured server-side** by `agnes push` and stored under the session data dir
  (`SESSION_DATA_DIR`, default `/data/user_sessions`, organized by user), browsable via the
  **admin-only** `app/api/admin_sessions.py` and parsed by `services/session_pipeline/`. There
  is no analyst-facing session API.

**Conclusion:** "four web agents" is not greenfield. It is **four specializations of the
existing chat runtime**, plus a thin authoring layer (preview + suggestion queue) and four
domain knowledge skills.

---

## 2. Goals & non-goals

### Goals
- Four specialized conversational agents, one per seam: **Marketplace**, **Data package**,
  **MCP**, **Corporate memory**. No shared "do everything" workflow object — the seams are
  genuinely different and stay separate.
- Each agent has **grounded context**: a domain knowledge skill *plus* live read tools so it
  reasons about the instance's actual state, not from memory.
- **Conversation → reviewable draft → approval → write.** Never write-and-pray.
- Available to **all users**; the action a user can finalize depends on their role
  (admin executes; non-admin produces a suggestion routed to approval).
- Outputs use the **vendor-neutral triple** where applicable — Agent Skills folder
  (`SKILL.md` + `references/`), MCP, and AGENTS.md-style context — so portability across AI
  clients comes for free.
- Maximize reuse of the chat runtime, store/guardrails, marketplace, RBAC, and the
  REST × CLI × MCP surface.

### Non-goals (deliberate YAGNI)
- ❌ A bespoke "universal capability compiler" / intermediate representation. Adopt open
  standards (Agent Skills, MCP) instead.
- ❌ A **blocking** quality gate. Per product decision, generation proceeds without a
  hard eval wall; eval/dedupe/contradiction checks are advisory signals, not blockers.
  (See §7 — this is an explicitly accepted slop risk.)
- ❌ A visual node-graph builder. Conversation + reviewable draft is the authoring surface.
- ❌ A new agent runtime. Reuse the E2B chat runtime (decision in §4).

---

## 3. Architecture

### 3.1 Runtime: reuse the E2B chat runtime

The four agents reuse the **execution** side of `app/chat/` (E2B sandbox, the Claude Agent SDK
loop in `runner.py`, the `LiveSession` state machine, token budget, concurrency caps, crash
recovery, co-drive). The sandbox is justified beyond reuse: three of the four agents benefit
from executing code — the Marketplace agent scaffolds and validates a skill folder (and may
`git`-init a repo), and the Corporate-memory agent parses session transcripts with code.

**Honest scope correction (review finding):** the runtime today is **single-persona**. The
runner constructs `ClaudeAgentOptions` with only `permission_mode`, `cwd`, and
`setting_sources` — **no `system_prompt`, no `allowed_tools`/`disallowed_tools`, no
`mcp_servers`**. The agent's persona comes entirely from the workspace `CLAUDE.md` loaded via
`setting_sources`. So "specialized agent profile" is **net-new foundation work, not a thin
extension**: a `profile` must thread through `POST /api/chat/sessions` → persistence → the
manager spawn path → the runner. **Preferred mechanism:** rather than plumbing a `system_prompt`
string through three layers, have the spawn path write a **profile-specific `CLAUDE.md` + a
read-only knowledge skill into the session workdir** (which `WorkdirManager` already builds),
so the existing `setting_sources` loader picks it up. The toolset restriction is layered on top
(§5).

### 3.2 Agents are specializations, not new infrastructure

```
┌─ AUTHORING AGENT (one of four) ─────────────────────────┐
│  KNOWLEDGE   skill: "how <domain> works in Agnes"        │  grounded context
│              (format, model, conventions, gotchas)       │
│  READ TOOLS  catalog / schema / list-existing            │  grounds in real state
│  AUTHOR TOOLS domain mutations (create / add / grant…)   │  actions
│  PREVIEW     show the draft BEFORE writing (diff/render)  │  reviewable draft
│  GATE        admin → write directly                       │  role-aware (see §6)
│              non-admin → suggestion → admin approval       │
└──────────────────────────────────────────────────────────┘
```

Two mechanisms make "grounded context" real:
1. **Domain knowledge skill** — a distilled `SKILL.md` (+ `references/`) capturing what today
   lives only in docs (`docs/curated-marketplace-format.md`, the data-package model, the MCP
   introspect flow, the memory model). Always in the agent's hands.
2. **Read-context tools** — before proposing anything, the agent inspects the instance's
   actual state (which tables exist, which packages/sources/domains already exist), so it
   builds on reality and can **deduplicate** against existing content.

---

## 4. The four agents

Each agent shares the §3.2 skeleton; only the payload differs.

### 4.1 Marketplace agent — author a skill / agent / plugin
- **Knows:** `docs/curated-marketplace-format.md`, the `SKILL.md` + `plugin.json` contract,
  progressive disclosure budgets (~100-token metadata, <5k-token body, the rest lazy-loaded
  from `references/`), the store guardrails.
- **Reads:** existing marketplace/store entries → **dedupe check** ("this already mostly
  exists — edit X instead?").
- **Does:** writes the skill folder in the sandbox → validates via `/entities/dryrun` →
  preview → publishes via `/api/store/entities` (non-admin) or registers a curated git repo
  (admin).
- **Done when:** a validated artifact is published (or queued) with a `description` that
  encodes a clear *"use when…"* trigger.

### 4.2 Data package agent — assemble tables + metrics, grant to a group
- **Knows:** the `data_packages` model, `table_registry`, `metric_definitions`, `query_mode`
  semantics, the current 3-step create→add-table→grant flow.
- **Reads:** `agnes catalog`, table `schema`, existing packages and metrics.
- **Does:** collapses the three admin operations into one guided flow — create the package,
  add tables, attach/propose relevant metrics, grant to a group.
- **Done when:** a package exists, contains the agreed tables, and is granted to the target
  group (so it appears in those analysts' `agnes pull` manifest).

### 4.3 MCP agent — connect an external tool
- **Knows:** the `mcp_sources` model, transports (stdio/http/sse), introspect/classify,
  materialize vs passthrough, secrets/auth, `tool_grants`.
- **Reads:** existing sources and tools.
- **Does:** wraps the existing 3–4 step admin flow in conversation — register → introspect →
  classify (reusing `connectors/mcp/classifier.py`) → explain each tool in plain language and
  recommend a mode → register tools → grant.
- **Done when:** a source is registered, its tools are classified and registered, and the
  intended tools are granted to the target group.

### 4.4 Corporate-memory agent — distill knowledge from past sessions (most novel)
- **Knows:** the `memory_domains` / `knowledge_items` model (sensitivity, confidence,
  validity fields), the `memory_domain_suggestions` approval queue, how memory reaches
  analysts via the sync manifest.
- **Reads:** `app/api/admin_sessions.py` to list/parse transcripts (via
  `services/session_pipeline/`); existing memory for dedupe + contradiction detection.
- **Does:** in the sandbox, walks session transcripts → extracts recurring solved problems,
  discovered facts, and golden query patterns → clusters them into a domain as
  `knowledge_items` → checks against existing memory → proposes the domain + items.
- **Done when:** a memory domain populated from real session evidence is created (or queued),
  with contradictions against existing memory surfaced for human resolution.
- **Note:** this is also the *value/eval* dimension — memory grounded in what actually
  happened rather than invented.

> **⚠️ Privacy is a hard gate (review finding — this agent does not ship until satisfied).**
> Session privacy today is **whole-session opt-out** (`/agnes-private` excludes a session from
> `agnes push`); there is **no field/content-level redaction** in `services/session_pipeline/`.
> A non-private transcript is stored verbatim and may contain PII, customer data, or secrets.
> Mining the not-marked-private long tail into a **shared, group-distributed** memory domain
> promotes per-user-private content into a broadcast trust tier. Therefore:
> 1. **Opt-IN consent, not implicit.** Mine only transcripts whose author positively opted into
>    corporate-memory mining (a new consent flag, distinct from the existing opt-out list).
> 2. **Provenance on every item.** Each proposed `knowledge_item` records which session/author
>    it derived from; that provenance is visible in the approval UI.
> 3. **Secret/PII scan before draft.** Extracted text runs through a secret/PII scan before it
>    can become a proposal.
> 4. **No admin-direct-write.** Memory-from-sessions always routes through the human approval
>    gate (§5), never the admin fast-path.
>
> **Scope correction:** the only session API is **admin-only** (`require_admin`). Under the §5
> "agent runs with the caller's permissions" invariant a non-admin session *cannot* read other
> users' transcripts. So this agent is inherently an **admin-scoped / server-side processor**,
> not a per-user chat tool like the other three.

---

## 5. Role, RBAC & approval

**Core invariant: an agent runs with the *caller's* permissions, never elevated ones.** The
agent's toolset is filtered by role at session creation:

| Role | Tools the agent gets | Outcome |
|---|---|---|
| **Admin** | read + draft + **execute mutations** | Writes directly (today's `require_admin` path) |
| **Non-admin (analyst)** | read + draft + **suggest** (no mutation tools) | Proposal → approval queue → admin approves → executes |
| **Analyst, own scope** | workspace-local skill, own private memory | Finalizes without approval |

This must be enforced at the **tool-binding layer** (a non-admin session is never handed a
mutation tool), not merely in the prompt.

**Generalize the existing suggestion queue.** Agnes already implements exactly this pattern
for one domain (`memory_domain_suggestions`: analyst suggests, admin approves/rejects).
Generalize it into a unified **`authoring_suggestions`** queue spanning all four domains, so
an admin has one place to review "what people proposed for publication." Each suggestion
stores: domain, proposed payload (the draft), proposer, created_at, status, and the target
grant. (Sequencing note, §9: keep the queue **single-domain** until a *second* agent needs it
— do not generalize speculatively.)

**Approval is re-validation, not trusted replay (review finding — security).** A draft is
**attacker-shaped data**: a non-admin proposer (or a prompt-injected transcript feeding the
corporate-memory agent) controls the payload, including the skill/memory body text and the
*target grant*. On approve, the payload MUST re-run the **full** mutation path — the same
guardrails (`src/store_guardrails/`), the same RBAC checks, and an explicit check that the
**proposer was entitled to target that grant** (evaluated against the proposer's groups, not
just the approving admin's authority) — never a trusted fast-path. The admin-review UI renders
the **complete** payload (full skill body, full memory item text, exact grant target), not a
summary. Required adversarial tests: a suggestion whose draft embeds an out-of-scope grant or
an injection string must be caught at approve-time.

**RBAC shape decision.** To avoid a premature new `ResourceType`, the queue is **admin-only**
for review/approve/reject (`require_admin`); a non-admin may list **only their own** proposals
via a `created_by`-scoped filter on an authenticated endpoint (not a `resource_grant`). If a
later requirement makes proposals group-scoped, *then* register an `AUTHORING_SUGGESTION`
`ResourceType` + `ResourceTypeSpec` in `app/resource_types.py` (§12).

---

## 6. Net-new vs. reuse

**Reuse (no change or thin extension):**
- `app/chat/` runtime (manager, runner, persistence, workdir, readiness).
- All four domains' admin REST endpoints + their MCP tool mirrors.
- `src/store_guardrails/` (security gate stays; runs on marketplace publishes).
- `connectors/mcp/classifier.py` (MCP mode heuristic).
- `services/session_pipeline/` JSONL parsing (corporate-memory agent).
- RBAC: `resource_grants`, group membership, `require_admin` / `require_resource_access`.
- Admin web page pattern (`base_ds.html` + embedded JS; e.g. `admin_mcp_sources.html`).

**Net-new to build:**
1. **Agent profile mechanism** — a way to launch a chat session bound to one of four profiles
   (system prompt + knowledge skill + role-filtered toolset + gate policy). Likely a small
   registry + a `profile` parameter on `POST /api/chat/sessions`.
2. **Four domain knowledge skills** — `SKILL.md` (+ `references/`) per agent, distilled from
   existing docs and the data model.
3. **Role-filtered toolsets** — bind the domain's read/author/suggest tools to a session by
   caller role (the §5 invariant).
4. **Preview/draft step** — a uniform "show the draft before writing" affordance (diff or
   rendered preview) the agent calls before any mutation or suggestion.
5. **Generalized `authoring_suggestions` queue** — table + repository (DuckDB **and**
   Postgres sibling, per dual-backend discipline) + admin REST endpoints + an admin review UI,
   superseding the single-domain `memory_domain_suggestions`.
6. **Four web entry points** — a launch surface for each agent (admin section, with
   non-admin-visible variants per audience decision).
7. **Advisory checks** (§7) wired as agent tools: dedupe lookup, `/entities/dryrun`,
   contradiction detection.

Each of these must honor the project non-negotiables: TDD-first, DuckDB↔Postgres parity in
the same change, migration-ladder sync, CHANGELOG entry, vendor-agnostic content, and the
REST × CLI × MCP coverage ratchet for any new endpoint.

---

## 7. Eval & trust (advisory, non-blocking)

Per the product decision to "generate now," there is **no blocking quality gate**. Instead,
the following are surfaced as advisory signals during authoring:
- **Dedupe + consumption signal (review finding).** Read-tools check for near-duplicates
  before proposing new content, **and surface a usefulness signal** (times pulled / installed /
  referenced) on existing content, so authoring is grounded in *what actually gets used* — the
  bottleneck is demand/curation, not supply. This is part of the foundation, not a deferral.
- **Dry-run** — marketplace artifacts validate through the existing `/entities/dryrun`.
- **Contradiction detection** — corporate-memory proposals are checked against existing items.
- **Security guardrails remain mandatory** for marketplace publishes (`src/store_guardrails/`
  is a security gate, not a quality gate — it stays on).
- **Grounded-on references (review finding — cheap drift substrate).** Every authored artifact
  records which catalog tables / MCP sources / metrics it was grounded against (the agent
  already reads these via §3.2 read-tools). The scheduled drift *detector* is deferred (§10),
  but recording the coupling now is cheap and is the data a later detector needs — deferring
  the provenance is the actual mistake, not deferring the scheduler.

**Accepted risk:** without a blocking value/eval gate, low-value or subtly-wrong content can
be published. Mitigations are the human approval gate (§5) for non-admins and the advisory
checks above. A future **drift detector** (re-validating published content against the live
catalog / MCP surface on a schedule) is noted as the highest-value follow-up but is **out of
scope** for this iteration.

---

## 8. Platform-agnostic emission

Portability is achieved by **adopting open standards, not building an IR**:
- Marketplace artifacts are **Agent Skills folders** (`SKILL.md` + `references/`), an open
  multi-client format.
- Tool integrations are **MCP** (the de-facto vendor-neutral tool standard).
- Context/instructions follow the **AGENTS.md** convention where relevant.

No bespoke transpiler is built; the neutral triple already compiles to multiple AI clients.

---

## 9. Sequencing

Revised after review to de-risk the single biggest unknown (custom persona in the existing
runtime) before any schema change or queue work:

0. **Slice 0 — prove one custom-persona session, zero migration.** Add a `profile` param to
   `POST /api/chat/sessions`, persist it, and have the spawn path write a profile-specific
   `CLAUDE.md` (+ read-only skill) into the session workdir (§3.1). Use the **data-package**
   domain, **admin caller only** (RBAC is god-mode, so no role-filtering yet), driving the
   existing `/api/admin/data-packages` endpoints via the in-sandbox `agnes` CLI. **No new
   toolset binding, no suggestion queue, no `memory_domain_suggestions` migration.** This
   proves the only genuinely unproven claim with zero schema risk.
1. **Foundation (after Slice 0 is green)** — role-filtered toolset binding, the uniform preview
   step, and the consumption/grounded-on signals (§7). The `authoring_suggestions` queue stays
   **single-domain** for now.
2. **First real agent: MCP** — wraps a multi-step flow with genuine risk, leans on the existing
   classifier, has **no transcript-privacy minefield**, and meaningfully exercises the
   role-filtered toolset + approval path. (Data package was Slice-0 plumbing, not the pilot —
   a green data-package run proves none of the risky parts.)
3. **Marketplace agent** — adds sandbox scaffolding + dry-run + guardrails.
4. **Corporate-memory agent — LAST and explicitly admin-scoped.** Blocked on the §4.4 privacy
   gate (opt-in consent + provenance + PII scan). Do not start until that is designed.

**Migration discipline:** when a *second* agent forces generalizing the queue into
`authoring_suggestions`, that migration (table generalization + dual-ladder + data preservation
of existing `memory_domain_suggestions` rows) is its **own serialized unit, sequenced last**
within its slice — never co-mingled with parallel agent work (per the `/agnes-build` "migration
serialized last" discipline).

Each agent is its own spec → plan → implementation cycle on top of the foundation.

---

## 10. Open questions / risks

- **Transcript privacy (BLOCKING for §4.4).** `/agnes-private` is whole-session opt-out with
  no in-pipeline redaction. Corporate-memory mining must be opt-IN with provenance + PII scan +
  human approval, and is admin-scoped. The corporate-memory agent must not ship until this is
  satisfied. (See §4.4.)
- **Approval confused-deputy (BLOCKING for the queue).** Approval must be full re-validation,
  not trusted replay, including a check that the proposer could target the requested grant.
  Adversarial tests required. (See §5.)
- **Slop risk (accepted).** No blocking quality gate by decision; relies on the approval gate,
  advisory checks, and the new consumption signal (§7). Revisit if marketplace
  signal-to-noise degrades.
- **Lifecycle / drift (deferred).** Generated content can rot when underlying tables/MCP
  servers change; no drift detection in this iteration. Flagged as the top follow-up.
- **E2B cost.** Reusing the sandbox per authoring session carries VM cost; acceptable given
  the code-execution needs of two of the four agents, but worth monitoring for the two
  REST-only agents (data package, MCP) which could later move to a lighter loop.
- **Suggestion-queue migration.** Generalizing `memory_domain_suggestions` into
  `authoring_suggestions` must preserve existing pending suggestions (data migration on both
  backends).
- **Tool-binding enforcement.** The §5 invariant (non-admin never receives mutation tools)
  must be verified by tests, not just prompt instructions.

---

## 11. Key references

- Chat runtime: `app/api/chat.py`, `app/chat/manager.py`, `app/chat/runner.py`,
  `app/chat/persistence.py`, `app/chat/workdir.py`, `app/chat/readiness.py`.
- Marketplace/store: `app/api/marketplace.py`, `app/api/store.py`, `src/store_guardrails/`,
  `docs/curated-marketplace-format.md`, `docs/marketplace.md`.
- Data packages: `app/api/data_packages.py`, `src/repositories/data_packages.py`,
  `src/repositories/data_packages_pg.py`, `app/api/metrics.py`, `docs/metrics/`.
- MCP: `app/api/admin_mcp.py`, `connectors/mcp/classifier.py`, `connectors/mcp/client.py`,
  `src/repositories/mcp_sources.py`, `src/repositories/tool_registry.py`.
- Corporate memory: `app/api/memory_domains.py`, `app/api/memory.py`,
  `app/api/memory_domain_suggestions.py`, `services/session_pipeline/`,
  `app/api/admin_sessions.py`.
- RBAC: `app/auth/access.py`, `app/resource_types.py`, `docs/RBAC.md`.
- Conventions: `CLAUDE.md`, `CONTRIBUTING.md` (sync-map), `.claude/skills/agnes-conventions/`.

---

## 12. Convention checklist (per implementing PR)

Surfaced by the conventions review against `CONTRIBUTING.md` (sync-map). Each implementing PR
must satisfy the rows it touches:

- [ ] **Dual-backend parity:** new `authoring_suggestions` repo gets a `_pg.py` sibling **+** a
      symmetric `{DUCKDB, PG}` factory entry in `src/repositories/__init__.py` **+** a contract
      test `tests/db_pg/test_authoring_suggestions_contract.py` — all in the same change.
- [ ] **Migration ladder:** concrete new `SCHEMA_VERSION` (77+) with a paired Alembic revision
      **and** a `_vN_to_v(N+1)` step in `src/db.py`, both reaching the same endpoint and
      performing the same data-preserving transform of existing `memory_domain_suggestions`
      rows on **both** engines. Serialized last (§9).
- [ ] **REST × CLI × MCP triple:** every new `authoring_suggestions` endpoint (list / get /
      approve / reject / count) gets a CLI command (`cli/commands/` via `cli/client.py`) **and**
      an MCP tool, with parity cases in `tests/test_cli_api_parity.py` for approve/reject; run
      `make update-openapi-snapshot`.
- [ ] **`profile` on `POST /api/chat/sessions` is a modification** of a grandfathered endpoint,
      so it carries no new triple-surface obligation (note this for reviewers).
- [ ] **RBAC:** queue endpoints `require_admin` (admin-only review); non-admin "my proposals"
      list is `created_by`-scoped, not a `resource_grant`. No new `ResourceType` unless the
      queue becomes group-scoped (then register the value **+** `ResourceTypeSpec`).
- [ ] **Backend-split:** new endpoint reads go through `*_repo()` factory fns — do **not**
      propagate the raw `_get_db` read pattern from the `memory_domain_suggestions` template.
- [ ] **Web-page contract:** new pages extend `base_ds.html`/`base_page.html` (never
      `base.html`); CSS in `{% block head_extra %}` (no inline body CSS); `var(--ds-*)` only;
      no `.container:has()`, no bare `:root{}`, no raw `#hex`.
- [ ] **CHANGELOG:** each implementing PR adds an `[Unreleased]` bullet.
- [ ] **Tool-binding enforcement** (§5) is verified by tests, not prompt instructions.
