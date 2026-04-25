# Project TODO

Tracked work that is intentionally not in scope for the current change but
must not be lost. Source items are grouped by area; each links back to the
review or design doc that originated them.

---

## Corporate Memory V1.5 (deferred from pd-ps review)

Originating doc: `docs/pd-ps-comments.md` (Padak × pd review, 2026-04-25).
The V1 must-fix items from that review are implemented on
`pabu/local-dev`; the architectural rationale lives in
`docs/ADR-corporate-memory-v1.md`. The items below are the V1.5 follow-ups.

### Q1 — Explicit duplicate-candidate hints in admin queue
- Severity: medium.
- Add a relation table:
  ```sql
  knowledge_item_relations (
      item_a_id   VARCHAR,
      item_b_id   VARCHAR,
      relation_type VARCHAR,  -- 'likely_duplicate', 'related', ...
      score        DOUBLE,
      resolved     BOOLEAN DEFAULT FALSE,
      created_at   TIMESTAMP DEFAULT current_timestamp,
      PRIMARY KEY (item_a_id, item_b_id, relation_type)
  )
  ```
- Add `KnowledgeRepository.create_relation()` and `list_relations()`.
- In `services/verification_detector/detector.py`, after `repo.create()` and
  before contradiction detection, run an entity-overlap candidate lookup and
  attach `relation_type='likely_duplicate'` rows for items that share ≥N
  normalized entities in the same domain.
- Surface in the admin UI alongside the existing contradictions tab.
- V2: replace entity-overlap with embedding cosine ≥ 0.85 within domain.

### Q2 — Contradiction scaling (post ADR Decision 4)
- Severity: low (was medium; the keyword pre-filter is gone, candidate
  selection + judgment + resolution now happen in one Haiku call).
- ~~V1.5: replace the keyword pre-filter with embedding cosine.~~ — no
  longer needed: the keyword filter was removed entirely in ADR Decision 4
  and the LLM does the topic matching inline.
- **V2 — Batch executor for nightly sweeps.** When a single instance
  starts producing more than a few hundred verifications per day, run the
  contradiction scan via the Anthropic Batch API (50 % cost reduction) on
  a nightly cadence rather than inline per verification. Same prompt and
  schema; only the dispatch differs. Sync stays for admin/manual/high-
  priority items.
- **V2 — Domain sharding when `DEFAULT_CANDIDATE_LIMIT` (100) is hit.**
  If a single domain corpus exceeds 100 items, the prompt truncates by
  `updated_at DESC`. Better long-term option: split the candidate set into
  shards of ~50, run one Haiku call per shard, union the judgments.
- **V2 — "No contradictions detected" alerting.** If a busy domain emits
  zero contradictions for N consecutive new items, surface to admins —
  guards against silent fail-open from Decision 4.

### Q3 — Externalize confidence + smarter decay
- Severity: medium.
- Move the constants currently in `services/corporate_memory/confidence.py`
  (`_BASE_CONFIDENCE`, `_MODIFIER_EFFECTS`, decay rate) to
  `instance.yaml`. Per CLAUDE.md (zero hardcoded values), this is mandatory
  before V1.5.
- Switch `apply_decay()` to exponential decay (half-life parameter) with a
  per-source-type floor — admin policies should not decay to zero, they
  should be explicitly revoked.
- V2: per-domain decay policy (finance churns quarterly, engineering
  conventions persist for years).
- V2: Bayesian metric surface in the admin UI — compute
  `P(approve | source_type, detection_type)` over recent items, surface as
  a read-only signal first. Auto-update behind a feature flag with a 10 %
  random-sample holdout to mitigate selection bias.

### Q3 — Multi-evidence boost computation
- The V1 fix persists `verification_evidence` rows but does not yet
  re-compute item confidence as new evidence rows accumulate.
- Add a job (or a hook on `create_evidence`) that re-runs
  `compute_confidence(...)` with `additional_verifiers = count(distinct
  source_user) - 1` and updates `knowledge_items.confidence`.

### Q4 — Typed entity registry with canonical IDs
- Severity: medium.
- Replace the case-insensitive substring match in
  `services/corporate_memory/entities.py` with a typed registry in
  `instance.yaml`:
  ```yaml
  entities:
    metrics:
      - id: metric.churn
        canonical: churn
        aliases: [attrition, customer loss, logo churn]
        kind: metric
        parent_id: null
        blocked_terms: []
      - id: org.team.sales.emea
        canonical: EMEA Sales
        kind: team
        parent_id: org.team.sales
  ```
- Use word-boundary regex (`\bcanonical\b`) for matching, with stricter
  rules for short aliases (require leading + trailing whitespace or
  punctuation; no substring matches inside words).
- Persist canonical IDs (`metric.churn`) into `knowledge_items.entities`,
  not display strings — display name is a join.
- Hierarchy via `parent_id` from day one (`EMEA Sales` rolls up to
  `Sales`). Time-bound entities (`MRR Q3`) modeled as
  `entity_ref + valid_from/valid_until`, not flat entries.
- V2+: embeddings + LLM extraction generate alias **suggestions** for
  admin approval. Never bypass deterministic entity resolution with
  embeddings — they are bad at exact short tokens.

---

## `admin_contradictions` enrichment vs. opt-in pattern (open question)

`GET /api/memory/admin/contradictions` (`app/api/memory.py`) is `KM_ADMIN`-only,
so it is not a strict privacy leak. But the enrichment loop unconditionally
inlines the full `item_a` and `item_b` dicts (`title`, `content`, `source_user`)
even when those items are flagged `is_personal=true`. ADR Decision 1 says
admins see personal items only when they explicitly opt in via
`exclude_personal=false`; this endpoint bypasses the opt-in.

Two ways forward — needs an alignment call before code lands:

- **Option A** — add `exclude_personal: bool = True` query param to the
  endpoint. When omitted, replace personal item dicts with
  `{"id": ..., "hidden": True}` so the contradiction record is still
  visible (governance still works) but the personal content is not.
- **Option B** — keep current behavior, document it explicitly: "the
  contradiction queue always shows full item content because contradictions
  need governance the same as public items."

Decision pending pd × Padak alignment.

---

## Audience-based knowledge distribution (half-built, never finished)

Status: **not in scope for V1 must-fix** — flagged here so it stops getting
silently lost.

What exists today:
- `knowledge_items.audience VARCHAR` column (`src/db.py`, original state-layer commit).
- Admin UI "Target Audience" selector with dynamic groups
  (`app/web/templates/corporate_memory_admin.html` ~line 1274).
- `POST /api/memory/admin/mandate` and `/admin/batch` accept an `audience`
  parameter (`app/api/memory.py`).

What is missing (must land for the feature to actually distribute):
1. `POST /admin/mandate` and `/admin/batch` must persist `audience` onto
   `knowledge_items.audience`, not only into the audit log.
2. `KnowledgeRepository.list_items` / `search` must accept a `for_user`
   (or `groups`) parameter and filter rows where `audience IN ('all',
   'group:<one-of-user-groups>')`.
3. `GET /api/memory` must derive the caller's group memberships
   (already in `users.groups`) and pass them to the repo.
4. Tests: a user in group A sees `audience='group:A'` items but not
   `audience='group:B'`; admins see everything regardless.
5. Decide policy for `pending`/`approved` items with no audience set —
   default `'all'` is the obvious choice.

This is the "approve for certain groups so we would manage distribution
of knowledge" feature pd was asking about (chat, 2026-04-25). It was
never removed; it was never finished. Tracked under V1.5.

---

## Hook configuration follow-ups

- The repo (or user) Stop hook returns
  `hookSpecificOutput.additionalContext`, which is only valid on
  `UserPromptSubmit` and `PostToolUse`. For Stop, use `systemMessage` (or
  exit silently and write to memory as a side-effect).
