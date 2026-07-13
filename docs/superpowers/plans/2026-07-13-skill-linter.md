# Skill Curation Linter Implementation Plan (issue #687)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Advisory skill-craft linter — lint on submit (dry-run + post-publish), weekly retro-audit with admin UI + dismiss workflow, findings reaching the author, per spec `docs/superpowers/specs/2026-07-13-skill-linter-design.md`.

**Architecture:** Pure lint engine (`src/store_guardrails/skill_lint.py`, no DB conn) composing existing soft checks + SL002 (mechanical bloat) + SL010 (one holistic LLM craft call incl. duplicate confirmation) + SL011/SL012 (keyless degraded rules). Lexical duplicate recall via a throwaway in-memory DuckDB FTS index over corpus fetched through `store_entities_repo()` (backend-agnostic). Findings persist in three new tables behind a dual-backend repo pair; surfaces = dry-run (JSON + multipart), post-publish BackgroundTasks, weekly scheduler job POSTing a self-guarded admin endpoint, `/admin/store/lint` page, owner-visible findings, CLI + MCP mirrors.

**Tech Stack:** FastAPI, DuckDB (+in-memory FTS extension), Postgres/Alembic, Jinja2 + design-system page shell, Anthropic provider (guardrail review tier), pytest.

## Global Constraints

- Advisory-only: lint NEVER blocks publish, never touches `visibility_status`.
- Engine takes NO raw DB connection — callers do repo I/O via factories (`store_entities_repo()`, `store_lint_repo()`); direct repo instantiation and `get_system_db()` are ratcheted by `tests/test_backend_split_guard.py`.
- Dual-backend discipline: every repo method lands in `store_lint.py` AND `store_lint_pg.py` in the same task, with `tests/db_pg/test_store_lint_contract.py` parametrizing both.
- One migration task only: DuckDB `_v88_to_v89` in `src/db.py` + Alembic sibling reaching the same endpoint (re-verify current `SCHEMA_VERSION` on rebased main first; renumber everywhere if it moved).
- Triple-surface: every new `/api/*` endpoint ships CLI command + MCP tool + `_COHORT` entry in `tests/test_documentation_api_triple_surface.py` + `docs/api-reference.md` line, same PR.
- Rule IDs and severities exactly: SL002 warn, SL010 warn/info, SL011 info (degraded only), SL012 info (degraded only). `doc_url` = `/docs/skill-guidelines#<rule_id lowercase>`.
- LLM: reuse `default_api_key_loader()` / `default_model_loader()` from `src/store_guardrails/runner.py`; ONE LLM call per skill; all failures degrade, never raise out of the engine.
- Blocking work (FTS build, LLM call) runs via `run_in_threadpool` in async handlers.
- Vendor-agnostic content everywhere (no customer names). No AI attribution in commits.
- Web pages: `{% extends "base_ds.html" %}`, spread `_chrome_ctx(request, user)`, CSS in `{% block head_extra %}`, `var(--ds-*)` tokens only.
- Tests: TDD per task; module suites while iterating; ONLY Task 9 runs the full suite (`.venv/bin/pytest tests/ --tb=short -n auto -q`).
- CHANGELOG bullet lands in Task 9 (deliberately deferred from Tasks 1–8).

## File Structure

```
docs/skill-guidelines.md                          (T1: guidelines + rule catalogue, anchored)
app/instance_config.py                            (T1: lint config knobs)
src/store_guardrails/skill_lint.py                (T2: engine, SL002/SL011 + composition; T4: SL010/SL012 wiring)
src/store_guardrails/lint_corpus.py               (T3: CorpusDoc + in-memory FTS + top_candidates)
src/store_guardrails/prompts.py                   (T4: craft-review prompt appended)
src/store_guardrails/craft_review.py              (T4: SL010 LLM call)
src/db.py                                         (T5: _v88_to_v89)
migrations/versions/00XX_store_lint_v89.py        (T5: Alembic sibling)
src/repositories/store_lint.py / store_lint_pg.py (T5: repo pair + factory entry)
app/api/store.py                                  (T6: dry_run flag, lint blocks, post-publish hook)
app/api/store_lint_admin.py                       (T6: admin endpoints incl. self-guard + dismiss)
app/web/router.py                                 (T7: /admin/store/lint route + owner findings ctx)
app/web/templates/admin_store_lint.html           (T7: admin page)
app/web/templates/admin_studio.html + static/js/studio.js  (T7: builder lint panel)
app/web/templates/store_upload.html               (T7: lint block in classic flow)
app/chat/profiles.py                              (T7: skill-author profile learns lint)
services/scheduler/__main__.py                    (T8: cron job tuple)
cli/commands/admin.py (or store.py — mirror existing)      (T8: CLI mirrors)
app/api/mcp_http.py                               (T8: MCP tools)
tests/…                                           (per task, see tasks)
```

---

### Task 1: Guidelines doc + lint config knobs

**Files:**
- Create: `docs/skill-guidelines.md`
- Modify: `app/instance_config.py` (append next to `get_guardrails_min_description_chars`)
- Modify: `config/instance.yaml.example` (document the knobs under `guardrails:`)
- Test: `tests/test_instance_config.py` (append class)

**Interfaces:**
- Produces: `get_lint_max_body_chars() -> int` (default **8000**), `get_lint_duplicate_top_n() -> int` (default **5**), `get_lint_audit_min_interval_hours() -> int` (default **144**). Doc anchors `#sl002 #sl010 #sl011 #sl012` in `docs/skill-guidelines.md`.

- [ ] **Step 1: Write failing tests** — mirror the existing guardrail-knob tests in `tests/test_instance_config.py` (find the `get_guardrails_min_description_chars` test class and copy its override/default pattern):

```python
class TestLintKnobs:
    def test_defaults(self):
        assert get_lint_max_body_chars() == 8000
        assert get_lint_duplicate_top_n() == 5
        assert get_lint_audit_min_interval_hours() == 144

    def test_yaml_override(self, tmp_instance_yaml):  # reuse the module's existing override fixture/pattern
        # guardrails: {lint_max_body_chars: 4000}
        assert get_lint_max_body_chars() == 4000
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_instance_config.py -q -k Lint` — expect FAIL (ImportError).
- [ ] **Step 3: Implement** the three getters in `app/instance_config.py`, exact copy of the `get_guardrails_min_description_chars` pattern (yaml path `guardrails.lint_*`, int coercion, default fallback). Document in `config/instance.yaml.example` under the guardrails section.
- [ ] **Step 4: Write `docs/skill-guidelines.md`**: a "What belongs in a skill" best-practices section (one purpose per skill; description states *when to use it* — trigger conditions, not a summary; keep the body lean, move detail to `references/` files; don't re-upload near-copies — extend the existing skill) followed by `## Rule catalogue` with one `### SL002 — bloat` / `### SL010 — craft review` / `### SL011 — trigger phrase (degraded)` / `### SL012 — duplicate candidates (degraded)` section each (anchor = lowercase rule id), each stating what fires it and how to fix it. Vendor-agnostic.
- [ ] **Step 5: Run** `.venv/bin/pytest tests/test_instance_config.py -q` — PASS. **Commit** `feat(store): lint config knobs + skill guidelines doc`.

---

### Task 2: Lint engine core — SL002, SL011, composition, hashing

**Files:**
- Create: `src/store_guardrails/skill_lint.py`
- Test: `tests/test_skill_lint.py`

**Interfaces:**
- Consumes: `quality_check.check(plugin_dir, *, description)` (existing), Task 1 knobs.
- Produces (Tasks 3,4,5,6 rely on these exact names):

```python
class LintFinding(TypedDict):
    rule_id: str            # "SL002" | "SL010" | "SL011" | "SL012" | quality-check passthrough ids
    severity: str           # "info" | "warn"
    message: str
    evidence: dict
    doc_url: str            # "/docs/skill-guidelines#sl002"

class LintReport(TypedDict):
    findings: list[LintFinding]
    rules_run: list[str]
    llm_used: bool
    content_hash: str

def compute_content_hash(skill_md: str) -> str          # sha256 hex of skill_md.strip()

def lint_skill(
    entity: dict,                     # needs: id?, name, description (may be None), type
    skill_md: str,
    *,
    plugin_dir: Path | None = None,   # baked tree when caller has one; None → synthesize temp tree
    candidates: list[tuple["CorpusDoc", float]] | None = None,  # lexical top-N (Task 3); None → skip dup rules
    craft: "CraftCaller | None" = None,   # Task 4 injectable; None → degraded mode (SL011/SL012)
) -> LintReport
```

- [ ] **Step 1: Write failing tests** in `tests/test_skill_lint.py`:

```python
from src.store_guardrails.skill_lint import lint_skill, compute_content_hash

_GOOD = {"name": "demo-skill", "description": "Use when importing call transcripts into the CRM and matching participants to accounts.", "type": "skill"}

def _md(body_len=300):
    return "---\nname: demo-skill\ndescription: x\n---\n\n# Demo\n\n" + ("word " * (body_len // 5))

def test_clean_skill_no_warn_findings():
    r = lint_skill(_GOOD, _md(), candidates=[])
    assert r["content_hash"] == compute_content_hash(_md())
    assert not [f for f in r["findings"] if f["severity"] == "warn"]

def test_sl002_fires_over_threshold():
    r = lint_skill(_GOOD, _md(body_len=9000), candidates=[])
    f = next(f for f in r["findings"] if f["rule_id"] == "SL002")
    assert f["severity"] == "warn" and "references/" in f["message"]
    assert f["doc_url"] == "/docs/skill-guidelines#sl002"

def test_sl011_degraded_only_and_info():
    bad = dict(_GOOD, description="A collection of many helpful things.")
    r = lint_skill(bad, _md(), candidates=[])          # craft=None → degraded
    f = next(f for f in r["findings"] if f["rule_id"] == "SL011")
    assert f["severity"] == "info"
    assert r["llm_used"] is False

def test_quality_check_composed_via_temp_tree():
    r = lint_skill(_GOOD, "---\nname: demo-skill\n---\n\nTODO: write me", candidates=[])
    assert any("placeholder" in f["message"].lower() or "todo" in f["message"].lower() for f in r["findings"])

def test_engine_never_raises_on_broken_input():
    r = lint_skill({"name": "x", "description": None, "type": "skill"}, "", candidates=[])
    assert isinstance(r["findings"], list)
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_skill_lint.py -q` — FAIL (module missing).
- [ ] **Step 3: Implement** `skill_lint.py`: SL002 vs `get_lint_max_body_chars()` (message: "SKILL.md body is N chars (limit M). Move detail into references/ files the agent loads on demand."); SL011 regex `re.search(r"\b(use when|use this when|triggers? on|activates? when|invoke when)\b", desc, re.I)` — only when `craft is None`, severity info; composition: when `plugin_dir is None`, synthesize `tempfile` tree `<name>/SKILL.md` (same shape `create_entity_from_markdown` bakes) and call `quality_check.check`, mapping its findings into `LintFinding` with passthrough rule ids (`QC-*` per its existing keys) and doc_url to the guidelines root; every rule body wrapped so one rule's exception logs + skips (engine never raises). Candidates/craft handling arrives in Tasks 3–4 — accept the params now, ignore `candidates` beyond passing-through, and treat `craft=None` as degraded.
- [ ] **Step 4: Run** the module tests — PASS. **Commit** `feat(store): skill lint engine core (SL002, SL011, composition)`.

---

### Task 3: Lexical duplicate stage — corpus + in-memory FTS

**Files:**
- Create: `src/store_guardrails/lint_corpus.py`
- Modify: `src/store_guardrails/skill_lint.py` (SL012 from candidates when degraded)
- Test: `tests/test_lint_corpus.py`

**Interfaces:**
- Consumes: `ensure_fts_loaded(conn)` from `src/fts.py`.
- Produces:

```python
class CorpusDoc(TypedDict):
    id: str
    name: str
    description: str
    body: str        # SKILL.md text ("" if unreadable)

def top_candidates(
    name: str, description: str, body: str,
    corpus: list[CorpusDoc], *, n: int, exclude_id: str | None = None,
) -> list[tuple[CorpusDoc, float]]     # BM25-ranked, best first; [] on any FTS failure

def load_corpus() -> list[CorpusDoc]   # store_entities_repo() published skills + SKILL.md read from the baked plugin dir
```

- [ ] **Step 1: Write failing tests** in `tests/test_lint_corpus.py`:

```python
from src.store_guardrails.lint_corpus import top_candidates

_CORPUS = [
    {"id": "1", "name": "gong-import", "description": "Import Gong call transcripts into the CRM", "body": "Import call transcripts, match participants to accounts, MEDDPICC insights."},
    {"id": "2", "name": "weather", "description": "Fetch weather forecasts", "body": "Query the forecast API for a city."},
]

def test_body_similarity_recalls_renamed_duplicate():
    # same body, fresh AI name+description — the case name/description-only search misses
    got = top_candidates("call-helper", "Assists with sales call data", "Import call transcripts, match participants to accounts, MEDDPICC insights.", _CORPUS, n=5)
    assert got and got[0][0]["id"] == "1"

def test_unrelated_returns_low_or_empty():
    got = top_candidates("weather-two", "Fetch weather forecasts", "Query the forecast API.", _CORPUS, n=5, exclude_id="2")
    assert all(c["id"] != "2" for c, _ in got) or got == []

def test_fts_failure_degrades_to_empty(monkeypatch):
    monkeypatch.setattr("src.store_guardrails.lint_corpus.ensure_fts_loaded", lambda conn: False)
    assert top_candidates("x", "y", "z", _CORPUS, n=5) == []

def test_sl012_degraded_finding():
    from src.store_guardrails.skill_lint import lint_skill
    cands = [(_CORPUS[0], 3.2)]
    r = lint_skill({"name": "call-helper", "description": "Use when importing call data into the CRM system.", "type": "skill"}, "---\n---\n\nbody", candidates=cands)
    f = next(f for f in r["findings"] if f["rule_id"] == "SL012")
    assert f["severity"] == "info" and "gong-import" in f["message"]
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_lint_corpus.py -q` — FAIL.
- [ ] **Step 3: Implement** `lint_corpus.py`: `duckdb.connect(":memory:")`, `CREATE TABLE corpus(id VARCHAR, name VARCHAR, description VARCHAR, body VARCHAR)`, bulk insert, `ensure_fts_loaded(conn)` then `PRAGMA create_fts_index('main.corpus', 'id', 'name', 'description', 'body', strip_accents=1, lower=1, overwrite=1)` (in-memory ⇒ NO CHECKPOINT concerns), query `fts_main_corpus.match_bm25(id, ?)` with `query = name + " " + description + " " + body[:2000]`, ORDER BY score DESC LIMIT n, filter `exclude_id`, whole thing in try/except → `[]`. `load_corpus()` uses `store_entities_repo()` list of published skills and reads each entity's baked `SKILL.md` via the store's plugin-dir helper (`_plugin_dir` in `app/api/store.py` — import the path helper, or replicate the path join from DATA_DIR; verify the helper's actual home and reuse, don't fork the logic). In `skill_lint.py`, add SL012: when `craft is None` and `candidates`, emit one info finding listing candidate names + scores with evidence `{"candidates": [...], "degraded": true}`.
- [ ] **Step 4: Run** module tests + `tests/test_skill_lint.py` — PASS. **Commit** `feat(store): lexical duplicate recall via in-memory FTS corpus`.

---

### Task 4: SL010 — holistic LLM craft review

**Files:**
- Create: `src/store_guardrails/craft_review.py`
- Modify: `src/store_guardrails/prompts.py` (append `CRAFT_REVIEW_PROMPT`)
- Modify: `src/store_guardrails/skill_lint.py` (wire `craft` caller)
- Test: `tests/test_craft_review.py`

**Interfaces:**
- Consumes: the provider invocation style of `llm_review.review_bundle` (`src/store_guardrails/llm_review.py:43` — same Anthropic client construction, JSON-verdict parsing with `_normalize`-style hardening), `default_api_key_loader()`/`default_model_loader()` (`runner.py`), Task 3 `CorpusDoc`.
- Produces:

```python
CraftCaller = Callable[[dict, str, list[tuple[CorpusDoc, float]]], list[LintFinding]]

def craft_review(entity: dict, skill_md: str, candidates, *, api_key: str, model: str) -> list[LintFinding]
def default_craft_caller() -> CraftCaller | None    # None when no key / guardrails LLM not ready
```

`craft_review` returns SL010 findings: trigger-clarity (warn, message includes the model's one-sentence rewrite suggestion), single-purpose (warn), confirmed-duplicate (warn, evidence names the confirmed entity ids), each `doc_url` → `#sl010`. Empty list = clean. Any exception → raise nothing: return `[{"rule_id": "SL010", "severity": "info", "message": "LLM craft review unavailable (…)", "evidence": {"degraded": True}, "doc_url": …}]`? **No** — on failure return `[]` and let `lint_skill` fall back to SL011/SL012 exactly as if `craft` were None (single degradation path, no special finding).

- [ ] **Step 1: Write failing tests** with a stubbed Anthropic client (mirror how `tests/` stub `llm_review` — find its existing test module and reuse the stub fixture): prompt receives name+description+body+candidate list; a canned JSON verdict `{"trigger_clear": false, "trigger_rewrite": "Use when …", "single_purpose": true, "duplicates": ["1"]}` maps to exactly 2 findings (SL010 trigger warn with rewrite text in message; SL010 duplicate warn with entity id 1 in evidence); malformed JSON → `[]`; `lint_skill(..., craft=stub)` sets `llm_used=True` and suppresses SL011/SL012.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** (`CRAFT_REVIEW_PROMPT` in prompts.py demands strict JSON; parse defensively like `_normalize_content_quality`). **Step 4: Run** `tests/test_craft_review.py tests/test_skill_lint.py -q` — PASS. **Commit** `feat(store): SL010 holistic LLM craft review`.

---

### Task 5: Migration v89 + dual-backend store_lint repo

**Files:**
- Modify: `src/db.py` (SCHEMA_VERSION → 89, `_v88_to_v89`)
- Create: `migrations/versions/00XX_store_lint_v89.py` (next number after current head; `alembic heads` to confirm)
- Create: `src/repositories/store_lint.py`, `src/repositories/store_lint_pg.py`
- Modify: `src/repositories/__init__.py` (factory `store_lint_repo()` dispatch entry)
- Test: `tests/db_pg/test_store_lint_contract.py`
- **First step: re-verify `SCHEMA_VERSION` is still 88 on the rebased branch; if it moved, renumber v89→vN+1 consistently across db.py, Alembic, and this plan's references.**

**Tables (both ladders, identical endpoint):**

```sql
CREATE TABLE store_lint_runs (
  id VARCHAR PRIMARY KEY, trigger VARCHAR NOT NULL,          -- scheduler|admin|publish
  started_at TIMESTAMP NOT NULL, finished_at TIMESTAMP,
  entities_linted INTEGER DEFAULT 0, entities_skipped INTEGER DEFAULT 0, findings_count INTEGER DEFAULT 0);
CREATE TABLE store_lint_findings (
  id VARCHAR PRIMARY KEY, run_id VARCHAR NOT NULL, entity_id VARCHAR NOT NULL,
  rule_id VARCHAR NOT NULL, severity VARCHAR NOT NULL, message VARCHAR NOT NULL,
  evidence VARCHAR DEFAULT '{}', doc_url VARCHAR DEFAULT '', content_hash VARCHAR DEFAULT '',
  created_at TIMESTAMP NOT NULL);
CREATE TABLE store_lint_dismissals (
  entity_id VARCHAR NOT NULL, rule_id VARCHAR NOT NULL, dismissed_by VARCHAR NOT NULL,
  dismissed_at TIMESTAMP NOT NULL, content_hash VARCHAR NOT NULL, PRIMARY KEY (entity_id, rule_id));
```

**Interfaces (both classes identical; Tasks 6–8 rely on these exact names):**

```python
class StoreLintRepository:
    def start_run(self, trigger: str) -> str
    def finish_run(self, run_id: str, *, linted: int, skipped: int, findings: int) -> None
    def replace_findings(self, entity_id: str, run_id: str, findings: list[LintFinding], content_hash: str) -> None
    def carry_forward(self, entity_id: str, new_run_id: str) -> None            # re-tag latest findings to new run
    def latest_findings(self, entity_id: str, *, include_dismissed: bool = True) -> list[dict]
    def all_latest_findings(self, *, include_dismissed: bool = False) -> list[dict]   # grouped feed for admin UI/CLI
    def last_content_hash(self, entity_id: str) -> str | None
    def dismiss(self, entity_id: str, rule_id: str, user_id: str, content_hash: str) -> None
    def is_dismissed(self, entity_id: str, rule_id: str, content_hash: str) -> bool   # hash mismatch ⇒ False (auto-reset)
    def last_run(self, trigger: str | None = None) -> dict | None
    def delete_for_entity(self, entity_id: str) -> None                          # store delete hook parity
```

- [ ] **Step 1: Write the contract test** `tests/db_pg/test_store_lint_contract.py` mirroring the newest existing contract file's fixture pattern (find `tests/db_pg/test_*_contract.py` most recently added — e.g. the authoring-suggestions one — and copy its both-backends parametrization): run lifecycle (start→replace→finish→last_run), latest_findings replacement semantics, carry_forward re-tags, dismissal filtering + content-hash auto-reset, delete_for_entity.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/db_pg/test_store_lint_contract.py -q` — FAIL. (PG-side cases auto-skip without a live PG, same as siblings.)
- [ ] **Step 3: Implement** migration + both repos + factory entry. **Step 4: Run** contract test + `tests/test_db_schema_version.py` + `tests/test_backend_split_guard.py` — PASS. **Commit** `feat(store): store_lint tables v89 + dual-backend repo`.

---

### Task 6: API surfaces — dry-run lint, post-publish hook, admin endpoints

**Files:**
- Modify: `app/api/store.py` (dry_run flag on from-markdown; `lint` block in `dryrun_entity`; post-publish hook; delete hook calls `delete_for_entity`)
- Create: `app/api/store_lint_admin.py` (router; register in `app/main.py` next to the store router)
- Test: `tests/test_store_lint_api.py`

**Interfaces:**
- Consumes: Tasks 2–5 exports; `run_in_threadpool`; `BackgroundTasks` pattern of `_schedule_llm_review` (`app/api/store.py:403`).
- Produces:
  - `POST /api/store/entities/from-markdown` body gains `dry_run: bool = False`. When true: run name/frontmatter synthesis as today, then inline checks + `lint_skill` (corpus via `load_corpus()`, candidates via `top_candidates`, craft via `default_craft_caller()`) — return `{"dry_run": true, "inline": …, "lint": LintReport}` with **no DB writes, no create_entity call**, status 200.
  - `dryrun_entity` response gains `lint: LintReport` (computed from the baked tree's SKILL.md when `type == "skill"`; other types omit the key).
  - Post-publish: in `create_entity` success path for skills, `background_tasks.add_task(_run_publish_lint, entity_id)` — helper fetches skill_md from the baked dir, lints, `start_run("publish")` → `replace_findings` → `finish_run`; wrapped in try/except (log + drop).
  - `app/api/store_lint_admin.py`: `GET /api/admin/store/lint-findings` (→ `all_latest_findings()`, query param `include_dismissed`), `POST /api/admin/store/lint-audit` (runs the full audit inline: corpus once, loop entities with content-hash skip + carry_forward, per-entity try/except; body `{"force": bool}`; **self-guard**: when invoked and `last_run("scheduler" if header X-Scheduler else None)` — simpler: when NOT `force` and the latest run of ANY trigger is younger than `get_lint_audit_min_interval_hours()`, return `{"skipped": true, "last_run": …}`), `POST /api/admin/store/lint-dismiss` (body `{entity_id, rule_id}`, records dismissal with the finding's content_hash). All three `Depends(require_admin)`.

- [ ] **Step 1: Write failing tests** in `tests/test_store_lint_api.py` (reuse `TestCreateFromMarkdown`'s client/user fixtures from `tests/test_store_api.py`): dry_run=true returns lint block + writes nothing (store listing unchanged); dry_run response includes SL002 for a 9000-char body; publish then poll: findings persisted with trigger publish (BackgroundTasks run synchronously under TestClient); admin audit happy path + `skipped` self-guard + `force` override; dismiss hides the finding from `include_dismissed=false` listing and republish-with-changed-body resurrects it; all three admin routes 403 for non-admin.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** (lint calls via `run_in_threadpool` inside async handlers; corpus loaded once per request/audit). **Step 4: Run** `tests/test_store_lint_api.py tests/test_store_api.py -q` — PASS. **Commit** `feat(store): lint on dry-run + post-publish + admin lint endpoints`.

---

### Task 7: Web surfaces — admin page, builder panel, upload page, owner view, profile

**Files:**
- Modify: `app/web/router.py` (route `/admin/store/lint`; owner-findings context on the entity edit view ~`store_edit.html` route at router.py:2289)
- Create: `app/web/templates/admin_store_lint.html`
- Modify: `app/web/templates/admin_studio.html` + `app/web/static/js/studio.js` (pre-Publish dry-run call + advisory panel; Publish stays enabled)
- Modify: `app/web/templates/store_upload.html` (render `lint` block from the multipart dry-run it already targets)
- Modify: `app/web/templates/store_edit.html` (owner-visible latest findings with doc_url links)
- Modify: `app/chat/profiles.py` (skill-author profile: mention lint + guidelines, "run a dry-run before Publish and address advisory findings")
- Test: `tests/test_web_store_lint.py`

**Interfaces:** consumes Task 6 endpoints + Task 5 repo (web routes read via `store_lint_repo()`); admin page extends `base_ds.html`, spreads `_chrome_ctx(request, user)` (**mandatory — without it the page renders unstyled with no nav**).

- [ ] **Step 1: Write failing tests**: `/admin/store/lint` 200 for admin (contains "Audit now" + a seeded finding + hides a dismissed one), 403/redirect for non-admin; store_edit shows the owner their findings incl. `doc_url` href, and a non-owner non-admin doesn't see them; profile body mentions dry-run/lint; design-contract sweep stays green (`tests/test_design_system_contract.py`).
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** Builder panel in `studio.js`: on Publish click for the skill domain, first `fetch(endpoint, {…, body: JSON.stringify({...payload, dry_run: true})})`, render `lint.findings` as a dismissible advisory list (severity badge + message + guideline link), then proceed with the real POST on confirm — single guard against double-submit. **Step 4:** module tests PASS; **screenshot the admin page and the builder panel against a running dev server before calling UI done** (memory: `_chrome_ctx` regressions are invisible to tests). **Commit** `feat(store): lint web surfaces (admin page, builder panel, owner view)`.

---

### Task 8: Scheduler job + CLI + MCP + cohort

**Files:**
- Modify: `services/scheduler/__main__.py` (`build_jobs()`: `("store-lint-audit", "cron 0 5 * * 1", "/api/admin/store/lint-audit", "POST", 900)`)
- Modify: CLI — mirror the existing admin command home (check `cli/commands/` for where admin store commands live; follow `agnes admin …` conventions): `agnes admin store lint-findings [--include-dismissed]`, `agnes admin store lint-audit [--force]`, `agnes admin store lint-dismiss <entity_id> <rule_id>`; the analyst dry-run/publish-md path prints the `lint` block when present.
- Modify: `app/api/mcp_http.py` (tools `admin_store_lint_findings`, `admin_store_lint_audit`, `admin_store_lint_dismiss` — self-call pattern with `_headers()`; update `test_exact_server_side_tool_set` in the same commit)
- Modify: `tests/test_documentation_api_triple_surface.py` (`_COHORT` entries for all three endpoints), `docs/api-reference.md`
- Test: `tests/test_cli_store_lint.py` + additions to `tests/test_mcp_http.py`

- [ ] **Step 1: Failing tests** (cohort entries make `test_mcp_tools_registered`/CLI checks red; CLI tests mock `api_get/api_post` like `tests/test_cli_store.py` does; scheduler: assert `build_jobs()` contains the tuple).
- [ ] **Step 2 → 4:** implement, run `tests/test_documentation_api_triple_surface.py tests/test_cli_store_lint.py tests/test_mcp_http.py services` module tests — PASS. **Commit** `feat(store): lint scheduler job + CLI + MCP mirrors`.

---

### Task 9: Browser E2E + CHANGELOG + full suite

**Files:**
- Modify: `tests/e2e/test_studio_web.py` (extend the existing skill-builder case: draft with a 9000-char body → lint panel shows SL002 → publish anyway → entity exists = advisory proven)
- Modify: `CHANGELOG.md` (one `Added` bullet under `[Unreleased]`: skill curation linter — advisory lint on dry-run/publish, weekly audit, `/admin/store/lint`, CLI + MCP mirrors, `docs/skill-guidelines.md`; reference issue #687)
- Full suite: `.venv/bin/pytest tests/ --tb=short -n auto -q`

- [ ] **Step 1:** E2E case (env-gated like its siblings; verify clean SKIP without gates). **Step 2:** run live against the docker stack if available; otherwise document skip-verified. **Step 3:** CHANGELOG bullet. **Step 4:** full suite — triage every failure (fix if touched by this branch; verify pre-existing otherwise and list in report). **Commit** `feat(store): skill linter E2E + changelog`.

---

## Self-Review (done at planning time)

- Spec coverage: decisions 1–6 → T6 (submit+audit+self-guard), advisory (global constraint), reach/T7 (owner view, upload page, builder), rules/T2–T4, corpus/T3, dismissals/T5+T6+T7, scheduler/T8, guidelines/T1, triple-surface/T8, testing/T9. Entity-detail = `store_edit.html` owner view (T7) — closest existing owner-facing detail surface; if a public store detail page exists on rebased main, prefer it (implementer verifies).
- No placeholders: every rule threshold, endpoint path, tuple, and table column is concrete; "verify against current main" hedges are limited to volatile facts (SCHEMA_VERSION, Alembic head number, CLI file home, detail-surface template) and say exactly what to verify.
- Type consistency: `LintFinding/LintReport/CorpusDoc/StoreLintRepository` signatures repeated verbatim in consuming tasks.
