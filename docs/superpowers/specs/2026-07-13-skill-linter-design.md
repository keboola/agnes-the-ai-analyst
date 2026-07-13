# Skill curation linter — design (issue #687)

Date: 2026-07-13
Status: approved (brainstorm 2026-07-13)
Related: #688 Skill Builder (shipped v0.74.41), #317 dry-run endpoint (shipped), #277 store guardrails.

## Problem

Store submissions pass hard guardrails (spam, safety, content floors) but nothing
judges skill *craft*: descriptions that don't say when to use the skill, bloated
bodies, kitchen-sink skills covering unrelated topics, and near-duplicates of
existing store skills. Curators' feedback: "people dump everything in there."
There are also no written best-practice guidelines to point authors at.

## Decisions (from brainstorm)

1. **Scope**: lint fires on submit (dry-run + post-publish) **and** as a
   retro-audit over already-published skills.
2. **Strictness**: advisory-only. Lint never blocks publish and never changes
   `visibility_status`. Existing hard guardrails are unchanged.
3. **Duplicate detection**: lexical shortlist (DuckDB FTS/BM25 over published
   skills' name + description, building on `src/fts.py`) → top-N candidates →
   one LLM call per candidate pair to confirm real overlap. Without an LLM key
   the check degrades to lexical-only and says so in the finding.
4. **Retro-audit delivery**: weekly scheduler job persisting findings **plus**
   an admin UI with an on-demand "Audit now" trigger.

## Architecture

### 1) Lint engine — `src/store_guardrails/skill_lint.py`

One entry point:

```python
def lint_skill(entity: dict, skill_md: str, *, conn, llm: bool = True) -> LintReport
```

`LintReport` = list of findings `{rule_id, severity: "info"|"warn", message,
evidence: dict}` + engine metadata (rules run, LLM used or degraded).

The engine *composes* existing soft checks (`quality_check.check`, the advisory
parts of `content_check`) into the same finding shape (their existing rule
semantics keep working standalone — no behavior change for current callers) and
adds new rules:

| Rule | Severity | What it checks |
|---|---|---|
| SL001 trigger clarity | warn | description contains no "use when / triggers on / activates" phrasing — the description doesn't tell an agent *when* to fire the skill |
| SL002 bloat | warn | SKILL.md body over threshold (default 8000 chars; config-overridable like existing guardrail floors in `app/instance_config.py`) |
| SL003 single purpose | info | too many unrelated top-level sections (default > 6 H2s) → "consider splitting" |
| SL004 duplicate/overlap | warn | BM25 top-5 published-skill candidates above score threshold; LLM confirms overlap per pair; lexical-only fallback marked in evidence |

Rules are pure functions over (entity metadata, skill_md, candidates) —
independently unit-testable. Thresholds live next to the existing guardrail
config knobs.

### 2) Persistence

Two tables (migration in BOTH ladders — `src/db.py` `_vN_to_v(N+1)` step +
Alembic revision; endpoint must match; base was v88 at design time, re-verify
at implementation):

- `store_lint_runs`: `id`, `trigger` (`scheduler` | `admin` | `publish`),
  `started_at`, `finished_at`, `entities_linted`, `findings_count`.
- `store_lint_findings`: `id`, `run_id`, `entity_id`, `rule_id`, `severity`,
  `message`, `evidence` (JSON text), `created_at`.

New dual-backend repo pair `src/repositories/store_lint.py` + `store_lint_pg.py`
behind the factory (`store_lint_repo()`), with a cross-engine contract test
`tests/db_pg/test_store_lint_contract.py`. A new run for an entity replaces its
previous findings (`run_id` keeps history at the run level; the admin view shows
the latest run per entity).

### 3) Surfaces

- **Dry-run**: `POST /api/store/entities/dryrun` response gains a `lint` block
  (computed inline, never persisted). The Skill Builder
  (`app/web/templates/studio.html` + `studio.js`) calls dry-run before Publish
  and renders an advisory findings panel; Publish stays enabled.
- **Post-publish**: after a successful skill publish, lint runs async off the
  request path (same pattern as `_schedule_llm_review` in `app/api/store.py`)
  and persists findings under a `trigger='publish'` run.
- **Scheduler**: weekly job in `services/scheduler/__main__.py` `build_jobs()`
  (env-gated like its siblings) linting all published skills in one run.
- **Admin**: page `/admin/store/lint` (extends `base_ds.html`, spreads
  `_chrome_ctx(request, user)`) — findings table grouped per skill with
  severity + rule + recommendation, an "Audit now" button, and last-run stats.
  API: `GET /api/admin/store/lint-findings`,
  `POST /api/admin/store/lint-audit` (both `Depends(require_admin)`).
- **Triple-surface**: both admin endpoints ship CLI (`agnes admin store
  lint-findings`, `agnes admin store lint-audit`) and MCP tools
  (`admin_store_lint_findings`, `admin_store_lint_audit`) in the same PR;
  cohort entries in `tests/test_documentation_api_triple_surface.py`.

### 4) Guidelines

`docs/skill-guidelines.md` — "what belongs in a skill" best practices + a
catalogue of lint rules by rule-ID (each finding's message references its ID).
The same content is added to the `skill-author` chat profile's knowledge skill
so the builder assistant advises consistently with what the linter checks.
The store UI links the doc from the skill submit/detail views.

### 5) Error handling

- LLM failures in SL004 degrade to lexical-only (finding notes the degradation);
  they never fail the run or the publish.
- The post-publish hook and scheduler job wrap per-entity lint in try/except —
  one broken bundle logs and skips, the run continues.
- Dry-run lint runs off the event loop (same executor pattern the endpoint
  already uses for guardrail checks).

### 6) Testing

- Unit: one test module per rule (pure-function level), engine composition test.
- Repo: dual-backend contract test (both engines, same assertions).
- API: endpoint tests for the two admin routes + `lint` block in dry-run.
- Scheduler: job registration + one-run integration test.
- Browser E2E: extend the studio E2E — publish an intentionally "bad" skill
  draft, assert the lint panel shows findings, publish anyway (advisory).
- Full suite green before push (`.venv/bin/pytest tests/ --tb=short -n auto -q`).

## Out of scope (v1)

- Blocking/soft-gating on lint findings ("needs attention" badges).
- Embedding-based similarity (BM25 + LLM shortlist only).
- Linting agents/plugins/commands beyond skills (engine takes an entity type
  but only `skill` rules ship in v1).
- Author notifications (findings are visible in admin UI + entity detail only).

## CHANGELOG

One `Added` bullet under `[Unreleased]`; release-cut per repo rules on the PR
that lands it.
