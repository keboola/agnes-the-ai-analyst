# Spec: skill sharing — audience/safety split, review after the fact

- **Date:** 2026-07-28
- **Status:** Phase 0 applied (config only); Phase 1 + Phase 2 proposed for approval
- **Scope:** How an authored skill/agent/plugin becomes visible to other people in the
  instance. **Explicit non-goals:** the inline (mechanical) guardrail checks, which stay
  exactly as they are and stay blocking; the ingested-marketplace path
  (`marketplace_registry` / `marketplace_plugins`), which already speaks `resource_grants`.

---

## 1. Motivation

A user authors a skill, shares it, and the Library badge reads **"In review."** It stays
there forever. There is no reviewer, and on an instance without LLM credentials there
never will be.

The mechanism: `guardrails.enabled` defaults to `True`
([`app/instance_config.py:1053`](../../../app/instance_config.py)), and the upload path
sets `hold_for_review = guardrails_enabled` *independently* of whether the LLM provider
has credentials ([`app/api/store.py:2196`](../../../app/api/store.py),
[`app/api/admin.py:4977`](../../../app/api/admin.py)). Intent `True` + credentials absent
is deliberately fail-CLOSED: the submission parks at `status='pending_llm'` and no LLM
call is ever scheduled. The design anticipated this and shouts about it at boot
([`app/main.py:2060`](../../../app/main.py)) — but the shout goes to a log, while the
user gets a badge promising a review that cannot happen.

Underneath the symptom is a modelling error. `store_entities.visibility_status`
(`pending` / `approved` / `hidden` / `archived`) encodes **two unrelated questions** in
one column:

1. *Is this thing dangerous?* — a safety judgment, made by the system or an admin.
2. *Who is this for?* — an audience choice, which belongs to the author.

Because they share a column, answering (2) requires waiting on (1). That is why sharing a
skill with four colleagues needs a security verdict, and why a missing API key means
nobody can share anything.

The rest of the Library does not work this way.
[`app/services/library_sharing.py`](../../../app/services/library_sharing.py) gives
collections, corpus files, and agents owner-initiated sharing — an owner may grant to
groups **they belong to**, writing the same `resource_grants` rows the admin layer writes.
Skills are explicitly carved out, and the docstring says why:

> "Skills are deliberately absent: a store entity is visible to every authenticated user
> once approved, so a `resource_grants` row on one would be dead mechanics — nothing reads
> it."

That is an accurate description of today and the whole problem in one sentence. The item
type users author most is the one type with no Share control.

---

## 2. Model

Split the column into two orthogonal axes.

| Axis | Values | Who sets it | Gates reads? |
|---|---|---|---|
| **Audience** (new) | `private` → `shared` (specific groups) → `everyone` | the **author**, instantly | yes — the only thing that gates visibility |
| **Safety** (existing, narrowed) | `ok` / `quarantined` | guardrail pipeline or admin | only on `quarantined` — a kill switch, not a queue |

**No visibility state is ever "pending."** An upload is live at its chosen audience the
moment the inline checks pass. The LLM review runs *after* publication and can **retract**
— it can no longer hold anything hostage.

### 2.1 Everyone == the Store

Decided. The Store is not a destination you submit to; it is the name of the `everyone`
shelf. Consequences:

- **One verb: Share.** "Publish to the Store" disappears as a separate action.
- The Store browse page becomes a **filtered view** over audience (`everyone`), not a
  catalogue with its own admission rules.
- `store_submissions` stops being a workflow table and becomes a **safety audit log** —
  rows record "we looked, here is what we found." Nothing waits on them.
- The words *pending*, *in review*, and *approved* leave the product vocabulary.

### 2.2 Trust without a gate

Three mechanisms replace the approval queue:

- **Attribution.** Author byline on every card and detail page. "by X · Data Team · not
  reviewed" is more informative than a green check nobody audited.
- **Inline checks stay blocking.** Manifest, content, and static security scan
  ([`app/api/store.py:343`](../../../app/api/store.py)) — synchronous, free, already
  hard-reject at upload. Unchanged. This is the bulk of the real protection.
- **Retraction instead of admission.** One admin click to quarantine, plus the LLM net
  below.

Risk is further bounded by a property the current design already has: **visible ≠
running.** Serving a skill into someone's Claude Code workspace goes through
`user_store_installs` — a separate, deliberate per-person install
([`src/repositories/store_entities.py:284`](../../../src/repositories/store_entities.py)).
Discoverability does not put anything in anyone's agent.

---

## 3. Review after the fact

Chosen over "no review at all." In the happy path the two are **indistinguishable** — no
wait, no badge, no blocked author. They differ only in the rare bad case, where the net
catches it without a human noticing. Cost is negligible (Haiku default, ~$0.001/review).

The existing machinery (`src/store_guardrails/runner.py`, `llm_review.py`) is reused
as-is; only its *consequence* changes from "hold" to "retract." Four rules keep that safe.

### Rule 1 — absence of a verdict must never retract

This is the rule that matters most, because it is the current bug wearing different
clothes. Today [`llm_review.py:211`](../../../src/store_guardrails/llm_review.py) returns
"not safe" when the review **errored**:

```python
if verdict.get("error"):
    return False
```

As a gate that means *hold it.* As a retraction trigger it would mean **pull a good skill
because Redis hiccuped or the key expired.** Silent removal is strictly worse than silent
non-publication.

So: missing credentials, timeout, malformed response, exhausted quota → the entity **stays
published, marked unreviewed**, and the **operator** is alerted, not the author. Fail-open
on the audience axis, always. The boot warning can then state something true: *"uploads
are publishing unreviewed."*

### Rule 2 — only security retracts; quality never

Content quality is currently a **hard gate**
([`llm_review.py:220`](../../../src/store_guardrails/llm_review.py)): *"Content quality is
a hard gate: weak descriptions block, no severity scale."* Defensible for admission to a
curated catalogue; indefensible as grounds to yank a skill out from under colleagues using
it. A thin description is feedback to the author.

| Verdict | Action |
|---|---|
| `risk_level` safe/low, no high+ findings | nothing happens (the ~95% case) |
| `medium` findings only | advisory note to author; stays live |
| `content_quality: fail` | authoring nudge to author; stays live |
| any `high` finding | **delist** — no new installs; existing installs keep working; author notified with the finding |
| any `critical` finding | **cut** — existing installs stop serving too; author + admin notified |
| `error` / no credentials | nothing happens; operator alerted (Rule 1) |

The `high`/`critical` split is deliberate. Breaking a working agent on a medium-confidence
LLM opinion causes more damage than it prevents; stopping *new* spread while a human looks
is proportionate. `critical` earns the hard cut.

### Rule 3 — retraction must be loud and reversible

- **Author notification is mandatory**, via
  [`app/notifications.py:42`](../../../app/notifications.py). A silent delist is the same
  disease as today's silent queue.
- **One-click admin restore**, re-pointing the existing force-publish override
  ([`app/api/admin.py:4769`](../../../app/api/admin.py)) at the safety axis. LLM reviewers
  are wrong sometimes; if un-retracting is bureaucratic, trust in the surface collapses.
- **Author can fix and re-share.** Retraction is a state, not a verdict on a person.

### Rule 4 — no green badges

Show review state **only when negative or actionable**. No "✓ Reviewed" chip: the moment
it exists there are two classes of skill again and people learn to wait for the good one —
exactly the behaviour being removed. Reviewed-and-clean must look identical to
not-yet-reviewed.

---

## 4. Phases

Independently shippable. Phase 1 alone delivers team sharing with no review anywhere;
Phase 2 adds the net without changing anything a user sees in the happy path.

### Phase 0 — config unblock (**applied 2026-07-28**)

- `guardrails.enabled: false` on the dev instance, documented in
  [`config/instance.yaml`](../../../config/instance.yaml) and set in the writable overlay
  (`${DATA_DIR}/state/instance.yaml`) — the overlay is what takes effect, because the
  static file wins only where it validates.
- Two submissions stuck at `pending_llm` (`library-flow-demo`, `test`), both inline-clean,
  released via `POST /api/admin/store/submissions/{id}/override`; entities now `approved`
  with an audit trail recording why.
- No code change, so no CHANGELOG entry. Reversible by flipping the flag back.

### Phase 1 — make grants real for store entities

The actual feature. Nothing here depends on Phase 2.

- Add `STORE_ENTITY` to `ResourceType` + register a `ResourceTypeSpec` in
  [`app/resource_types.py:42`](../../../app/resource_types.py). Migration-free per the RBAC
  contract.
- Register skills in `_OWNER_RESOLVERS` in
  [`library_sharing.py`](../../../app/services/library_sharing.py) and delete the carve-out
  docstring paragraph. The owner-scoped rules (share only to groups you are in;
  admin-made grants preserved across an owner's `set_shares`) already do the right thing.
- Teach four read paths to honour grants:
  - `_enforce_visibility` ([`app/api/store.py:1363`](../../../app/api/store.py)) — one
    chokepoint for all 11 detail-page callers.
  - the `list` / `search` visibility clause
    ([`store_entities.py:683`](../../../src/repositories/store_entities.py)).
  - `list_approved_synthetic_types` (line 785) — the chat/CLI install path.
  - `list_for_serving` / `UserStoreInstallsRepository.list_for_user` (line 284) — the
    marketplace.zip + .git serving filter.
- DuckDB↔PG sibling methods and a `tests/db_pg/` contract test in the same PR.
- Migration mapping: `approved` → `everyone`; `pending` → `everyone` (they were *meant* to
  publish — honouring that intent is both correct and what un-sticks existing queues);
  `hidden` / `archived` unchanged on the safety/lifecycle axis.

### Phase 2 — retraction semantics + the share sheet

- Split `is_safe()` into `should_retract()` per the §3 table; invert the error path
  (Rule 1); narrow quality to advisory (Rule 2).
- Author notifications + admin restore (Rule 3).
- Replace `explainSkillSharing`'s apologetic toast
  ([`library.html:2037`](../../../app/web/templates/library.html)) with the same share
  sheet collections and agents already have. Badge shows **audience**
  (`Private` / `Data Team` / `Everyone`), never a review verdict.
- Fix the stale hint text in the `/admin/server-config` schema
  ([`app/api/admin.py:865`](../../../app/api/admin.py)) — it currently tells operators that
  when `ANTHROPIC_API_KEY` / `LLM_API_KEY` is absent, uploads "skip the LLM security +
  content-quality review and auto-approve." That has not been true since the fail-closed
  change (`hold_for_review = guardrails_enabled`, ignoring provider readiness), and it is
  precisely the misreading that let this bug hide. (The
  [`config/instance.yaml.example`](../../../config/instance.yaml.example) comment is
  accurate by contrast — it claims auto-approve only for `enabled: false`.)

### Explicitly not doing

- A human review queue, or notifications for approvals.
- Keeping `pending_llm` as a visibility state under a friendlier name.
- Removing `guardrails.enabled` — it stays as the opt-in for deployments that genuinely
  want a blocking gate. It simply stops being the default path, and stops being able to
  fail silently.

---

## 5. Guards

- `tests/db_pg/test_store_entities_contract.py` — extend for the new grant-aware methods
  (dual-backend discipline).
- A sweep asserting **no read path returns an entity the caller has no grant for**, per
  audience value — the leak this design must not introduce.
- A test pinning Rule 1: `verdict.error` / absent credentials → entity stays visible. This
  is the regression that would recreate the original bug.
- `tests/test_ui_layout_theme.py` conventions apply to the share-sheet markup.

---

## 6. Open questions

- Does `shared` (team-scoped) content flow through marketplace.zip / .git for grantees, or
  is the served channel `everyone`-only? The plugin path already filters by grants, so the
  mechanism exists; this is a product call about whether team-scoped skills should
  auto-appear in a member's workspace sync.
- Should an author be able to share to a group they are *not* in (request-to-publish), or
  is group containment absolute? Phase 1 assumes absolute, matching the existing
  owner-scoped model.
