# Corporate Memory V1 — review notes (pd × ps)

**Branch:** `pabu/local-dev` (worktree review)
**Date:** 2026-04-25
**Reviewers:** pd (with Claude Code), independent second opinion from Codex (GPT-5.5, xhigh reasoning)
**Scope:** verification flywheel + corporate memory V1 — `services/verification_detector/`, `services/corporate_memory/`, `app/api/memory.py`, schema `v7→v8`.

This document captures open questions and proposed changes raised during a walkthrough of the branch. It is not a blocker list — it is a starting point for a follow-up conversation. Severity ratings are pd's initial estimate; please push back where they feel off.

---

## Context

The branch implements a "verification flywheel": session JSONLs are scanned by `verification_detector`, an LLM extracts corrections / confirmations / unprompted definitions, and the resulting facts feed `knowledge_items` with calibrated confidence. Admin governance (HITL) gates everything before approval. New schema columns track provenance (`source_type`, `source_ref`, `confidence`, `domain`, `entities`, `valid_from/until`, `supersedes`, `sensitivity`, `is_personal`) and two new tables (`knowledge_contradictions`, `session_extraction_state`).

The mechanism is sound. The notes below are about edges where V1 calibration choices may not survive contact with real data, and one suspected access-control issue.

---

## 1. Knowledge-item deduplication

### Problem

`services/verification_detector/detector.py` derives the item ID as:

```python
id = "kv_" + sha256(f"{title}:{content}").hexdigest()[:12]
```

Two analysts independently surfacing the same fact will almost never produce identical `title` and `content` strings — LLM phrasing varies even on the same underlying claim. So this hash collides ~never in practice. It is a deterministic ID, not a deduplication mechanism.

The branch has no other dedup safeguard at create time. Practical result: the admin review queue will grow with near-duplicates that humans must triage.

### Our proposal

Do not attempt auto-dedup at create time. Instead, surface near-duplicates into the admin queue via three layers:

1. **V1.5** — entity-tag match: items sharing ≥N normalized entities become a "likely duplicate" candidate pair, exposed in the admin UI alongside the existing contradictions tab.
2. **V2** — embedding similarity: at create time, compute cosine against existing approved items in the same domain; threshold (e.g. ≥ 0.85) → flag as `likely_duplicate_of: <id>` for admin merge.
3. The contradiction detector already surfaces "soft contradictions" — these can be repurposed to also catch near-duplicates with a single LLM judge call (same infra, different prompt).

Reasoning: auto-merge at create time is risky (false positives bury new nuance under stale items). Admin queue spam is the lesser evil; embedding pre-filter at V2 keeps admin load bounded.

### Codex second opinion

> Codex run: `gpt-5.5`, `model_reasoning_effort = xhigh`, executed against the same files in worktree.

- **Verdict:** Partial. Don't auto-merge at create time, but pre-check likely duplicates *before* inserting into the admin queue (don't punt entirely to admin manual review).
- **Blind spot we missed:** contradiction detection is **not a duplicate detector**, and in the current pipeline it is not wired into `verification_detector.run()` at all. Items are created at `detector.py:178` but `detect_and_record()` is never called. The contradiction prompt explicitly says "specificity / different perspective is not a contradiction", so near-duplicates would not surface there even if it were wired.
- **Concrete alternative:** add a relation table `knowledge_item_relations(item_a_id, item_b_id, relation_type, score, resolved)` near `src/db.py:76`. Add `repo.create_relation()` near `src/repositories/knowledge.py:182`. In `detector.py:171`, run duplicate-candidate lookup (entity-overlap is fine for V1.5; embeddings later) and attach `relation_type='likely_duplicate'` so the admin queue shows duplicate candidates explicitly.
- **Severity:** medium. Must fix before broad historical backfill; acceptable for narrow V1 only if confirmations are limited and admins see explicit duplicate candidates.
- **Confidence:** 90%.

### Plan (revised after Codex)

- V1: accept that duplicate hint is missing; flag this loudly in PR description so reviewers know what's deferred.
- V1.5: add `knowledge_item_relations` table + entity-overlap-based duplicate suggestion. Surface in admin UI alongside contradictions. ~1 day work.
- V2: embedding similarity at create time, gated by domain.

**Severity:** medium (was: medium). Codex agrees on default but raises the bar for V1.5 — duplicate candidates should be surfaced explicitly, not discovered ad-hoc by admin.

---

## 2. Contradiction detection — synchronous + SQL pre-filter vs Anthropic Batch API

### Current

`services/corporate_memory/contradiction.py` runs synchronously per new item:

1. **Pre-filter (DuckDB):** find candidates with same `domain` + keyword match on `title.split()` words >3 chars. Limit 10.
2. **LLM-as-judge (sync):** Haiku prompt returns `{contradicts, explanation, severity, suggested_resolution}`.

The pre-filter has predictable recall problems: synonyms, paraphrases, cross-domain conflicts (a finance metric definition can contradict a data engineering definition of the same metric).

### Proposed migration to Batch API

Anthropic's Batch API offers ~50% cost reduction with async SLA (≤24h, typically <1h). This is attractive because contradiction detection does not need real-time response — admins review queues, not push notifications.

### Our proposal

Layered evolution:

- **V1**: keep synchronous + SQL filter as-is. Ship.
- **V1.5**: add embedding-based pre-filter. Voyage embeddings at ~$0.02/1M tokens are essentially free at our volume. Replace keyword filter with `cosine(item, candidate) > 0.6` per domain; LLM judge unchanged. Catches paraphrases the keyword filter misses.
- **V2**: switch the LLM judge phase to Batch API. Run a nightly sweep over `pending × approved` per domain. With 50% cost reduction and higher rate limits, we can afford O(N²) within domain shards (no pre-filter needed in Batch mode — let the model judge all pairs).

### Open questions

- **Hidden Batch API costs** beyond cost: dev experience (test cycles), observability (job tracking, retries), debug latency. Worth it?
- **Hybrid mode**: keep sync for high-priority sources (admin_mandate, user corrections) and batch for bulk (`session_transcript`, low-confidence pending)? Or single mode for simplicity?
- **Embedding threshold**: 0.6 is a guess. Calibrate against held-out labeled pairs from V1 data once we have them.

### Codex second opinion

- **Verdict:** Partial. Batch API is likely a V2 win for bulk sweeps, **but not as the only mode.**
- **Blind spot we missed (two of them, both severe):**
  1. The SQL pre-filter is **weaker than we described.** It is not `domain AND keyword`; it is `domain OR keyword` — `src/repositories/knowledge.py:287` joins all conditions with `OR`. Combined with `ORDER BY updated_at DESC LIMIT 10`, recent same-domain noise crowds out the actual conflict.
  2. Detector-created items **never invoke contradiction detection at all.** `detect_and_record()` exists in `services/corporate_memory/contradiction.py` but is not called from `services/verification_detector/detector.py`. So V1 contradiction governance is effectively a stub.
- **Concrete alternative:** first fix V1 before optimizing.
  1. In `src/repositories/knowledge.py:272`, build `domain = ?` as a top-level conjunct (`AND`) and the title/content keyword expansion as an inner `OR` group.
  2. In `services/verification_detector/detector.py:178`, after `repo.create()`, call `contradiction.detect_and_record(extractor, item_dict, repo)`. Or enqueue it for nightly batch — but it must run somewhere.
  3. For V2, use **one shared job model with two executors**: sync for admin/manual/high-priority items, batch for session backfills and nightly sweeps. Don't fork prompt/candidate logic.
- **On embeddings threshold:** `cosine > 0.6` is arbitrary. Calibrate using labeled pairs (duplicate / contradiction / related-but-compatible / unrelated). Optimize for recall before LLM judging — target `>95% recall` with bounded candidate count. **Use top-k plus threshold, not threshold alone.**
- **Severity:** **high.** If V1 claims contradiction governance, this needs fixing before merge — currently the feature is shipped but inert.
- **Confidence:** 92%.

### Plan (revised after Codex)

- **V1 must-fix (was missed):**
  - Fix `OR` → `AND domain + (OR keywords)` in `find_contradiction_candidates`.
  - Wire `detect_and_record()` from `verification_detector.detector.run()`. Or, if we don't want sync LLM cost in detector, enqueue items into a `pending_contradiction_check` table for a nightly batch.
  - Either of those, or remove the V1 claim that contradictions are surfaced.
- V1.5: embedding pre-filter (top-k + threshold, calibrated against labeled pairs). Single shared job model.
- V2: batch executor for nightly sweeps; sync executor for admin/manual; same prompt+candidate logic.

**Severity:** **high** (was: low for V1). Codex's nailed two bugs we missed entirely.

---

## 3. Confidence scoring — calibration, decay, feedback

### Current

`services/corporate_memory/confidence.py` is a hand-rolled lookup:

- `_BASE_CONFIDENCE`: hard-coded dict, e.g. `user_verification.correction = 0.90`, `admin_mandate = 1.00`, `claude_local_md = 0.50`.
- `_MODIFIER_EFFECTS`: hard-coded modifiers (`+0.05` per additional verifier, `+0.20` for admin confirmation).
- `apply_decay()`: linear `0.02` per month → everything reaches 0 at ~50 months including admin policies.

Three problems:

1. **Tuning requires deploy.** The numbers are guesses; in real use we will discover that, say, `user_verification.confirmation` (currently 0.60) is overoptimistic. Each iteration = code change + deploy.
2. **Linear decay is wrong for admin policies.** `admin_mandate = 1.00` decaying linearly to 0 in 50 months is sematically incorrect — admin policies are policies, not "aging facts". They are explicitly revoked, not slowly forgotten.
3. **No feedback from admin actions.** When an admin rejects an item, that signal is lost — we don't update the prior for `(source_type, detection_type)` based on observed approval rates.

### Our proposal

Three layers:

**A. Externalize all numbers to `instance.yaml`** (V1.5, mandatory):

```yaml
corporate_memory:
  confidence:
    base:
      user_verification.correction: 0.90
      user_verification.confirmation: 0.60
      user_verification.unprompted_definition: 0.90
      admin_mandate: 1.00
      claude_local_md: 0.50
      session_transcript: 0.50
    modifiers:
      additional_verifiers_per_user: 0.05
      admin_confirmed_bonus: 0.20
    decay:
      mode: exponential       # or "linear" for back-compat
      half_life_months: 12    # confidence halves every 12 months
      floor:
        admin_mandate: 0.50   # admin items never decay below 0.5
        user_verification.correction: 0.10
        claude_local_md: 0.0
        session_transcript: 0.0
```

**B. Switch to exponential decay with per-source-type floor** (V1.5):

```python
def apply_decay(confidence, created_at, source_type, half_life_months=12, floor_per_source=None):
    age_months = ...
    decayed = confidence * (0.5 ** (age_months / half_life_months))
    floor = (floor_per_source or {}).get(source_type, 0.0)
    return max(decayed, floor)
```

**C. Bayesian prior calibration from admin actions** (V2):

Nightly: compute `P(approve | source_type, detection_type)` over the last N items per category. Surface as a metric in admin UI. Initially: human-edited config update. Later: gated auto-update with selection-bias mitigation (random sampling holdout).

### Open questions

- **Power-law vs exponential decay.** Ebbinghaus forgetting curve research suggests power-law may match human knowledge half-life better. Does that translate to organizational knowledge? Probably yes for "memorized facts", probably no for "policies that are explicitly maintained".
- **Per-domain calibration.** Finance metrics churn quarterly; engineering conventions persist for years. Should `decay.half_life_months` be per-domain, not just per-source-type?
- **Selection bias in feedback loop.** If priors gate which items admins see (sorted by confidence), and we update priors from approval rate, low-confidence items rarely get reviewed → priors stay biased. Mitigation: reserve a small random sample (e.g. 10%) outside the priority queue for unbiased measurement.
- **No "post-create confirmation" mechanism.** When a new verification cites an existing approved item, only the new item gets confidence; the existing item doesn't receive a verification-count boost. This loses the "multiple analysts independently cited this" signal over time.

### Codex second opinion

- **Verdict:** Partial. External config + floors are good. **The decay framing is wrong, and there are bigger V1 bugs in the data flow.**
- **Blind spot we missed (significant):**
  1. **LLM-controlled confidence.** `services/verification_detector/prompts.py:37` asks the LLM to return `base_confidence` as part of the JSON output, and `services/verification_detector/detector.py:187` stores it directly into the DB. **Confidence should not be LLM-controlled.** The current `compute_confidence()` exists but is bypassed for verification-detector items.
  2. **Lost evidence.** The detector extracts `user_quote` from the LLM (the exact quote that constitutes the verification — the most valuable signal!) and **discards it.** It is not stored anywhere. Without `user_quote` and `detection_type` persisted in DB, future Bayesian re-calibration has no raw material.
  3. **Decay framing is wrong.** Confidence is being treated as "truth decays with age" (Ebbinghaus). Organizational facts usually change by **events, scope, or validity windows** — not by smooth forgetting. The `valid_from/valid_until` columns already exist; the right model is "fact has a validity window", not "fact slowly fades".
- **Concrete alternative:**
  1. Remove `base_confidence` from the LLM-required output in `services/verification_detector/schemas.py:30`, **or** ignore it on the read side. Compute confidence in code: `compute_confidence("user_verification", v["detection_type"])`.
  2. Add an **evidence table**: `verification_evidence(id, item_id, source_user, source_ref, detection_type, user_quote, created_at)`. Persist `user_quote` and `detection_type` per-verification. Multiple evidence rows per item enables real "additional verifiers" boost computation (one user × one quote = one evidence row).
  3. Split "evidence confidence" (signal strength from sources) from "freshness / review-due" (validity windows, explicit revocation). Don't conflate them in one number.
- **On power-law vs exponential:** secondary issue. Domain-specific volatility matters more — use **hierarchical priors**: global `(source_type, detection_type)` priors plus per-domain freshness policy.
- **On feedback loops:** mitigate via random review sampling, holdout calibration sets, and **never use learned priors as the sole gate for admin visibility.** Codex agrees with our concern but emphasizes random sampling more strongly.
- **Severity:** medium. Bayesian/config overhaul can wait, but **LLM-controlled confidence and missing evidence should be fixed before V1.**
- **Confidence:** 88%.

### Plan (revised after Codex)

- **V1 must-fix (was missed):**
  - Stop trusting LLM-returned `base_confidence`. Either drop it from `VERIFICATION_SCHEMA` or ignore it on insert and call `compute_confidence(...)` instead.
  - Add `verification_evidence` table to persist `user_quote` + `detection_type` + source reference. This is also what makes "multi-user verification boost" actually computable post-create.
- V1.5: externalize numbers to `instance.yaml` (A) + exponential decay with per-source floor (B).
- V2: Bayesian metric surface (read-only) + per-domain decay policy. Auto-update behind feature flag with random-sample holdout.

**Severity:** **high** for V1 must-fix items above; medium for the rest.

---

## 4. Entity resolution v1

### Problem

`services/corporate_memory/entities.py` does case-insensitive **substring** match against a static registry. Two consequences:

- **False positives** on short tokens. A registry containing `"MD"` matches every occurrence of `"markdown"`, `"command"`, `"admin"`, `"medical"`. Severe noise.
- **False negatives** on synonyms. `"churn"` does not match `"attrition"`, `"customer loss"`, `"logo churn"`. Severe miss.
- No lemmatization, no co-occurrence, no confidence on the match itself.

### Our proposal

Three layers:

**1. V1.5 — word-boundary regex + canonical+aliases registry:**

```yaml
corporate_memory:
  entities:
    metrics:
      - canonical: churn
        aliases: [attrition, customer loss, logo churn]
      - canonical: MRR
        aliases: [monthly recurring revenue, monthly revenue]
```

```python
import re
def resolve_entities(content, title, registry):
    text = f"{title} {content}"
    matched = set()
    for category, entries in registry.items():
        for entry in entries:
            patterns = [entry["canonical"]] + entry.get("aliases", [])
            for p in patterns:
                if re.search(rf"\b{re.escape(p)}\b", text, re.IGNORECASE):
                    matched.add(entry["canonical"])  # always store canonical
                    break
    return sorted(matched)
```

Cost: ~30 LOC, no new dependencies, deterministic, eliminates false positives on short tokens.

**2. V2 — embedding-based fuzzy match.** Pre-compute embeddings for all canonical entities (cache). Per item: embed text, cosine to each entity embedding, threshold 0.75. Catches paraphrases the alias list misses. Voyage cost is negligible.

**3. V2.5 — LLM extraction for low-entity items, batch mode.** Items with <2 resolved entities go into a nightly batch LLM job that suggests candidates for admin curation. This is also how the synonym registry grows over time without manual curation.

### Open questions

- **Skip V1.5, go straight to embeddings?** Argument for: per-item cost is ~$0.0000002, negligible. Argument against: word-boundary fix is so cheap (~30 LOC) it's a no-brainer; embeddings pull in voyage_sdk + caching infrastructure that we don't need yet.
- **Synonym registry maintenance.** Who curates it? Does it become a Big Ball Of Mud? Mitigation: V2.5 LLM-suggest pipeline auto-grows it; admin reviews additions.
- **Hierarchical entities.** "EMEA Sales" is a sub-team of "Sales". "MRR Q3" is an instance of "MRR". Layered approach doesn't model this. Do we need it for V1? Probably not. For V2? Maybe — depends on how often analysts ask drill-down questions.

### Codex second opinion

- **Verdict:** Partial. **Do not skip V1.5.** Embeddings are not a replacement for deterministic entity resolution.
- **Blind spot we missed:** embeddings are **bad at exact short entities, acronyms, product codes, metric names, and aliases where precision matters** (e.g. `MRR`, `NPS`, internal product codes). Substring → embeddings would trade one class of false positives for a different class of *silent* errors that are harder to detect. Word-boundary regex with a maintained alias list is *more correct* than embeddings for these cases, not just cheaper.
- **Concrete alternative:** rebuild `entities.py:14` to use a **typed registry with stable canonical IDs**:
  ```yaml
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
  Replace substring matching at `entities.py:57` with escaped word-boundary regex, with **special rules for short aliases** (e.g. require leading + trailing whitespace or punctuation). Store canonical **IDs** (`metric.churn`) in `knowledge_items.entities`, not display strings — display is a join. Use embeddings/LLM **only to suggest aliases or candidates for admin approval**, never to bypass deterministic resolution.
- **On hierarchies:** worth modeling minimally now. `parent_id` is sufficient. `EMEA Sales` rolls up to `Sales`. `MRR Q3` is "metric + time period", not a separate flat entity — model it as `entity_ref + valid_from/valid_until`.
- **Severity:** medium. Not a V1 blocker by itself, but **definitely required before V1.5** if dedup or contradiction depend on entities (which our V1.5 plan does).
- **Confidence:** 86%.

### Plan (revised after Codex)

- V1: ship as-is, accept noise.
- V1.5: typed registry with canonical IDs, word-boundary regex, **store IDs not strings**, support `parent_id` for hierarchies. Use this for duplicate-candidate detection (Q1 V1.5 plan). ~2 days now (more scope than originally planned).
- V2+: embeddings + LLM-suggest pipeline for **alias growth**, not entity resolution. Admin approves auto-suggested aliases.

**Severity:** medium. Codex sharpened the design — store canonical IDs, not strings; hierarchy is V1.5, not V2.

---

## 5. `is_personal` flag — suspected leak

### Code paths

`app/api/memory.py`:

- `POST /{item_id}/personal` (line ~196) — only the contributor (`item.source_user == user.email`) can flag. Correct.
- `GET /api/memory` (line ~63) — accepts query parameter `exclude_personal: bool = True`. Default excludes personal items, but the endpoint accepts `exclude_personal=False` from any authenticated user with no role check.
- `GET /api/memory/my-contributions` — returns contributor's own items including personal ones. Correct.

### Concern

Any authenticated user can request:

```
GET /api/memory?exclude_personal=false
```

…and receive items flagged `is_personal=true` by other users. The flag is meant to keep an item private to the contributor, but the list endpoint exposes it whenever the caller asks.

### Impact

`is_personal` was introduced for emergency-exit cases (the detector pulled out something private that shouldn't be team-wide). If any user can override the default, the flag provides false security. This is a confidentiality leak unless `is_personal` is purely an "admin convenience flag" — but that's not how the toggle UI presents it.

### Proposed fix

Two options:

**Option A — kill the override entirely.** Remove the `exclude_personal` query parameter. Personal items are never visible via `GET /api/memory`. Contributors see them only via `/my-contributions`. Admins see them via a separate admin endpoint guarded by `Role.KM_ADMIN`.

**Option B — gate the override by role.** Keep the parameter, but require `Role.KM_ADMIN` to set it to `False`. Add a server-side check; non-admin requests with `exclude_personal=False` get 403 or are silently coerced to `True`.

Option A is simpler, safer, and matches the most likely product intent. Option B preserves a flexible audit endpoint for admins but is easier to misuse.

### Codex second opinion

- **Verdict:** **Y. This is a leak.**
- **Blind spot we missed (worse than the list path alone):**
  - The public list endpoint passes caller-controlled `exclude_personal` into `repo.list_items()` at `app/api/memory.py:87`, and `repo.list_items()` only filters when that flag is true at `src/repositories/knowledge.py:113` — caller controls it.
  - **Search ignores `exclude_personal` entirely** via `app/api/memory.py:78` and `src/repositories/knowledge.py:119`. Any authenticated user can `GET /api/memory?search=foo` and see personal items unconditionally.
  - **Direct item access** (`/{id}`, `/{id}/provenance`, vote endpoints) has no `is_personal` check either. If a user knows or guesses an item ID, they retrieve it.
- **Concrete alternative:**
  1. For the public `GET /api/memory`, **force `exclude_personal=True`** unless `user.role in ("km_admin", "admin")`. Or simpler: always force true and leave personal review to admin endpoints.
  2. Add `exclude_personal` support to `repo.search()` (currently it's only on `list_items()`).
  3. Add a shared `_can_view_item(user, item)` check used by provenance, vote, and direct item actions. Personal items are visible only to the contributor + admins.
- **Severity:** **high. Must fix before merging V1.**
- **Confidence:** **99%.**

### Plan (revised after Codex)

- **V1 blocker.** Fix all three vectors (list, search, direct access), not just the list parameter.
  - Force `exclude_personal=True` for non-admins on `GET /api/memory`.
  - Plumb `exclude_personal` through `repo.search()`.
  - Add `_can_view_item(user, item)` helper, apply on `/{id}`, `/{id}/provenance`, and any other direct-item endpoint.
- Add regression test that an authenticated non-contributor cannot retrieve another user's `is_personal` item via list, search, or direct GET.

**Severity:** **high.**

---

## Top issues to address before merging V1

(Initial pd list was three items. Codex review added two more high-severity findings we missed entirely. Updated list below.)

1. **`is_personal` leak in full breadth (Q5).** Fix list, search, and direct-item access. Force `exclude_personal=True` for non-admins on `GET /api/memory`; plumb the filter through `repo.search()`; add `_can_view_item(user, item)` for `/{id}`, `/{id}/provenance`, vote endpoints. Add regression test. **Severity: high. Confidence: 99%.**
2. **Contradiction detection is shipped but inert (Q2).** `detect_and_record()` is never called from `verification_detector.run()`. Either wire it (sync after `repo.create()` or enqueue for batch) or remove the V1 claim that contradictions are surfaced. Also fix the `OR → AND` bug in the SQL pre-filter at `src/repositories/knowledge.py:272-287` so `domain` is a top-level conjunct and keywords are an inner `OR` group. **Severity: high.**
3. **LLM-controlled confidence + lost evidence (Q3).** Detector trusts `base_confidence` from the LLM and discards `user_quote`. Drop `base_confidence` from `VERIFICATION_SCHEMA` (or ignore on insert), call `compute_confidence(...)` in code instead. Add `verification_evidence` table to persist `user_quote`, `detection_type`, `source_user`, `source_ref` per evidence row. Without this, "additional verifiers boost" is computable in theory only. **Severity: high.**

The rest (Q1 explicit duplicate hint, Q2 batch sweep, Q3 YAML externalize + exponential decay, Q4 typed registry) can land in V1.5 as planned.

### What V1.5 must own (consequential refinements, not blockers)

- Q1: `knowledge_item_relations` table for explicit duplicate candidate hints in admin queue.
- Q2: embedding pre-filter (top-k + threshold, calibrated against labeled pairs).
- Q3: externalize confidence to `instance.yaml`; exponential decay with per-source floor.
- Q4: typed entity registry with canonical IDs, word-boundary regex, hierarchical `parent_id`.

---

## Process notes

- Worktree: `/Users/padak/github/agnes-pabu-local-dev`, branch `pabu/local-dev` tracks `origin/pabu/local-dev`.
- Walkthrough done file-by-file with paired reading; second-opinion run via Codex CLI (model `gpt-5.5`, `model_reasoning_effort = xhigh`) over the same files.
- Document is intended for round-trip discussion: pd commits the first pass, ps reads, replies inline or in PR comments.
