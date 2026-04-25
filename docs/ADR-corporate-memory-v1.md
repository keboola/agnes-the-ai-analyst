# ADR — Corporate Memory V1 must-fix changes

**Status:** Accepted
**Date:** 2026-04-25
**Branch:** `pabu/local-dev`
**Origin:** `docs/pd-ps-comments.md` (pd × Padak walkthrough, second-opinion
review by Codex `gpt-5.5`).

This ADR records the architectural decisions made to address the three
high-severity findings raised before V1 of the corporate-memory feature
ships. V1.5 follow-ups are tracked separately in `docs/TODO.md`.

---

## Change set overview

| Fix | Files | Reason |
|-----|-------|--------|
| 1. `is_personal` privacy enforcement | `app/api/memory.py`, `src/repositories/knowledge.py`, `tests/test_memory_api.py` | Personal items were leaking through three vectors. |
| 2. Wire contradiction detection + fix candidate SQL | `services/verification_detector/detector.py`, `src/repositories/knowledge.py`, `tests/test_corporate_memory_v1.py` | The feature shipped inert; the SQL pre-filter was structurally broken. |
| 3. Stop trusting LLM-supplied confidence + persist evidence | `services/verification_detector/{schemas,prompts,detector}.py`, `src/db.py`, `src/repositories/knowledge.py`, `tests/test_corporate_memory_v1.py` | The LLM should not set its own credibility, and the most valuable signal (the user quote) was being discarded. |

Schema bumped from v8 to v9 (`SCHEMA_VERSION = 9` in `src/db.py`) — adds the
`verification_evidence` table.

---

## Decision 1 — `is_personal` is a hard privacy boundary, not a UI hint

### Context

Five paths interacted with `is_personal`:

1. `GET /api/memory` — accepted caller-controlled `exclude_personal` (default
   `True`) with no role check. Any authenticated user could pass
   `exclude_personal=false` and read everyone's personal items.
2. `repo.search()` — ignored `is_personal` entirely. `GET /api/memory?search=…`
   bypassed the filter unconditionally.
3. `GET /{id}/provenance` — no per-item privacy check.
4. `POST /{id}/vote` — no per-item privacy check.
5. `POST /{id}/personal` — already correctly gated to the contributor.

The toggle UI presents `is_personal` as "make this item private to me", so
treating it as a UI convenience would have been a confidentiality breach.

### Decision

Treat `is_personal` as an authorization rule enforced at every read site.

- `KnowledgeRepository.search()` gains an `exclude_personal` parameter and
  appends `AND (is_personal = FALSE OR is_personal IS NULL)` when set.
- `app/api/memory.list_knowledge` derives `effective_exclude_personal`:
  - Privileged viewers (`km_admin`, `admin`) — caller's choice respected.
  - Everyone else — silently coerced to `True`. The query parameter is
    intentionally not rejected with 403 to avoid encouraging client-side
    probing for the role boundary.
- A shared `_can_view_item(user, item)` helper centralizes the rule
  (`not is_personal OR contributor OR privileged_viewer`) and is invoked
  from `/{id}/provenance` and `/{id}/vote`.
- Denied access returns **404, not 403**. The motivation is existence-
  hiding: a 403 response would let an attacker enumerate item IDs and
  learn which ones exist as personal items belonging to others.
- Contributors continue to see their own personal items via
  `/api/memory/my-contributions`, which already has the correct semantics.

### Alternatives considered

- **Reject `exclude_personal=false` from non-admins with 403.** Simpler
  to log, but advertises the role boundary. The silent-coerce path matches
  the principle "the API behaves the same regardless of whether private
  data exists."
- **Strip `is_personal` from query/response entirely for non-admins.** Too
  destructive — admins still need the toggle and the indicator. Centralizing
  on `_can_view_item` keeps the mental model uniform.

### Trade-offs

- 404-vs-403 is a deliberate UX cost: a contributor who mistypes a URL
  gets the same 404 as an attacker, with no hint that the issue is auth.
  The existence-leak risk dominates.
- The repo `search()` signature changed (added one parameter with a default
  of `False`). Existing callers need not change, but the API layer now
  always passes the effective flag.

---

## Decision 2 — Contradiction detection runs inline; SQL pre-filter is structured

### Context

`services/corporate_memory/contradiction.detect_and_record()` exists, but
`services/verification_detector/detector.run()` never called it. Items were
being created and the contradictions table was always empty. Combined with:

- `find_contradiction_candidates` in `src/repositories/knowledge.py` joined
  domain and keyword conditions with `OR`. Combined with `ORDER BY
  updated_at DESC LIMIT 10`, recent same-domain noise crowded out genuine
  conflicts and cross-domain conflicts were treated as same-domain noise.

The feature was effectively a stub.

### Decision

1. **Wire the call inline.** After `repo.create(...)` succeeds in
   `detector.run()`, the new item is reloaded and passed to
   `contradiction.detect_and_record(extractor, new_item, repo)`.
   - Wrapped in `try/except` so an LLM judge failure does not abort the
     session — the verification still lands, the contradiction simply
     isn't recorded for that item.
   - A new `contradictions_recorded` counter is added to `stats`.
2. **Fix the SQL pre-filter.** `find_contradiction_candidates` now treats
   `domain` as a top-level conjunct and `keywords` as an inner OR group:
   - Both supplied → `domain = ? AND (keyword OR keyword OR …)`
   - Domain only → `domain = ?`
   - Keywords only → `(keyword OR keyword)` (cross-domain fallback)
   - Neither supplied → return empty list.

### Alternatives considered

- **Enqueue contradiction checks for a nightly batch job.** This is the
  V2 plan (Anthropic Batch API, 50 % cost reduction), tracked in
  `docs/TODO.md`. Inline is the right V1 choice: detector volume is low,
  governance is high-priority, and admin queues need fresh signal.
- **Keep keyword-only candidate matching.** Rejected — the original bug
  meant cross-domain hits were treated as conflicts (e.g. a "data
  pipeline churn" doc would conflict with a "finance churn metric").
  Adding cross-domain fallback only when domain is missing preserves
  recall without re-introducing the bug.

### Trade-offs

- Each new verification now triggers up to 10 extra LLM calls
  (one per candidate). At V1 session volume this is acceptable; if it
  bites, the V2 batch executor (`docs/TODO.md`) is the upgrade path.
- The detector loop is now tighter coupled to the corporate-memory module
  via direct import. This is fine for now — both modules are in the same
  service tier — but if a future split is needed, the call would move
  behind a queue.

---

## Decision 3 — Confidence is computed in code; user_quote is persistent evidence

### Context

`services/verification_detector/schemas.py` required `base_confidence` from
the LLM, and `detector.py` stored it directly into
`knowledge_items.confidence`. The handcrafted `compute_confidence(...)`
table existed but was bypassed. Two failure modes follow:

1. **Trust:** The LLM (or a prompt injection inside a session) can elevate
   confidence at will, undermining the entire governance flywheel.
2. **Calibration:** Hardcoded numbers in `_BASE_CONFIDENCE` cannot be
   tuned without a deploy, and the raw evidence (the user's exact quote
   that triggered the verification) was extracted by the LLM but
   immediately discarded.

### Decision

1. **Drop `base_confidence` from the schema.** Removed from
   `VERIFICATION_SCHEMA` properties and `required`. The prompt no longer
   asks the LLM for it.
2. **Compute confidence in code.**
   `confidence = compute_confidence("user_verification", v["detection_type"])`.
   If `detection_type` is unknown (e.g. an LLM hallucinated a new value),
   we fall back to the `confirmation` baseline rather than the LLM's
   number.
3. **Persist evidence.** New table `verification_evidence`:
   ```sql
   CREATE TABLE verification_evidence (
       id              VARCHAR PRIMARY KEY,
       item_id         VARCHAR NOT NULL,
       source_user     VARCHAR,
       source_ref      VARCHAR,
       detection_type  VARCHAR,
       user_quote      TEXT,
       created_at      TIMESTAMP DEFAULT current_timestamp
   )
   ```
   Indexed on `item_id`. Multiple rows per item are expected — one per
   distinct verification event. Repository methods:
   `create_evidence(...)` and `list_evidence(item_id)`.
4. **Schema version bump.** `SCHEMA_VERSION = 9`. Added `_V8_TO_V9_MIGRATIONS`
   following the existing migration pattern; the table is also added to
   the fresh-install `_SYSTEM_SCHEMA` block.

### Alternatives considered

- **Keep `base_confidence` in the schema but ignore it on read.** Rejected
  — it would still consume LLM tokens and invite regressions where someone
  later "improves" the pipeline by reading the field.
- **Store evidence inline on `knowledge_items` (e.g. `last_user_quote`
  TEXT).** Rejected — multiple analysts independently verifying the same
  item over time is exactly the signal we want to accumulate. A 1:N table
  is the right shape.
- **Compute confidence as a derived view over `verification_evidence`.**
  Tempting but premature — the `apply_decay()`, modifier, and per-source
  floor logic make this non-trivial. Stored materialized value with a
  recompute job (V1.5, `docs/TODO.md`) is the staged plan.

### Trade-offs

- `_BASE_CONFIDENCE` is still hardcoded. Externalizing it to
  `instance.yaml` is a CLAUDE.md violation we are knowingly carrying
  through V1. It is the first item in the V1.5 plan
  (`docs/TODO.md` § Q3) and should land before any production rollout
  beyond a single instance.
- Evidence rows accumulate without bounded retention. At V1 volumes this
  is fine; once a re-confidence job is added (V1.5), retention can piggy-
  back on that pipeline.

---

## Decisions explicitly deferred (NOT made in this change)

These were proposed in the review but pushed to V1.5 because they are
either non-blocking or require design work that should not delay the
must-fix bundle. Each is tracked in `docs/TODO.md`.

- **Explicit duplicate-candidate hints** (`knowledge_item_relations` table,
  entity-overlap detection, admin UI surface).
- **Embedding pre-filter** for contradiction candidates.
- **Anthropic Batch API executor** for nightly contradiction sweeps.
- **Externalized confidence config** in `instance.yaml`, exponential decay
  with per-source floors, per-domain decay policy.
- **Bayesian prior calibration** from admin actions, with random-sample
  holdout.
- **Typed entity registry** with canonical IDs, word-boundary regex,
  hierarchical `parent_id`.
- **Multi-evidence boost re-computation** on `verification_evidence` insert.

---

## Decision 4 — Topics + judgment + resolution as one Haiku structured call

### Context

Decision 2 wired `detect_and_record(...)` into the detector and fixed the
`OR → AND` SQL bug, but kept the underlying architecture: a SQL keyword
pre-filter (`title.split()` words >3 chars matched via `ILIKE`) followed by
N sequential LLM judge calls (one per candidate). Two structural problems
with that design carried over:

1. **Recall holes in the SQL pre-filter.** Synonyms, paraphrases, language
   variants, and metric aliases miss. "Churn", "attrition", and "logo
   churn" are the same concept; the keyword filter would treat them as
   unrelated.
2. **N sequential calls.** Each new verification fired up to 10 Haiku
   calls. Latency and cost both grow linearly with the candidate cap.

Anthropic's Structured Outputs feature is now GA on Haiku 4.5, with strict
schema enforcement (required fields guaranteed, types guaranteed,
`additionalProperties: false` honored). This makes a single batched
structured-output call reliable enough to replace both the keyword filter
and the per-candidate loop.

### Decision

Replace the keyword SQL pre-filter and the N-call judge loop with **one
batched Haiku call** that returns:

- a judgment per same-domain candidate (`is_contradiction`, `severity`,
  `explanation`),
- and a structured **resolution** suggestion in the same response
  (`resolution_action` ∈ `{kept_a, kept_b, merge, both_valid}`, with
  `resolution_merged_content` populated only when `merge`, plus
  `resolution_justification`).

Concretely:

- `services/corporate_memory/prompts.py` adds `BATCH_CONTRADICTION_PROMPT`
  + `BATCH_CONTRADICTION_SCHEMA`. Resolution is **flattened** into the
  judgment object (`resolution_action`, `resolution_merged_content`,
  `resolution_justification`) rather than nested, to stay inside the
  strict-output constraints (no recursion, predictable required fields).
- `services/corporate_memory/contradiction.py`'s `find_and_judge(...)`
  loads same-domain candidates from SQL and runs **one** Haiku call.
  `detect_and_record(...)` persists the resulting records.
- `src/repositories/knowledge.py`:
  - `find_contradiction_candidates` is reduced to a domain-only SELECT.
    The `title_words` parameter is removed entirely. Domain remains a hard
    SQL conjunct (cheap; bounds prompt size as the corpus grows).
  - `create_contradiction(suggested_resolution=…)` now accepts either a
    dict (JSON-encoded into the existing TEXT column) or a string
    (legacy/free-form). `list_contradictions` and `get_contradiction`
    decode JSON-shaped values back into dicts on read.

### Defenses against LLM failure modes

- **Hallucinated candidate IDs**: every returned `candidate_id` is
  validated against the input set; rows with unknown IDs are dropped.
- **Out-of-enum severity**: normalized to `None` before persistence (the
  schema enum should already block this, but we don't trust it).
- **Out-of-enum resolution_action**: dropped from the persisted record.
  The contradiction itself stays — only the resolution suggestion is
  discarded.
- **Empty corpus**: short-circuits before any LLM call (cost guard).
- **LLM error**: caught and logged; record creation simply yields no
  contradictions for that item, the verification still lands, the session
  is still marked processed.

### Alternatives considered

- **Keep N sequential calls but switch them to structured output.** Half
  the gain. Latency and per-item cost stay linear in candidate count.
- **No SQL at all — pass the entire `knowledge_items` corpus to Haiku.**
  Rejected — token cost grows with the corpus size and gets unbounded for
  large instances. Domain filter is a cheap hard ACL.
- **Per-domain shards via embedding cosine + Haiku judge.** Tracked as a
  future V2 refinement when a domain corpus exceeds ~100 items
  (`docs/TODO.md`). Not needed for current scale.
- **Nested resolution object in the schema.** Tested but flattened —
  Anthropic's strict structured output works most reliably with no nested
  optional objects; required-fields-with-null is more reproducible than
  `anyOf`/`oneOf`.

### Trade-offs

- **Fail-open on the LLM.** If Haiku says "no contradiction" for
  everything, contradictions silently never surface. The previous SQL
  keyword filter at least seeded obvious matches. Mitigation (deferred):
  log + alert when a busy domain shows zero contradictions for N
  consecutive new items.
- **No textual fallback.** When the LLM is down, no contradictions are
  detected at all. Acceptable because the judge was already
  LLM-dependent — the only thing additionally lost is the candidate seed.
- **Prompt size growth.** Prompt scales with same-domain corpus size. At
  ~100 items × ~50 tokens each the input is ~5k tokens — still cheap on
  Haiku 4.5. Sharding kicks in via `DEFAULT_CANDIDATE_LIMIT = 100` if a
  domain blows past that.
- **Test rewrites.** All contradiction-side tests previously asserted on
  the legacy single-pair `{contradicts, …}` schema and `title_words`
  parameter. They were rewritten to the batched
  `{judgments: [{candidate_id, is_contradiction, severity, …}]}` shape.

### Schema-side impact

None. `knowledge_contradictions.suggested_resolution` is already TEXT;
JSON-encoded dicts fit there without a migration.

---

## Verification

Tests covering each decision live in:

- Decision 1: `tests/test_memory_api.py::TestPersonalItemPrivacy`
- Decision 2: `tests/test_corporate_memory_v1.py::TestContradictionCandidateSqlNarrowing`,
  `TestDetectorWiresContradictionDetection`
- Decision 3: `tests/test_corporate_memory_v1.py::TestDetectorIgnoresLLMConfidence`,
  `TestDetectorPersistsEvidence`
- Decision 4: `tests/test_corporate_memory_v1.py::TestContradictionDetectionIntegration`,
  `TestBatchedContradictionFindAndJudge` (single-call cost guarantee,
  hallucinated-id defense, mixed batch, severity normalization, resolution
  round-trip, legacy-string back-compat)

Run:
```bash
pytest tests/test_memory_api.py tests/test_corporate_memory_v1.py -v
```
