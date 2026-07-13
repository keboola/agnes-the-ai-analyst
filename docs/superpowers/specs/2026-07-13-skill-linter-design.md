# Skill curation linter — design (issue #687)

Date: 2026-07-13
Status: approved (brainstorm 2026-07-13; revised same day after two independent
design reviews — technical codebase-fit + product critique, both "sound with
changes"; all findings folded in)
Related: #688 Skill Builder (shipped v0.74.41), #317 dry-run endpoint (shipped),
#277 store guardrails.

## Problem

Store submissions pass hard guardrails (spam, safety, content floors) but nothing
judges skill *craft*: descriptions that don't say when to use the skill, bloated
bodies, kitchen-sink skills covering unrelated topics, and near-duplicates of
existing store skills. Curators' feedback: "people dump everything in there."
There are also no written best-practice guidelines to point authors at.

## Decisions (from brainstorm + review round)

1. **Scope**: lint fires on submit (dry-run + post-publish) **and** as a
   retro-audit over already-published skills.
2. **Strictness**: advisory-only. Lint never blocks publish and never changes
   `visibility_status`. Existing hard guardrails are unchanged.
3. **Reach** (review round): advisory findings must reach the *author*, not
   only admins — the skill's store detail page shows its latest findings to
   the owner and admins. Without this the linter is a write-only log for
   everyone outside the Skill Builder.
4. **Rules** (review round): one mechanical rule + one holistic LLM craft
   review, instead of three regex heuristics. Regex proxies for "clear
   trigger" or "single purpose" false-positive on good skills and are
   trivially gamed; the LLM infra is already paid for (guardrail review model
   tier, ~$0.001/call on the default haiku-class tier).
5. **Duplicate detection**: lexical recall stage (BM25 over name +
   description + **SKILL.md body**) → top-N candidates → LLM confirms real
   overlap as part of the craft call. Backend-agnostic: the corpus is fetched
   through `store_entities_repo()` and indexed in a **throwaway in-memory
   DuckDB FTS index built per lint run** — works identically on DuckDB- and
   Postgres-backed instances, avoids adding another FTS index (and its
   CHECKPOINT/WAL hazards) to `system.duckdb`, and needs no cross-engine
   score-threshold calibration (plain top-N ranking, no absolute threshold).
6. **Retro-audit delivery**: weekly scheduler job persisting findings **plus**
   an admin UI with an on-demand "Audit now" trigger, with an
   acknowledge/dismiss workflow so repeated runs don't cause alarm fatigue.

## Architecture

### 1) Lint engine — `src/store_guardrails/skill_lint.py`

Pure engine, **no DB connection parameter** (the raw-`conn` pattern is the
retired backend-split bug class — see `src/store_guardrails/runner.py`'s
factory note). Callers do all repo I/O via factories and hand the engine
plain data:

```python
def lint_skill(
    entity: dict,            # metadata (id, name, description, type, ...)
    skill_md: str,
    *,
    plugin_dir: Path | None = None,   # baked tree when available (post-publish, audit)
    corpus: list[CorpusDoc],          # published skills (id, name, description, body)
    llm: bool = True,
) -> LintReport
```

`LintReport` = findings `{rule_id, severity: "info"|"warn", message,
evidence: dict, doc_url}` + engine metadata (rules run, LLM used/degraded).
`doc_url` deep-links the rule's anchor in the guidelines doc
(`docs/skill-guidelines.md#sl002`) and is rendered as a link on every surface.

The engine *composes* existing soft checks (`quality_check.check`, advisory
parts of `content_check`) into the same finding shape without changing their
standalone behavior. `quality_check.check` operates on a baked plugin tree:
when `plugin_dir` is None (markdown dry-run), the engine synthesizes a temp
tree from `skill_md` — the same synthesis the from-markdown endpoint already
does.

Rules:

| Rule | Severity | What it checks |
|---|---|---|
| SL002 bloat | warn | SKILL.md body over threshold (default 8000 chars; config knob next to existing guardrail floors in `app/instance_config.py`). Message prescribes the fix ("move detail into `references/` files"), not just the number. |
| SL010 craft (LLM) | warn/info per aspect | One holistic LLM call per skill judging: (a) trigger clarity — does the description tell an agent *when* to fire; (b) single purpose — is this one skill or several; (c) duplicate confirmation — the lexical top-N candidates' name+description are passed in the same prompt, the model marks which are real overlaps; (d) a one-sentence rewrite suggestion when (a) fails. |
| SL011 trigger phrase | info | Degraded-mode only (no LLM key): description lacks any "use when / triggers on / activates"-style phrasing. Never `warn` — it is a weak proxy kept only so keyless instances get *some* trigger signal. |
| SL012 duplicate candidates | info | Degraded-mode only: lexical top-N above rank cutoff reported as *unconfirmed* candidates, evidence notes the degradation. With LLM, confirmed overlaps surface under SL010(c) as `warn`. |

LLM specifics: reuse the guardrail review provider path
(`connectors/llm/anthropic_provider`, key via `ANTHROPIC_API_KEY`/`LLM_API_KEY`,
model tier via `guardrails.review_model`). LLM failures degrade to
SL002+SL011+SL012 and never fail the run or the publish. One LLM call per
skill total (craft + duplicate confirmation folded together).

### 2) Persistence

Three tables (migration in BOTH ladders — `src/db.py` `_vN_to_v(N+1)` step +
Alembic revision reaching the same endpoint; base was v88 at design time,
re-verify at implementation):

- `store_lint_runs`: `id`, `trigger` (`scheduler` | `admin` | `publish`),
  `started_at`, `finished_at`, `entities_linted`, `entities_skipped`,
  `findings_count`.
- `store_lint_findings`: `id`, `run_id`, `entity_id`, `rule_id`, `severity`,
  `message`, `evidence` (JSON text), `doc_url`, `content_hash`, `created_at`.
- `store_lint_dismissals`: `entity_id`, `rule_id`, `dismissed_by`,
  `dismissed_at`, `content_hash` — an admin "acknowledge/dismiss" survives
  future runs for that (entity, rule) pair **until the entity's content hash
  changes**; the admin view filters dismissed findings by default.

A new run replaces the entity's previous findings (run-level history stays in
`store_lint_runs`). **Unchanged-content skip**: an entity whose `content_hash`
AND lexical candidate shortlist are unchanged since its last lint is skipped
and its previous findings carried forward — the weekly job costs zero LLM
calls on a static store.

New dual-backend repo pair `src/repositories/store_lint.py` + `store_lint_pg.py`
behind the factory (`store_lint_repo()`), cross-engine contract test
`tests/db_pg/test_store_lint_contract.py`.

### 3) Surfaces

- **Dry-run (markdown)**: `POST /api/store/entities/from-markdown` gains a
  `dry_run: true` flag — reuses the endpoint's existing in-memory ZIP
  synthesis, runs guardrail checks + lint, returns the verdict + `lint`
  block, writes nothing. (The existing multipart
  `POST /api/store/entities/dryrun` gains the same `lint` block.) The Skill
  Builder (`app/web/templates/admin_studio.html` + `app/web/static/js/studio.js`)
  calls the dry-run before Publish and renders an advisory findings panel;
  Publish stays enabled. The classic upload page (`store_upload.html`)
  renders the same block from the multipart dry-run it already targets. The
  `agnes-skill-authoring` profile skill (`app/chat/profiles.py`) is updated
  so the builder assistant knows lint exists and pre-checks drafts against
  the guidelines.
- **Post-publish**: after a successful skill publish, lint runs via
  `BackgroundTasks` off the request path (same pattern as
  `_schedule_llm_review`), resolves repos through factories, and persists
  findings under a `trigger='publish'` run.
- **Entity detail (author reach)**: the skill's store detail page shows the
  latest findings (rule, severity, message, guideline link) to the entity
  owner and admins.
- **Scheduler**: job in `services/scheduler/__main__.py` `build_jobs()`,
  schedule string `cron 0 5 * * 1` (the grammar has no `weekly` keyword).
  The scheduler is HTTP-only by design (DuckDB single-writer): the job POSTs
  `/api/admin/store/lint-audit`; the engine runs in the app process. Because
  scheduler `last_run` state is in-memory and restarts re-fire jobs, the
  endpoint **self-guards**: if the latest `trigger='scheduler'` run is
  younger than a configurable min-interval, it returns `{skipped: true}`.
- **Admin**: page `/admin/store/lint` (extends `base_ds.html`, spreads
  `_chrome_ctx(request, user)`) — findings grouped per skill with severity +
  rule + guideline link, dismiss/acknowledge toggle, "Audit now" button,
  last-run stats, dismissed hidden by default. API:
  `GET /api/admin/store/lint-findings`, `POST /api/admin/store/lint-audit`,
  `POST /api/admin/store/lint-dismiss` (all `Depends(require_admin)`).
- **Triple-surface**: all new admin endpoints ship CLI (`agnes admin store
  lint-findings` / `lint-audit` / `lint-dismiss`) and MCP tools in the same
  PR; cohort entries in `tests/test_documentation_api_triple_surface.py`.
  The analyst-side CLI dry-run/submit path prints the `lint` block from the
  response so CLI authors see findings too.

Concurrency note: the lint engine's blocking parts (in-memory FTS build, LLM
call) run via `run_in_threadpool` — only the LLM review currently does; inline
checks run on the event loop and lint must not join them there.

### 4) Guidelines

`docs/skill-guidelines.md` — "what belongs in a skill" best practices + a
catalogue of lint rules, one anchored section per rule-ID (`#sl002`, `#sl010`,
…) so `doc_url` deep links resolve. The same content is added to the
`skill-author` profile's knowledge skill; the store submit/detail views link
the doc.

### 5) Error handling

- LLM failure → degraded rules (SL011/SL012), degradation named in the
  report; never fails the run or the publish.
- Post-publish hook and audit runs wrap per-entity lint in try/except — one
  broken bundle logs and skips, the run continues.
- Dry-run lint needs the published-skill corpus: the dry-run handlers gain a
  factory-repo corpus read (they currently have no DB dependency).

### 6) Testing

- Unit: per-rule tests (pure-function level), engine composition, degraded
  (keyless) mode, unchanged-content skip logic, dismissal survival/reset.
- Repo: dual-backend contract test (both engines, same assertions), incl.
  dismissals.
- API: dry-run `lint` block (both JSON and multipart), the three admin
  routes, self-guard skip, entity-detail findings visibility (owner vs
  stranger).
- Scheduler: job registration + cron string.
- Browser E2E: extend the studio E2E — draft an intentionally "bad" skill,
  assert the lint panel shows findings, publish anyway (advisory).
- Full suite green before push (`.venv/bin/pytest tests/ --tb=short -n auto -q`).

## Out of scope (v1)

- Blocking/soft-gating on lint findings ("needs attention" badges).
- Embedding-based similarity (lexical recall + LLM confirmation only).
- Lint *rules* for agents/plugins/commands (engine and FTS corpus are
  type-agnostic so cross-type duplicate detection can land in v2, but only
  `skill` rules ship in v1).
- Push notifications to authors (reach = entity detail page + dry-run
  surfaces in v1).

## CHANGELOG

One `Added` bullet under `[Unreleased]`; release-cut per repo rules on the PR
that lands it.
