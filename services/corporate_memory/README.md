# Corporate Memory

> **The knowledge layer between your people and your AI agents.**

Corporate Memory is a persistent, governed, audience-aware knowledge base that closes the loop between what your team *knows* and what your AI agents *act on*. It collects expertise from analysts' notes and session transcripts, routes it through a human review workflow, scores it for confidence and freshness, and injects the highest-value facts into every AI agent context — automatically, at sync time.

It ships as part of [Agnes](../../README.md), but the module is architecturally self-contained: the data model is a handful of DuckDB tables, the API is a FastAPI router, and the agent integration is a single HTTP endpoint plus a CLI write step. Anything that can make HTTP calls can consume it.

---

## Why this exists

Every team accumulates institutional knowledge that never makes it into docs: "the `orders` table has a 3-day settlement lag", "never filter by `created_at` on the events table — use `event_time` instead", "the Q4 numbers include a one-time restatement". This knowledge lives in Slack, in people's heads, and occasionally in a `CLAUDE.local.md` file.

When an AI analyst runs a query without that context, it produces a subtly wrong answer. Not wrong enough to be caught immediately — wrong enough to silently mislead a decision.

Corporate Memory solves this by making institutional knowledge:

- **Discoverable** — collected automatically from where it already lives
- **Trustworthy** — scored by source, recency, and human confirmation
- **Governed** — admin-approved before it reaches agents
- **Private-by-default** — personal notes stay personal
- **Fresh** — decays over time so stale facts don't outlive their usefulness

---

## How knowledge flows

```
┌─────────────────────────────────────────────────────────────────────┐
│                        KNOWLEDGE SOURCES                            │
│                                                                     │
│  CLAUDE.local.md          Session transcripts        Admin mandate  │
│  (analyst notes)          (JSONL conversation logs)  (direct input) │
└──────────────┬──────────────────┬───────────────────────┬───────────┘
               │                  │                       │
               ▼                  ▼                       │
    ┌──────────────────┐  ┌──────────────────┐            │
    │   Collector      │  │  Verification    │            │
    │                  │  │  Detector        │            │
    │  Haiku batch     │  │                  │            │
    │  extraction      │  │  Haiku per-      │            │
    │  from MD files   │  │  session extract │            │
    │  (change-detect) │  │  (dedup by hash) │            │
    └────────┬─────────┘  └────────┬─────────┘            │
             │                     │                      │
             └──────────┬──────────┘                      │
                        ▼                                 ▼
             ┌──────────────────────────────────────────────┐
             │             knowledge_items                  │
             │                                              │
             │  status: pending → approved / mandatory      │
             │  confidence: 0.0 – 1.0                       │
             │  audience: null | all | group:X              │
             │  is_personal: bool (hard privacy boundary)   │
             └───────────────────┬──────────────────────────┘
                                 │
                        ┌────────┴────────┐
                        ▼                 ▼
             ┌──────────────────┐  ┌──────────────────┐
             │  Admin Review    │  │  Contradiction   │
             │                  │  │  Detection       │
             │  Approve         │  │                  │
             │  Reject          │  │  Haiku batch     │
             │  Mandate         │  │  judge per       │
             │  Revoke          │  │  domain          │
             │  Edit            │  └──────────────────┘
             └────────┬─────────┘
                      │
                      ▼
           ┌──────────────────────┐
           │   /api/memory/bundle │
           │                      │
           │  mandatory items     │  ← always included
           │  + ranked approved   │  ← confidence × recency
           │  audience-filtered   │
           │  6000-token budget   │
           └──────────┬───────────┘
                      │
           ┌──────────▼───────────┐
           │    da sync           │
           │                      │
           │  .claude/rules/      │
           │    km_<id>.md        │  ← one per mandatory item
           │    km_approved.md    │  ← ranked approved bundle
           └──────────┬───────────┘
                      │
                      ▼
           ┌──────────────────────┐
           │   Claude Code        │
           │   (AI agent)         │
           │                      │
           │  rules/ auto-loaded  │
           │  on every session    │
           └──────────────────────┘
```

---

## Data model

### `knowledge_items`

The core table. Each row is one discrete, citable fact.

| Field | Type | Purpose |
|-------|------|---------|
| `id` | VARCHAR | Stable ID (`km_<12-hex>`) — preserved across re-collections so votes don't orphan |
| `title` | VARCHAR | One-line summary of the fact |
| `content` | TEXT | Full explanation, including SQL examples, caveats, context |
| `category` | VARCHAR | `data_analysis` · `api_integration` · `debugging` · `performance` · `workflow` · `infrastructure` · `business_logic` |
| `domain` | VARCHAR | Hard partition for contradiction detection: `finance` · `engineering` · `product` · `data` · `operations` · `infrastructure` |
| `status` | VARCHAR | Lifecycle: `pending` → `approved` / `mandatory` / `rejected` / `revoked` / `expired` |
| `confidence` | DOUBLE | 0.0 – 1.0. Source-derived, decay-adjusted, evidence-boosted |
| `audience` | VARCHAR | `NULL` / `all` = everyone · `group:finance` = that group only |
| `is_personal` | BOOLEAN | Hard privacy flag: contributor + admins only, never reaches agents |
| `source_type` | VARCHAR | `claude_local_md` · `session_transcript` · `user_verification` · `admin_mandate` |
| `source_user` | VARCHAR | Email of contributor |
| `entities` | JSON | Recognized teams, metrics, domains (auto-tagged) |
| `supersedes` | VARCHAR | ID of fact this replaces (deprecation chain) |
| `valid_from` / `valid_until` | TIMESTAMP | Optional temporal window |
| `sensitivity` | VARCHAR | `internal` (default) — future sensitivity tiers |

### Supporting tables

```
knowledge_votes         — per-user upvote / downvote (upsert, last vote wins)
knowledge_contradictions — pairs of conflicting items + Haiku resolution suggestion
verification_evidence   — user quotes that support or correct a fact
session_extraction_state — deduplication log: which JSONL sessions were processed
```

---

## Confidence scoring

Every fact carries a confidence score that drives bundle ranking and communicates trust to agents.

### Source baseline

| Source | Subtype | Base confidence |
|--------|---------|----------------|
| `admin_mandate` | — | **1.00** |
| `user_verification` | correction | **0.90** |
| `user_verification` | unprompted definition | **0.90** |
| `user_verification` | confirmation | **0.60** |
| `claude_local_md` | — | **0.50** |
| `session_transcript` | — | **0.50** |

### Modifiers (additive)
- `+0.20` — admin approved
- `+0.20` — confirmed in a live session
- `+0.05` — for each additional independent verifier

### Decay
Facts lose confidence over time. Default half-life: **12 months**.

```
confidence(t) = base × 0.5^(age_months / 12)
```

Floors prevent full expiry: `admin_mandate` never falls below 0.50; verified facts stay above 0.40.

### Bundle ranking
Approved items compete for the 6000-token context budget using:

```
score = confidence × max(0, 1 - age_days / 365)
```

The highest-ranked facts enter the agent's context first. Mandatory items bypass the budget entirely.

---

## Status lifecycle

```
          ┌──────────┐
          │ pending  │  (default on creation, also after collector harvest)
          └────┬─────┘
               │  admin review
       ┌───────┼───────┐
       ▼       ▼       ▼
  ┌─────────┐  │  ┌──────────┐
  │approved │  │  │ rejected │
  └────┬────┘  │  └──────────┘
       │       │
       │  mandate (mandatory always in bundle)
       ▼       ▼
  ┌───────────────┐
  │  mandatory    │
  └───────┬───────┘
          │  revoke / expire
          ▼
      ┌────────┐
      │revoked │
      └────────┘
```

---

## Agent integration

### The bundle endpoint

`GET /api/memory/bundle` is the single endpoint agents need. It returns a token-budgeted, audience-filtered, confidence-ranked payload ready for context injection.

```json
{
  "mandatory": [
    {
      "id": "km_a3f82c119e4d",
      "title": "orders table has 3-day settlement lag",
      "content": "The `orders.completed_at` timestamp reflects payment settlement, not placement. Always subtract 3 days when comparing to `events.created_at` for same-day analysis.",
      "confidence": 1.0,
      "domain": "finance",
      "category": "data_analysis"
    }
  ],
  "approved": [
    {
      "id": "km_7b1e0d9c4a22",
      "title": "Use event_time not created_at on events table",
      "content": "...",
      "confidence": 0.82
    }
  ],
  "token_estimate": 1240,
  "token_budget": 6000
}
```

**Mandatory items** are always included regardless of token budget. If your organization has decided a fact is mandatory, agents must always have it.

**Approved items** are ranked by `confidence × recency` and included until the budget is exhausted. The budget is conservative (4 chars/token) so the estimate undershoots rather than overshoots.

**Audience filtering** is automatic. The endpoint reads the caller's JWT, resolves their group memberships (`users.groups`), and applies SQL filtering at query time. Agents authenticated as group members get their slice; admins get everything.

### Claude Code integration

`da sync` writes the bundle as files in `.claude/rules/`:

```
.claude/rules/
  km_a3f82c119e4d.md   ← one file per mandatory item
  km_approved.md       ← all approved items in a single ranked file
```

Claude Code automatically loads every file in `.claude/rules/` at session start. No prompt engineering needed — your agent just knows what your team knows.

Files are pruned on every sync. Revoked items disappear from the next session.

### Using the bundle in your own agent

Any HTTP client works:

```python
import httpx

resp = httpx.get(
    "https://your-agnes-host/api/memory/bundle",
    headers={"Authorization": f"Bearer {token}"},
)
bundle = resp.json()

# Build system context
rules = []
for item in bundle["mandatory"]:
    rules.append(f"## {item['title']}\n{item['content']}")
for item in bundle["approved"]:
    rules.append(f"## {item['title']}\n{item['content']}")

system_prompt = "You are a data analyst.\n\n# Team Knowledge\n\n" + "\n\n".join(rules)
```

The endpoint respects the caller's identity — agents running as a specific user or service account get audience-appropriate facts, nothing more.

---

## Governance workflow

### For knowledge admins

The admin UI at `/corporate-memory/admin` provides:

**Review queue** — pending items appear here after collection. One click to approve, reject, mandate, or revoke. Batch actions available for queue clearing.

**Contradiction dashboard** — when two facts conflict (same domain, detected by Haiku), they appear side-by-side with a suggested resolution: keep A, keep B, merge, or both valid. Admin picks and the resolution is audited.

**Audit log** — every governance action (approve, reject, mandate, revoke, edit, contradiction resolution) is logged with who, what, and when.

### REST API for CI/CD or programmatic governance

```bash
# Mandate a fact via API
curl -X POST https://your-host/api/memory/admin/mandate?item_id=km_a3f82c \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"reason": "agreed in Q2 planning", "audience": "group:finance"}'

# Batch approve after bulk import
curl -X POST https://your-host/api/memory/admin/batch \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"item_ids": ["km_...", "km_..."], "action": "approve"}'
```

---

## Privacy model

Personal items are a **hard privacy boundary**, not a UI hint.

When a contributor marks an item `is_personal=True`:

- Excluded from `/api/memory/stats` (existence not leaked via aggregate counts)
- Excluded from `/api/memory/bundle` (never reaches AI agents)
- Excluded from contradiction detection (content never sent to external LLM)
- Contradiction admin endpoint returns `{"id": "...", "hidden": true}` in place of content
- Visible only to the contributor and KM_ADMIN / ADMIN roles

This matters for session-transcript extraction: an analyst's notes about a sensitive customer or personal observation stay in their personal space unless they explicitly make them shared.

---

## Contradiction detection

When a new fact arrives (via collector or API), the system automatically:

1. **Finds candidates** — same domain, exclude personal items (SQL, no LLM yet)
2. **Judges in batch** — one Haiku call with all candidates and the new item
3. **Records conflicts** — stores `knowledge_contradictions` row with severity and suggested resolution
4. **Surfaces to admins** — appears in contradiction dashboard

Haiku's resolution suggestion takes one of four shapes:

| Action | Meaning |
|--------|---------|
| `kept_a` | The original item is correct; new item is wrong |
| `kept_b` | The new item supersedes the original |
| `merge` | Both contain partial truth; includes `merged_content` |
| `both_valid` | No contradiction — context-dependent |

Admins make the final call. The suggestion is an accelerator, not a decision.

---

## Architecture: where each component lives

```
agnes-the-ai-analyst/
│
├── services/corporate_memory/      ← background services
│   ├── collector.py                   Haiku extraction from CLAUDE.local.md files
│   ├── confidence.py                  Source-based scoring + decay + evidence boost
│   ├── contradiction.py               Candidate search + Haiku batch judgment
│   ├── entities.py                    Entity tagging (teams, metrics, domains)
│   └── prompts.py                     All LLM prompts (collector, verification, contradiction)
│
├── services/verification_detector/    ← session transcript extraction
│   ├── detector.py                    JSONL scan → Haiku extraction → evidence store
│   └── ...
│
├── app/api/memory.py               ← FastAPI router (all /api/memory/* endpoints)
├── app/web/router.py               ← Web UI routes (/corporate-memory, /corporate-memory/admin)
├── app/web/templates/
│   ├── corporate_memory.html          User-facing knowledge browser + voting
│   └── corporate_memory_admin.html    Admin review queue + contradiction dashboard
│
├── src/repositories/knowledge.py   ← DuckDB CRUD (no SQL in API layer)
├── src/db.py                       ← Schema: knowledge_items + 4 supporting tables
│
└── cli/commands/sync.py            ← da sync step 7: fetch bundle → write km_*.md
```

---

## Use cases

### The data analyst who hates re-explaining context

Every time a new analyst joins, they spend two weeks re-discovering the same caveats — settlement lags, table gotchas, metric definitions. With Corporate Memory, that knowledge is automatically injected at session start. Day one analyst gets the same context as the five-year veteran.

### The analytics lead who needs to propagate a correction

You just discovered the revenue metric has been calculated wrong for three months. You add a correction to the knowledge base, mandate it, and every agent and analyst gets the fix on next sync. No Slack blast, no doc update, no hoping people notice.

### The data governance team that needs an audit trail

Every approval, rejection, mandate, and revocation is logged with actor, timestamp, and reason. The contradiction log shows which conflicting facts were surfaced and how admins resolved them. Full provenance from source transcript to agent context.

### The platform team building agents on top of this

You have five different agents (SQL assistant, dashboard builder, metric explainer, anomaly detector, report writer). They all call `/api/memory/bundle` at session start with their service account JWT. Each gets the same governed, audience-filtered facts without any per-agent configuration. One knowledge base, all agents benefit.

### The analyst who wants private notes

An analyst working on sensitive M&A data marks their items as personal. The notes are available to their own sessions via `/api/memory/my-contributions` but never appear in the shared bundle, never get aggregated into stats visible to colleagues, and never flow to an external LLM for contradiction detection. The hard privacy boundary is enforced at the SQL layer, not in application logic.

---

## Comparison with alternatives

| | Corporate Memory | Static `CLAUDE.md` | Vector RAG | Fine-tuning |
|---|---|---|---|---|
| **Update latency** | Next `da sync` (~minutes) | Manual edit + redeploy | Near-realtime | Days to weeks |
| **Governance** | Approve / reject / audit | None | None | Training data curation |
| **Confidence scoring** | Yes (source + decay) | No | Similarity score only | Baked into weights |
| **Contradiction detection** | Yes (auto, per domain) | No | No | No (invisible) |
| **Audience filtering** | Yes (group-level) | No | Typically no | No |
| **Personal items** | Hard privacy boundary | No concept | Difficult | No concept |
| **Agent-readiness** | Bundle endpoint | Manual copy | Custom retriever | Inference only |
| **Token budget control** | Yes (6000-token budget) | Fixed | Result count | N/A |
| **Provenance** | Source + user + evidence | None | Document reference | None |
| **Setup cost** | Low (DuckDB, FastAPI) | None | Embedding pipeline | High |

**When to use this instead of a vector store:** When your knowledge base is small enough to fit in a few thousand tokens (most teams: under 500 facts), governance and provenance matter more than recall breadth, and you need audience-level access control baked in.

**When a vector store is better:** When your corpus has millions of documents, you need semantic similarity across a large unstructured dataset, or your facts don't need human approval workflows.

**When to use both:** Large unstructured corpus in a vector store for broad retrieval, Corporate Memory for the high-value curated facts that must always appear in the context window.

---

## Running the collector

The collector is a background service that harvests `CLAUDE.local.md` files from all registered users.

```bash
# Dry run — shows what would be extracted without writing anything
python -m services.corporate_memory --dry-run

# Full collection
python -m services.corporate_memory

# Reset: drop existing items and re-collect from scratch
python -m services.corporate_memory --reset
```

**Change detection:** The collector MD5-hashes each user's file and skips unchanged files. A full org collection typically makes one Haiku call per changed file — cost is proportional to change rate, not corpus size.

---

## Running the verification detector

```bash
python -m services.verification_detector
```

Scans `/data/user_sessions/*.jsonl`, extracts knowledge from unprocessed sessions, records evidence, runs contradiction detection. Sessions are marked processed by file hash — re-running is safe.

---

## Connecting to Agnes

Corporate Memory is wired into Agnes' sync pipeline automatically:

```
da sync
  step 1–6: download tables, rebuild DuckDB views
  step 7: fetch /api/memory/bundle → write .claude/rules/km_*.md
```

After sync, every `claude` session in the local project directory automatically inherits the full governed knowledge base for that user's audience groups.

The `CLAUDE.md` template (at `config/claude_md_template.txt`) documents the `km_*.md` convention so agents understand what they're reading and why the files exist.

---

## For agents reading this

If you are an AI agent that has been given access to a system running Corporate Memory:

- `GET /api/memory/bundle` — call this at the start of tasks that involve data analysis, metric interpretation, or domain-specific queries. The facts in the response have been approved by a human and are scored for freshness.
- Items in `mandatory` are non-negotiable context. If a mandatory fact contradicts your training data, trust the mandatory fact — it reflects a recent, admin-confirmed organizational decision.
- Items in `approved` are ranked by confidence. Higher-ranked items are more recently verified and from more reliable sources.
- `audience` on each item has already been filtered for you — you won't see facts you're not supposed to.
- Never invent metric definitions or table semantics. If a fact exists in this bundle, use it. If it doesn't, ask a human rather than guessing.

---

## Contributing knowledge

**As an analyst:** Add facts to your `CLAUDE.local.md` file. The collector picks them up on the next scheduled run and routes them to the admin review queue. Or submit directly via the web UI at `/corporate-memory`.

**As an admin:** Review pending items at `/corporate-memory/admin`. Approve individual items or use batch actions. Mandate critical facts to ensure they always appear in agent contexts.

**Via API:**
```bash
curl -X POST https://your-host/api/memory \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Q3 revenue includes one-time restatement",
    "content": "The Q3 2025 revenue figure (+18% YoY) includes a $2.4M restatement...",
    "category": "business_logic",
    "domain": "finance",
    "tags": ["revenue", "restatement", "Q3-2025"]
  }'
```

New items start as `pending` and become available to agents after admin approval.

---

## License

Part of [Agnes](../../README.md) — open-source data distribution platform for AI analytical systems.
