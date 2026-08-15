# Wave 0: Classic-Experience Retirement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the redesign (rail chrome + paper default theme + redesigned pages) the only experience: retire the `classic` preset, the `topnav` chrome, and every `*_legacy.html` frozen surface, so later waves modify exactly one chat surface.

**Architecture:** Three concentric removals — (1) flip the `experience` switch default and drop `classic` from its options, (2) hard-wire the rail chrome (`get_ui_layout()` → `"rail"`, delete `_app_header.html`, unwrap `chat.html`), (3) delete each `*_legacy.html` template together with its router branch. The default-parity guard test is deliberately rewritten: its old contract ("default look never changes") is the thing this wave retires.

**Tech Stack:** FastAPI + Jinja2 templates, `app/switches.py` registry, pytest.

## Global Constraints

- **BREAKING** CHANGELOG bullet required (`## [Unreleased]` → Changed): instances without an explicit redesign opt-in change look on upgrade.
- No AI attribution in commits; vendor-agnostic wording everywhere.
- Full test suite runs in CI via draft PR (open it after the first commit); locally run only the named tests + `scripts/verify_syncmap.py`.
- Line numbers below are anchors as of `63db64e3e` — re-grep before editing; do not edit blind.
- Work on branch `zs/w0-legacy-retirement` in a fresh worktree (`scripts/dev/worktree-spawn.sh zs/w0-legacy-retirement origin/main`).

---

### Task 1: `experience` preset — default `redesign`, retire `classic`

**Files:**
- Modify: `app/switches.py:187-196` (the `experience` select entry)
- Test: `tests/test_ui_layout_theme.py` (class `TestResolvers`)

**Interfaces:**
- Produces: `switch_value("experience")` now returns `"redesign"` unless yaml/env explicitly says `"redesign"` (the only remaining option; anything else → `on_invalid="default"` → `"redesign"`). Every consumer (`get_experience`, `preset_knob_default`, `preset_flag_default`) flips automatically.

- [ ] **Step 1: Write the failing tests** — replace `TestResolvers`'s default expectations:

```python
class TestResolvers:
    def test_ui_layout_defaults_to_rail(self, monkeypatch):
        monkeypatch.delenv("AGNES_UI_LAYOUT", raising=False)
        monkeypatch.delenv("AGNES_INSTANCE_EXPERIENCE", raising=False)
        assert get_ui_layout() == "rail"

    def test_theme_defaults_to_paper(self, monkeypatch):
        monkeypatch.delenv("AGNES_INSTANCE_THEME", raising=False)
        monkeypatch.delenv("AGNES_INSTANCE_EXPERIENCE", raising=False)
        assert get_instance_theme() == "paper"

    def test_explicit_blue_theme_still_wins(self, monkeypatch):
        monkeypatch.setenv("AGNES_INSTANCE_THEME", "blue")
        assert get_instance_theme() == "blue"

    def test_classic_experience_falls_back_to_redesign(self, monkeypatch):
        monkeypatch.setenv("AGNES_INSTANCE_EXPERIENCE", "classic")
        from app.instance_config import get_experience
        assert get_experience() == "redesign"
```

- [ ] **Step 2: Run to verify they fail** — `pytest tests/test_ui_layout_theme.py::TestResolvers -q` → FAIL (defaults still topnav/blue/classic).
- [ ] **Step 3: Edit the switch entry** — in `app/switches.py`: `options=("classic", "redesign")` → `options=("redesign",)`; `default="classic"` → `default="redesign"`; rewrite `description` to say the preset is retired-as-a-choice and kept only so existing yaml keys don't error.
- [ ] **Step 4: Run the tests** — same command → `TestResolvers` PASSES (other classes in the file still fail; they are Task 5's job). Also run `pytest tests/test_switches*.py -q` and fix any pinned option-list assertions there.
- [ ] **Step 5: Commit** — `git commit -m "feat(web)!: default experience becomes redesign; classic retired"`

---

### Task 2: Hard-wire the rail chrome

**Files:**
- Modify: `app/instance_config.py:652-680` (`get_ui_layout`), `app/api/config_surface.py:95-98` (drop the `ui_layout` row)
- Modify: `app/web/templates/base_ds.html:38,60,65,118-123`
- Delete: `app/web/templates/_app_header.html`
- Modify: `app/web/static/js/chat_onboarding.js:40` (IS_RAIL gate)
- Test: `tests/test_ui_layout_theme.py` (rail classes), `tests/test_config_surface*.py`

**Interfaces:**
- Produces: `get_ui_layout()` always returns `"rail"`; templates may keep reading `ui_layout` from context (it is always `"rail"`), and `html[data-ui-layout="rail"]` stays stamped (rail.css keys on it).

- [ ] **Step 1: Collapse the resolver** — replace `get_ui_layout`'s body with:

```python
def get_ui_layout() -> str:
    """Structural chrome layout — always ``"rail"`` since the classic
    topnav chrome was retired (Wave 0, 2026-08). The function stays so
    template context + config surface keep one source of truth; a
    configured ``AGNES_UI_LAYOUT``/``instance.ui_layout`` is ignored
    (warned once) rather than an error, so old instance.yaml files boot."""
    raw = os.environ.get("AGNES_UI_LAYOUT") or _yaml_value("instance", "ui_layout")
    if raw and raw != "rail":
        _warn_once("ui_layout", f"instance.ui_layout={raw!r} is retired; rail chrome is always on")
    return "rail"
```

(match the module's existing yaml-lookup + warn-once helpers; if `_warn_once` doesn't exist, use the module logger with a module-level `set` guard).
- [ ] **Step 2: base_ds.html** — line 122-123: replace the `{% if %}`/include pair with an unconditional `{% include '_app_rail.html' %}`; line 38: `data-ui-layout="rail"`; lines 60/65: simplify `paper-or-rail` conditions to always-true branch.
- [ ] **Step 3: Delete `_app_header.html`**; grep `grep -rn '_app_header' app/ tests/` → expected: only comments; scrub them.
- [ ] **Step 4: chat_onboarding.js** — `const IS_RAIL = …` → delete the constant and both early-return gates (`chat_onboarding.js:1112,1136`).
- [ ] **Step 5: config_surface.py** — remove the `ui_layout` known-field row; run `pytest tests/ -k config_surface -q`, fix pinned field lists.
- [ ] **Step 6: Targeted tests** — `pytest tests/test_ui_layout_theme.py -q -k 'Rail'` → rail classes pass WITHOUT the `AGNES_UI_LAYOUT` monkeypatch (drop it where present).
- [ ] **Step 7: Commit** — `git commit -m "feat(web)!: rail chrome always on; topnav retired"`

---

### Task 3: chat.html becomes single-surface

**Files:**
- Modify: `app/web/templates/chat.html`
- Delete: `app/web/templates/_chat_welcome_cards_legacy.html`
- Test: `tests/test_web_chat*.py`, `tests/test_ui_layout_theme.py`

**Interfaces:**
- Produces: `chat.html` with zero `ui_layout` conditionals — the rail markup (rdb dashboard empty state, composer pill, plus-menu, copy-transcript, row-menu script) is unconditional. Element ids unchanged (`#chat-capabilities`, `#chat-empty-extras`, `#chat-form`, …) so `chat.js` needs no edits.

- [ ] **Step 1: Unwrap** — in `chat.html`: delete the topnav-only sidebar block (lines 108-161 `{% if ui_layout != 'rail' %}…{% endif %}`); for every `{% if ui_layout == 'rail' %}` (lines 25, 181, 212(+`{% else %}`→line 253), 272, 284, 318, 330, 341, 501, 607) keep the rail branch, drop the wrapper and any `{% else %}` topnav branch. The line-247-253 `{% else %}` welcome-cards include disappears with its `{% if %}`.
- [ ] **Step 2: Delete `_chat_welcome_cards_legacy.html`**; `grep -rn 'welcome_cards_legacy' app/ tests/` → zero hits.
- [ ] **Step 3: Render test** — `pytest tests/ -q -k 'chat and (page or render or template)'`; then boot locally (`uvicorn app.main:app`) and load `/chat`: rail chrome + rdb dashboard render; no Jinja UndefinedError.
- [ ] **Step 4: Commit** — `git commit -m "feat(web)!: chat page is single-surface (rail markup unconditional)"`

---

### Task 4: Delete legacy templates + their router branches

**Files:**
- Modify: `app/web/router.py` (branch sites listed below)
- Delete: 19 `*_legacy.html` templates + `app/web/static/js/tour_legacy.js`
- Modify/Delete: `app/web/onboarding.py` (legacy tour steps), `app/web/templates/base.html` (tour include)
- Test: `tests/test_web_*` for each touched page

**Interfaces:**
- Consumes: Task 2 (`get_ui_layout() == "rail"` always) — several branches key on it and become dead.
- Produces: `grep -rn "_legacy" app/ --include='*.py' --include='*.html'` → zero hits.

- [ ] **Step 1: Kill the detail-template selector** — `app/web/router.py:~403-421`: the function returning `f"{base}_legacy.html"` under default chrome now returns `base + ".html"` unconditionally; inline it away. Delete: `catalog_package_detail_legacy.html`, `catalog_recipe_detail_legacy.html`, `catalog_table_detail_legacy.html`, `marketplace_item_detail_legacy.html`, `marketplace_plugin_detail_legacy.html`, `memory_domain_detail_legacy.html`, `library_detail_legacy.html`.
- [ ] **Step 2: Page-by-page sweep** — for each row: make the redesign template unconditional, delete the legacy branch + file, run that page's test file.

| Router site (anchor) | Keep | Delete |
|---|---|---|
| agents (`agents_legacy` ref) | `agents.html` | `agents_legacy.html` |
| catalog (`~2028-2196`, classic contract branches) | redesign catalog semantics (rail branch) | `catalog_legacy.html` |
| corporate memory | redesign page | `corporate_memory_legacy.html` |
| library | `library.html` | `library_legacy.html` |
| marketplace | redesign page | `marketplace_legacy.html` |
| me/activity (`:1598-1601`) | `me_activity.html` unconditional | `me_activity_legacy.html` |
| me/cowork (`:1330-1400`) | unconditional 302 → `/how-it-works#connect` | `me_cowork_legacy.html` + handler body |
| profile | `profile.html` | `profile_legacy.html`, `_profile_tokens_legacy.html`, `_profile_troubleshooting_legacy.html` |
| home (`:1229` comment) | state-aware landing | legacy `home_onboarded.html` branch if the comment's classic path exists — audit, don't guess |
| tour (`:302-305`) | redesign tour (`js/tour.js`) | `_tour_legacy.html`, `js/tour_legacy.js`, the legacy-steps global + its `onboarding.py` module if now unreferenced |

- [ ] **Step 3: base.html audit** — after Step 2, `grep -rln "extends .base.html'" app/web/templates/` — every remaining extender is a live non-legacy page (e.g. `catalog_table_detail.html`, `admin_scheduler_runs.html`): base.html STAYS this wave (its retirement is real work, not a deletion — out of scope; leave the CLAUDE.md "never base.html" rule as is). Only remove the `_tour_legacy` include from it.
- [ ] **Step 4: Zero-hit gate** — `grep -rn '_legacy' app/ --include='*.py' --include='*.html' --include='*.js'` → zero; `pytest tests/ -q -k 'catalog or library or marketplace or profile or activity or cowork or agents_page'` → green.
- [ ] **Step 5: Commit** — `git commit -m "feat(web)!: delete classic legacy surfaces (templates + router branches + legacy tour)"`

---

### Task 5: Rewrite the chrome guard test

**Files:**
- Modify: `tests/test_ui_layout_theme.py`
- Test: itself

**Interfaces:**
- Produces: the new contract later waves rely on — default render IS rail+paper; `classic`/`topnav` values are tolerated-but-inert.

- [ ] **Step 0: Full default-pin sweep (T1 audit follow-up)** — beyond this file, ~19 tests across 7 files pin the retired "unset ⇒ classic" defaults (list + reasoning in `.superpowers/sdd/2026-08-14-workspace-w0-legacy-retirement/task-1-report.md`): `tests/test_stack_membership_modes.py`, `tests/db_pg/test_parity_stack.py`, `tests/test_e2e_stack_rbac.py`, `tests/test_cli_api_parity.py`, `tests/test_web_catalog_unified.py`, `tests/test_web_library.py`, `tests/test_api_sync_manifest_v49.py`; plus the T2-collateral group broken by the `_app_header.html` deletion (audit reasoning in `task-2-report.md` Concern 1): `tests/test_admin_nav_parity.py`, `tests/test_onboarding_not_outdated.py`, `tests/test_tour_onboarding_steps.py`, `tests/test_initial_workspace_api.py` — note Task 4 may already delete some of these with their legacy surfaces; whatever survives Task 4 gets the same re-pin/delete/regression triage here. For each failure: if it pins the old default, re-pin to the redesign default (auto-membership True etc.); if it asserts classic-only behavior that no longer exists, delete it with a note; if it reveals an actual regression, STOP and report. The PG parity suite is in the list — both backends' expectations move together.
- [ ] **Step 1: Delete retired classes** — `TestDefaultChromeUnchanged`, every `TestDefaultContentParity`-style assertion (`test_topnav_catalog_keeps_classic_page` at :546 and siblings), and any test monkeypatching `AGNES_UI_LAYOUT=rail` to opt in (now the default).
- [ ] **Step 2: Add the new default-contract class:**

```python
class TestRedesignIsTheOnlyExperience:
    def test_default_renders_rail_chrome(self, web_client, admin_cookie):
        html = web_client.get("/library", cookies=admin_cookie).text
        assert 'data-ui-layout="rail"' in html
        assert 'class="rail"' in html          # _app_rail.html rendered
        assert 'class="app-header"' not in html   # the marker the deleted header rendered; "_app_header" alone is a tautology

    def test_topnav_value_is_inert(self, web_client, admin_cookie, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "topnav")
        html = web_client.get("/library", cookies=admin_cookie).text
        assert 'data-ui-layout="rail"' in html
```

- [ ] **Step 3: Run the file** — `pytest tests/test_ui_layout_theme.py -q` → green.
- [ ] **Step 4: Commit** — `git commit -m "test: chrome contract = rail-only (default-parity guard retired)"`

---

### Task 6: Config surface, docs, CHANGELOG, syncmap

**Files:**
- Modify: `config/instance.yaml.example`, `CLAUDE.md` (Visual standard + Web pages paragraphs), `.claude/skills/agnes-conventions/references/web-page.md` + `design-system.md` (dual-surface guidance → single-surface), `CHANGELOG.md`
- Test: `scripts/verify_syncmap.py`, `make update-openapi-snapshot` if any endpoint docstring changed

- [ ] **Step 1: Docs sweep** — `grep -rn 'topnav\|classic' CLAUDE.md docs/ config/ .claude/skills/agnes-conventions/ | grep -vi changelog` and rewrite each hit that describes a choice that no longer exists (chrome switch, default parity, dual-surface fix rule).
- [ ] **Step 2: CHANGELOG** — under `## [Unreleased]` → `### Changed`:
  `**BREAKING**: the classic experience (topnav chrome, pre-redesign pages, legacy chat) is retired; every instance renders the redesign (rail + paper default). Explicit theme choices still win; \`instance.ui_layout\`/\`experience: classic\` are ignored with a startup warning. Stack membership defaults to auto-membership (\`features.stack_auto_membership\` default flips to true; an explicit \`false\` still wins).`
- [ ] **Step 3: Gates** — `scripts/verify_syncmap.py`; `pytest tests/test_design_system_contract.py -q`; openapi snapshot only if `gh pr checks` complains.
- [ ] **Step 4: Commit + draft PR** — `git commit -m "docs: single-surface guidance + BREAKING changelog"`; `gh pr create --draft` → watch CI (`gh pr checks --watch`, then confirm conclusions via `gh run list`).

---

## Self-review notes

- Spec coverage: Wave 0 bullet list in the spec ↔ Tasks 1-6 (switch flip ↔ T1; chrome ↔ T2; chat ↔ T3; legacy templates/router/tour ↔ T4; guard rewrite ↔ T5; BREAKING + docs ↔ T6). `home_onboarded` audit is explicitly an audit, not a guess.
- The one intentionally-kept "legacy": `base.html` (live non-legacy extenders) — documented in T4/Step 3.
- After this wave: `/agnes-review` on the diff, then release-cut decision before merge (repo rule).
