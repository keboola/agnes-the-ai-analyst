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

**IMPLEMENTED in V1.5** (`feat(memory): V1.5`).

- `_BASE_CONFIDENCE`, `_MODIFIER_EFFECTS`, and decay config moved from hardcoded dicts to
  `instance.yaml` under `corporate_memory.confidence.*`. `configure(config)` in `confidence.py`
  reads the section and overrides the module globals; defaults are still present for backward compat.
- `apply_decay()` now uses exponential decay by default (`0.5^(age/half_life)`, default half_life=12m).
  Per-source-type floor: `admin_mandate` defaults to 0.50 (never silently decays to zero).
- `instance.yaml.example` documents all fields with their default values.

Remaining V2 items:
- Per-domain decay policy (finance churns quarterly, engineering conventions persist for years).
- Bayesian metric surface in the admin UI — compute `P(approve | source_type, detection_type)`.
- Call `confidence.configure(instance_config["corporate_memory"]["confidence"])` at app startup
  (so production instances using `instance.yaml` actually use the configured values).

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

## `admin_contradictions` enrichment vs. opt-in pattern

**RESOLVED — Option A implemented in V1.5** (`feat(memory): V1.5`).

`GET /api/memory/admin/contradictions` now accepts `exclude_personal: bool = True`.
When omitted (default), personal items are replaced with `{id, hidden: true}` so
the contradiction record is visible for governance but private content is not
exposed. Pass `exclude_personal=false` to opt in to full content (KM_ADMIN only).

---

## Audience-based knowledge distribution

**IMPLEMENTED in V1.5** (`feat(memory): V1.5`).

- `/admin/mandate` and `/admin/batch` now persist `audience` onto `knowledge_items.audience`.
- `KnowledgeRepository.list_items()` and `search()` accept `user_groups: list[str] | None`.
  `None` = no filter (admins); `[]` = only null/all; `["group:finance"]` = also includes that group.
- `GET /api/memory` derives groups from `users.groups` (JSON column added in schema v10),
  prefixes with `"group:"`, and passes to the repo. Admins bypass the filter.
- Items with `audience IS NULL` or `audience = 'all'` are visible to everyone.
- `users.groups` column added via schema v10 migration; `UserRepository.update()` allows it.

**Remaining V2 items:**
- Admin UI "Target Audience" selector already exists but doesn't reflect the group filter logic;
  update to show which groups can see each item.
- Google Workspace group sync (`users.groups` population on login) is on a separate branch
  and needs to be merged into the main line.

---

## Hook configuration follow-ups

- The repo (or user) Stop hook returns
  `hookSpecificOutput.additionalContext`, which is only valid on
  `UserPromptSubmit` and `PostToolUse`. For Stop, use `systemMessage` (or
  exit silently and write to memory as a side-effect).
