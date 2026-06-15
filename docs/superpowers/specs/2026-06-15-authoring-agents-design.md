# Authoring Agents — conversational web assistants for building harness content

- **Status:** Draft (brainstorming output, pre-implementation-plan)
- **Date:** 2026-06-15
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
- **Sessions are captured server-side** by `agnes push` and stored at
  `${DATA_DIR}/users/{email}/sessions/{id}/transcript.jsonl`, browsable via
  `app/api/admin_sessions.py` and parsed by `services/session_pipeline/`.

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

The four agents reuse `app/chat/` end-to-end. Each authoring session is a chat session with a
specialized **agent profile** (system prompt + knowledge skill + toolset + gate policy). The
E2B sandbox is justified beyond mere reuse: three of the four agents benefit from executing
code in isolation — the Marketplace agent scaffolds and validates a skill folder (and may
`git`-init a repo), and the Corporate-memory agent parses session JSONL transcripts with code.
Token budget, concurrency caps, crash recovery, and co-drive are inherited.

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
- **Does:** in the sandbox, walks session JSONL transcripts → extracts recurring solved
  problems, discovered facts, and golden query patterns → clusters them into a domain as
  `knowledge_items` → checks against existing memory → creates the domain + items → grants.
- **Done when:** a memory domain populated from real session evidence is created (or queued),
  with contradictions against existing memory surfaced for human resolution.
- **Note:** this is also the *value/eval* dimension — memory grounded in what actually
  happened rather than invented.

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
grant. Approval replays the draft through the same author tools the admin agent would use.

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
- **Dedupe** — read-tools check for near-duplicates before proposing new content.
- **Dry-run** — marketplace artifacts validate through the existing `/entities/dryrun`.
- **Contradiction detection** — corporate-memory proposals are checked against existing items.
- **Security guardrails remain mandatory** for marketplace publishes (`src/store_guardrails/`
  is a security gate, not a quality gate — it stays on).

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

Build the **agent profile mechanism + one agent end-to-end first**, then replicate the proven
pattern. Recommended order (highest value / lowest runtime risk first):

1. **Foundation** — agent profile mechanism, role-filtered toolset binding, preview step,
   generalized `authoring_suggestions` queue (replacing `memory_domain_suggestions`).
2. **Pilot: Data package agent** — purely internal REST calls, no code execution, immediately
   useful, lowest risk. Proves the skeleton.
3. **MCP agent** — wraps an existing multi-step flow; classifier already present.
4. **Marketplace agent** — adds sandbox scaffolding + dry-run + guardrails.
5. **Corporate-memory agent** — most novel; adds session-transcript mining.

Each agent is its own spec → plan → implementation cycle on top of the foundation.

---

## 10. Open questions / risks

- **Slop risk (accepted).** No blocking quality gate by decision; relies on the approval gate
  and advisory checks. Revisit if marketplace signal-to-noise degrades.
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
