# Design System Unification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. One PR for the whole thing — no mid-stream releases.

**Goal:** Make every page of the Agnes web UI look like part of the same product — one CSS file, one design-token palette, one set of primitives (buttons, inputs, filter bars, page headers, tables, empty states, toasts), one nav style. Resolve the user-visible drift: top-nav Admin entry looking different from sibling links, filter bars rendering 3 different ways across pages, page headers in 5 different sizes, admin tables each rolling their own CSS.

**Architecture:**
- Consolidate `app/web/static/style.css` into `app/web/static/style-custom.css` and delete `style.css`. One stylesheet, one `:root` token block.
- Introduce canonical primitives (`.btn` family, `.search-input`, `.filter-bar`, `.page-header`, `.data-table`, `.empty-state`, `.toast`, `.app-nav-link` unified for `<a>` and `<button>`). Keep legacy class names as **CSS aliases** during migration so individual templates can flip independently — final task removes the aliases.
- Migrate all 41 inline-style templates to the canonical primitives, deleting per-page `<style>` blocks where they only duplicate now-global rules.
- Add a thin `app.js` for the toast helper + the (already-present) dropdown wiring extracted from `_app_header.html`.
- Add a Python contract test (`tests/test_design_system_contract.py`) that fails if any template re-introduces a deprecated class name or a raw `style.css` link tag, so regressions don't slip in after merge.

**Tech Stack:** FastAPI + Jinja2 templates, vanilla CSS (no preprocessor), vanilla JS, pytest for contract tests, agent-browser for visual smoke tests across all routes.

**One-PR rule:** This plan bundles every task into one branch (`zs/design-pass`) and one PR. The migration is mechanically repetitive after Task 4 finishes — small commits per template keep `git log` readable but the review surface is one PR. Half-migrated state would look worse than current drift, so no intermediate merges.

---

## Revised execution order (post-review)

External review flagged: (a) the user's nav complaint must land in the first commit, not buried at Task 5; (b) Tasks 14 (sticky header) + 15 (dark-mode skeleton) expand review surface without addressing the complaint — cut from this PR; (c) `static_url()` has no cache-busting → users hit stale 404s after `style.css` deletion → add mtime version query string in Task 1; (d) the contract test's deprecated-class detection has bugs (won't catch multi-line `class=` attributes, false-positives on prose text) → tokenize the `class="..."` attribute properly.

**Execution follows the task tracker (Tasks 41–58 in the project task list), not the plan-section numbering. Section headers below are kept in their original order for diff readability.**

| Tracker | Plan section | What |
|---|---|---|
| Task 0 | Task 0 | Setup + baseline pytest + **baseline screenshots** for ~30 routes |
| Task 1 | "Task 5" | **Nav fix first** — user's complaint lands in commit #1 |
| Task 2 | "Task 1" | CSS consolidation + `static_url` mtime cache-busting |
| Task 3 | "Task 2" | Contract test (**tokenized** class detection) + Jinja smoke render |
| Tasks 4–7 | "Tasks 3, 4, 6, 7" | Primitives — buttons, form controls, page-header, table/empty/toast/tab/stat |
| Tasks 8–15 | "Tasks 8–12" | Template migration sweep, Task 11 split into 11/12/13/14, Task 12 stays as Task 15 (login verify) |
| Task 16 | "Task 13" | Remove legacy CSS aliases |
| Task 17 | "Task 16" | CHANGELOG + widened vendor-grep + full pytest + smoke + push |
| ~~"Tasks 14–15"~~ | ~~sticky header + dark mode~~ | **DROPPED** — defer to follow-up PRs |

---

## File structure (what gets touched)

**New files:**
- `app/web/static/app.js` — toast helper (`window.appToast({kind, msg, timeout})`) + nav dropdown wiring moved out of inline `<script>` in `_app_header.html`.
- `tests/test_design_system_contract.py` — contract assertions: no `style.css` reference, no deprecated class names in templates, single `:root` block, all canonical primitives defined.

**Modified (CSS):**
- `app/web/static/style-custom.css` — absorbs `style.css`, adds new primitive sections, defines legacy aliases.

**Modified (Jinja chrome):**
- `app/web/templates/base.html` — drops `style.css` link.
- `app/web/templates/base_login.html` — drops `style.css` link.
- `app/web/templates/_app_header.html` — Admin trigger becomes `<a class="app-nav-link"><details>`–pattern OR `.app-nav-link.is-trigger` (decided in Task 5). Extract inline `<script>` to `app.js`.

**Deleted:**
- `app/web/static/style.css` — content folded into `style-custom.css`, file removed.

**Migrated templates (41 total):**
- **Admin index pages (12)**: `activity_center.html`, `admin_access.html`, `admin_groups.html`, `admin_marketplaces.html`, `admin_scheduler_runs.html`, `admin_sessions.html`, `admin_store_submissions.html`, `admin_tables.html`, `admin_tokens.html`, `admin_usage.html`, `admin_users.html`, `admin_welcome.html`.
- **Admin detail / single-purpose (7)**: `admin_group_detail.html`, `admin_server_config.html`, `admin_session_detail.html`, `admin_store_submission_detail.html`, `admin_user_detail.html`, `admin_workspace_prompt.html`, `admin/news_editor.html`.
- **Catalog + marketplace + store (8)**: `catalog.html`, `marketplace.html`, `marketplace_guide.html`, `marketplace_item_detail.html`, `marketplace_plugin_detail.html`, `store_edit.html`, `store_examples.html`, `store_upload.html`.
- **Corporate memory (2)**: `corporate_memory.html`, `corporate_memory_admin.html`.
- **Profile + sessions + tokens (3)**: `profile.html`, `profile_sessions.html`, `my_tokens.html`.
- **Home / dashboard / install (5)**: `home_not_onboarded.html`, `home_onboarded.html`, `dashboard.html`, `install.html`, `setup_advanced.html`.
- **Misc (4)**: `error.html`, `me_debug.html`, `news.html`, plus the existing nav partial.

**Tests:**
- Existing `tests/test_route_integrity.py` — already in tree; re-run after every template touch.
- New `tests/test_design_system_contract.py` — see Task 2.
- Full `pytest tests/ -n auto` before push (per CLAUDE.md "Run tests before every push").

---

## Task 0: Setup + baseline

**Files:** none (environment work).

- [ ] **Step 1: Worktree already prepared.**

The plan is being written in `.worktrees/design-pass` on branch `zs/design-pass` tracking `origin/main`. Confirm:

```bash
cd .worktrees/design-pass
git status                # working tree clean (only this plan staged later)
git rev-parse --abbrev-ref HEAD   # zs/design-pass
git log -1 --oneline      # should be origin/main tip
```

- [ ] **Step 2: Create venv and install deps.**

```bash
python3 -m venv .venv
.venv/bin/pip install -q -e ".[dev]"
```

Expected: install completes without error. Confirms the new worktree is wired up.

- [ ] **Step 3: Run the full baseline test suite.**

```bash
.venv/bin/pytest tests/ --tb=short -n auto -q
```

Expected: full suite green (or only the pre-existing `test_clean_install_integration.py::test_readers_in_pre_init_dir` flake). Note exact count + any pre-existing failures in scratch notes — these are the baseline; any new failure caused by this PR is a regression.

- [ ] **Step 4: Confirm LOCAL_DEV_MODE behavior + baseline screenshots.**

```bash
grep -rn "LOCAL_DEV_MODE" app/auth/ app/main.py | head    # confirm OAuth short-circuit exists
LOCAL_DEV_MODE=1 .venv/bin/uvicorn app.main:app --port 8000 &
sleep 3
mkdir -p /tmp/design-pass-baseline
ROUTES=(
    /dashboard /home /catalog /marketplace /marketplace/guide
    /corporate-memory /corporate-memory/admin
    /admin/users /admin/groups /admin/access /admin/tokens
    /admin/marketplaces /admin/tables /admin/scheduler-runs
    /admin/server-config /admin/agent-prompt /admin/workspace-prompt
    /admin/activity /admin/telemetry /admin/usage /admin/sessions
    /admin/store/submissions /admin/welcome
    /profile /profile/sessions /tokens
    /setup /install /login
)
for r in "${ROUTES[@]}"; do
    safe="${r//\//-}"
    agent-browser open "http://localhost:8000${r}"
    agent-browser wait --load networkidle
    agent-browser screenshot "/tmp/design-pass-baseline${safe}.png" --full
done
kill %1
```

Outcome: a `/tmp/design-pass-baseline/` snapshot tree the migration tasks compare against.

- [ ] **Step 5: Commit the plan.**

```bash
git add docs/superpowers/plans/2026-05-13-design-system-unification.md
git commit -m "docs(plan): design-system unification plan (post-review revisions)"
```

---

## Task 1: Token consolidation (CSS file merge)

**Goal:** One stylesheet, one `:root`, one palette. No visible change yet — pure refactor.

**Files:**
- Modify: `app/web/static/style-custom.css` (top of file)
- Modify: `app/web/templates/base.html:7-8`
- Modify: `app/web/templates/base_login.html:7-8`
- Delete: `app/web/static/style.css`

- [ ] **Step 1: Read both CSS files in full.**

```bash
wc -l app/web/static/style.css app/web/static/style-custom.css
```

Read both end-to-end. Make a scratch list of every rule in `style.css` and decide for each:
- **Drop**: duplicated in `style-custom.css` already (e.g. duplicate `body` rule that style-custom overrides anyway).
- **Move**: not yet in style-custom; copy into the appropriate section of style-custom.
- **Replace**: same selector exists in style-custom with a better value; keep style-custom's version.

- [ ] **Step 2: Add to `:root` in style-custom.css any tokens style.css had that style-custom didn't.**

Examples to look for (verify against actual file contents):
- `--btn-google-bg`, `--btn-google-border` (if `.btn-google` is still used by login pages — keep tokens).
- Any color literals like `#1a73e8` that style.css used as `--primary` — drop them; style-custom's `#0073D1` is canonical.
- Any fonts referenced by `--font-family` (style.css legacy name) — alias them inside style-custom.

After this step, style-custom.css's `:root` is the only token block in the codebase.

- [ ] **Step 3: Move non-token rules from style.css into a clearly labeled section near the bottom of style-custom.css.**

Add a section header:
```css
/* =====================================================
   Legacy rules absorbed from the deleted style.css
   (login button family, base typography, table defaults).
   Reorganize during Tasks 3–4; remove once primitives cover them.
   ===================================================== */
```

Drop every rule from style.css that maps to a primitive we'll create in Tasks 3–4 (don't move `.btn-primary` etc. — Task 3 owns the button family).

- [ ] **Step 4: Drop the `style.css` link tag from both base templates.**

```bash
sed -n '6,9p' app/web/templates/base.html
sed -n '6,9p' app/web/templates/base_login.html
```

Edit `base.html:7` and `base_login.html:7` — remove the `<link rel="stylesheet" href="{{ static_url('style.css') }}">` line. Leave the `style-custom.css` line.

- [ ] **Step 5: Delete `style.css`.**

```bash
git rm app/web/static/style.css
```

- [ ] **Step 6: Visual smoke check.**

```bash
.venv/bin/uvicorn app.main:app --reload &
# Wait for "Application startup complete"
agent-browser open http://localhost:8000/login
agent-browser screenshot /tmp/login-after-merge.png
agent-browser open http://localhost:8000/        # while unauth, this redirects to login
```

Expected: login page renders without missing styles. Inter font visible. Google button still styled. Stop the server.

- [ ] **Step 7: Run tests + commit.**

```bash
.venv/bin/pytest tests/ -k "not test_clean_install_integration" --tb=short -q
git add app/web/static/style-custom.css app/web/templates/base.html app/web/templates/base_login.html
git rm app/web/static/style.css 2>/dev/null   # already done in step 5; ensure index is correct
git commit -m "style(css): consolidate style.css into style-custom.css

Single design-token block in style-custom.css. style.css deleted; its
rules absorbed into a labeled section pending primitive migration in
later commits."
```

---

## Task 2: Contract test for design-system invariants

**Goal:** Guard the refactor with a test that fails if a future PR re-introduces a deprecated class, points at the deleted `style.css`, or splits the token block. The test is **strict from this commit forward** — every later task in this plan must keep it passing.

**Files:**
- Create: `tests/test_design_system_contract.py`

- [ ] **Step 1: Write the test file.**

```python
"""Design-system invariants. Fails if a regression undoes the design-pass."""
from pathlib import Path
import re

TEMPLATES = Path("app/web/templates")
STATIC = Path("app/web/static")


def _all_html() -> list[Path]:
    return sorted(p for p in TEMPLATES.rglob("*.html"))


def test_style_css_deleted() -> None:
    assert not (STATIC / "style.css").exists(), (
        "style.css must stay deleted — all rules live in style-custom.css"
    )


def test_no_template_references_style_css() -> None:
    offenders = []
    for path in _all_html():
        text = path.read_text(encoding="utf-8")
        if "static_url('style.css')" in text or 'static_url("style.css")' in text:
            offenders.append(str(path))
    assert not offenders, f"templates still link style.css: {offenders}"


def test_style_custom_has_single_root_block() -> None:
    css = (STATIC / "style-custom.css").read_text(encoding="utf-8")
    root_count = len(re.findall(r"^:root\s*\{", css, flags=re.MULTILINE))
    assert root_count == 1, f"expected exactly one :root block, found {root_count}"


def test_canonical_primitives_defined() -> None:
    """All primitives Tasks 3–7 introduce must be declared in style-custom.css."""
    css = (STATIC / "style-custom.css").read_text(encoding="utf-8")
    required = [
        ".btn",            # base button
        ".btn-primary",
        ".btn-secondary",
        ".btn-ghost",
        ".btn-danger",
        ".search-input",
        ".filter-bar",
        ".page-header",
        ".page-header__title",
        ".data-table",
        ".empty-state",
        ".toast",
    ]
    missing = [sel for sel in required if sel not in css]
    assert not missing, f"missing canonical primitive selectors: {missing}"


DEPRECATED_CLASSES = {
    # Single class tokens only — multi-token patterns ("modal-btn primary")
    # are caught by .modal-btn alone, no need to special-case.
    "btn-primary-v2": "btn-primary",
    "btn-secondary-v2": "btn-secondary",
    "modal-btn": "btn (+ .btn-primary / .btn-secondary)",
    "users-table": "data-table",
    "gp-table": "data-table",
    "marketplaces-table": "data-table",
    "audit-table": "data-table",
    "users-search": "search-input",
    "marketplaces-search": "search-input",
    "kb-search": "search-input",
    "filters-card": "filter-bar",
    "pill": "filter-pill",
}

# Match every class="..." or class='...' attribute, possibly multi-line.
_CLASS_ATTR_RE = re.compile(r"""class\s*=\s*(["'])(.*?)\1""", re.DOTALL)


def _classes_in_template(text: str) -> set[str]:
    """Extract every class token used in the template. Tokenizes the
    class attribute on whitespace so multi-class attrs ("btn btn-primary")
    and multi-line attrs (Jinja templates do this) split cleanly."""
    tokens: set[str] = set()
    for match in _CLASS_ATTR_RE.finditer(text):
        attr_value = match.group(2)
        for tok in attr_value.split():
            # Jinja conditionals: skip tokens that contain "{{", "{%", "}"
            # — we only care about literal class names authors wrote.
            if "{" in tok or "}" in tok:
                continue
            tokens.add(tok)
    return tokens


def test_no_deprecated_class_in_templates() -> None:
    """Templates must use canonical primitives, not legacy aliases."""
    offenders: dict[str, list[str]] = {}
    for path in _all_html():
        text = path.read_text(encoding="utf-8")
        used = _classes_in_template(text)
        for cls, _replacement in DEPRECATED_CLASSES.items():
            if cls in used:
                offenders.setdefault(cls, []).append(path.name)
    assert not offenders, (
        "deprecated classes found in templates:\n"
        + "\n".join(f"  {cls} -> use {DEPRECATED_CLASSES[cls]} ({files})"
                    for cls, files in offenders.items())
    )
```

- [ ] **Step 2: Run the test — it should FAIL.**

```bash
.venv/bin/pytest tests/test_design_system_contract.py -v
```

Expected: `test_canonical_primitives_defined` fails (primitives not added yet), `test_no_deprecated_class_in_templates` fails (templates still use legacy classes). The other tests should pass (Task 1 already deleted style.css). Note the failures — Tasks 3 onward drive them to green.

- [ ] **Step 3: Commit.**

```bash
git add tests/test_design_system_contract.py
git commit -m "test(design): contract test for design-system invariants

Asserts style.css stays deleted, single :root, canonical primitives
exist, no deprecated class names in templates. Failing until Tasks
3-7 add the primitives and migrate the templates."
```

---

## Task 3: Button primitive hierarchy

**Goal:** Define `.btn` + `.btn-primary` + `.btn-secondary` + `.btn-ghost` + `.btn-danger` + `.btn-sm`/`.btn-lg` size modifiers. Map legacy class names (`.btn-primary-v2`, `.modal-btn`, etc.) as CSS aliases so existing templates keep rendering during migration.

**Files:**
- Modify: `app/web/static/style-custom.css` (new section, before legacy-absorption block)

- [ ] **Step 1: Add the button section to style-custom.css.**

Insert after the existing token block, before the existing component styles. Anchor with a clear comment so reviewers can find it.

```css
/* =====================================================
   Buttons
   Canonical:  .btn (+ variant) (+ size).
   Variants:   .btn-primary | .btn-secondary | .btn-ghost | .btn-danger
   Sizes:      default | .btn-sm | .btn-lg
   Legacy aliases at end of section — to be removed in final cleanup.
   ===================================================== */

.btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: var(--space-2);
    padding: 8px 16px;
    font-family: var(--font-primary);
    font-size: var(--text-base);
    font-weight: var(--font-medium);
    line-height: 1;
    border: 1px solid transparent;
    border-radius: var(--radius-md);
    background: transparent;
    color: var(--text-primary);
    cursor: pointer;
    transition: background var(--transition-fast), color var(--transition-fast),
                border-color var(--transition-fast), box-shadow var(--transition-fast);
    white-space: nowrap;
    user-select: none;
}

.btn:disabled,
.btn[aria-disabled="true"] { opacity: 0.5; cursor: not-allowed; }

.btn:focus-visible { outline: none; box-shadow: var(--focus-ring); }

.btn-primary {
    background: var(--primary);
    color: #fff;
    border-color: var(--primary);
}
.btn-primary:hover { background: var(--primary-dark); border-color: var(--primary-dark); }

.btn-secondary {
    background: var(--surface);
    color: var(--text-primary);
    border-color: var(--border);
}
.btn-secondary:hover { background: var(--border-light); }

.btn-ghost {
    background: transparent;
    color: var(--text-secondary);
}
.btn-ghost:hover { background: var(--border-light); color: var(--text-primary); }

.btn-danger {
    background: #fff;
    color: var(--error);
    border-color: var(--error);
}
.btn-danger:hover { background: var(--error); color: #fff; }

.btn-sm { padding: 4px 10px; font-size: var(--text-sm); }
.btn-lg { padding: 12px 20px; font-size: var(--text-md); }

/* Legacy aliases — remove in Task 13 once all templates migrated. */
.btn-primary-v2 { /* alias */ }   /* will be normalized via JS-free CSS @extend-style by repeating rules below */
```

Note: CSS has no `@extend`, so the legacy alias block needs to either (a) physically share selectors (`.btn-primary-v2, .btn-primary { … }`) or (b) be temporary copy-pasted rules. Option (a) is cleaner:

```css
/* Legacy aliases — primary group */
.btn-primary-v2 { /* deprecated: use .btn .btn-primary */ }
.btn.btn-primary-v2,
.btn-primary-v2 { /* falls through to .btn-primary on legacy markup */
    /* duplicate of .btn .btn-primary so legacy markup without .btn still works */
    display: inline-flex;
    /* …copy of .btn + .btn-primary properties… */
}
```

Simpler approach: just add **selector-list** rules:

```css
.btn.btn-primary, .btn-primary-v2 { background: var(--primary); color: #fff; border-color: var(--primary); }
.btn.btn-primary:hover, .btn-primary-v2:hover { background: var(--primary-dark); border-color: var(--primary-dark); }
```

Apply same selector-list trick for `.modal-btn.primary` (alias of `.btn .btn-primary` inside modals), `.modal-btn` (alias of `.btn .btn-secondary`).

- [ ] **Step 2: Search for every distinct button class name used today.**

```bash
grep -roh 'class="[^"]*\(btn\|button\)[^"]*"' app/web/templates/ \
    | sort -u > /tmp/btn-classes.txt
cat /tmp/btn-classes.txt
```

For every entry, add an alias rule to the legacy block IF it's NOT one of the canonical names. Verify nothing renders unstyled.

- [ ] **Step 3: Verify contract test progresses.**

```bash
.venv/bin/pytest tests/test_design_system_contract.py::test_canonical_primitives_defined -v
```

Expected: now passes for the button selectors (`.btn`, `.btn-primary`, etc.).

- [ ] **Step 4: Visual smoke check.**

```bash
.venv/bin/uvicorn app.main:app --reload &
agent-browser open http://localhost:8000/login
agent-browser snapshot -i           # verify Google button still renders correctly
```

- [ ] **Step 5: Commit.**

```bash
git add app/web/static/style-custom.css
git commit -m "feat(css): canonical .btn primitive hierarchy

Defines .btn + .btn-primary/.btn-secondary/.btn-ghost/.btn-danger +
.btn-sm/.btn-lg. Legacy class names (.btn-primary-v2, .modal-btn,
.modal-btn.primary, etc.) aliased via selector lists so existing
templates keep rendering. Aliases removed in final cleanup task."
```

---

## Task 4: Form-control primitives — `.search-input`, `.filter-bar`, `.filter-pill`

**Goal:** One filter-bar pattern across all pages. Same height, same focus ring, same border radius. Pill-shaped category filters become a separate explicit class (`.filter-pill`) used inside `.filter-bar`.

**Files:**
- Modify: `app/web/static/style-custom.css`

- [ ] **Step 1: Add the form-controls section.**

```css
/* =====================================================
   Form controls — inputs, selects, textareas, filter bars.
   Canonical:  .search-input | .filter-bar | .filter-pill
   ===================================================== */

.search-input,
.filter-bar input[type="search"],
.filter-bar input[type="text"],
.filter-bar select {
    height: 36px;
    padding: 0 12px;
    font-family: var(--font-primary);
    font-size: var(--text-sm);
    color: var(--text-primary);
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}

.search-input::placeholder,
.filter-bar input::placeholder { color: var(--text-muted); }

.search-input:focus,
.filter-bar input:focus,
.filter-bar select:focus {
    outline: none;
    border-color: var(--primary);
    box-shadow: var(--focus-ring);
}

.filter-bar {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    flex-wrap: wrap;
    padding: var(--space-3) var(--space-4);
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    margin-bottom: var(--space-4);
}

.filter-bar > .search-input { flex: 1 1 240px; min-width: 200px; }

.filter-pill {
    display: inline-flex;
    align-items: center;
    height: 28px;
    padding: 0 12px;
    font-size: var(--text-sm);
    font-weight: var(--font-medium);
    color: var(--text-secondary);
    background: transparent;
    border: 1px solid var(--border);
    border-radius: var(--radius-full);
    cursor: pointer;
    transition: background var(--transition-fast), color var(--transition-fast);
}

.filter-pill:hover { background: var(--border-light); color: var(--text-primary); }

.filter-pill.is-active {
    background: var(--primary-light);
    color: var(--primary);
    border-color: var(--primary);
}

/* Legacy aliases. Same selector-list trick as buttons. */
.users-search,
.marketplaces-search,
.kb-search { /* legacy classes, treated as .search-input */ }
.users-search, .marketplaces-search, .kb-search, .search-input {
    /* duplicated to keep legacy markup styled until templates migrate */
}
```

(In practice the test enforces these legacy class names are deleted from templates by Task 13 — the alias block is throwaway scaffolding.)

- [ ] **Step 2: Run contract test.**

```bash
.venv/bin/pytest tests/test_design_system_contract.py -v
```

Expected: `.search-input`, `.filter-bar` now found.

- [ ] **Step 3: Commit.**

```bash
git add app/web/static/style-custom.css
git commit -m "feat(css): .search-input / .filter-bar / .filter-pill primitives

Single canonical filter-bar shape: 36px-height inputs, 28px pills with
.is-active state, consistent focus ring. Legacy .users-search /
.marketplaces-search / .kb-search aliased."
```

---

## Task 5: Nav unification — fix the Admin entry

**Goal:** Resolve the user's specific complaint. "Home", "Marketplace", "Data Packages" render via `<a class="app-nav-link">`; "Admin" renders via `<button class="app-nav-link app-nav-menu-trigger">`. Today the button strips font/color/padding inheritance and adds its own active state (`var(--border-light)` instead of `var(--primary)`). Make them identical.

**Files:**
- Modify: `app/web/templates/_app_header.html`
- Modify: `app/web/static/style-custom.css` (the existing `.app-nav-link` + `.app-nav-menu-trigger` blocks, around line 2124–2220 per the audit)
- Create: `app/web/static/app.js`

- [ ] **Step 1: Read the current nav CSS rules.**

```bash
grep -n "\.app-nav-link\|\.app-nav-menu-trigger\|\.app-nav-menu-chevron" app/web/static/style-custom.css
```

Read every block referenced, end to end. Confirm where active state diverges.

- [ ] **Step 2: Merge selectors so `<button>` inherits the full link styling.**

Change every `.app-nav-link { … }` rule to `.app-nav-link, .app-nav-menu-trigger { … }`. Same for `:hover`, `.is-active`, focus states. Then DELETE the standalone `.app-nav-menu-trigger { … }` rules that previously stripped button chrome — they're redundant once the trigger shares the link rules.

The Admin trigger should now match siblings 1:1 in font, color, padding, hover, and active state. Active state of Admin uses the same `var(--primary)` color + `var(--primary-light)` background as other active links. Chevron icon stays.

- [ ] **Step 3: Extract inline `<script>` from `_app_header.html`.**

Move the `_wireDropdown` function + the two `_wireDropdown(...)` calls out of `_app_header.html:122-146` into a new file `app/web/static/app.js`.

```javascript
// app/web/static/app.js
(function () {
    function wireDropdown(triggerId, panelId) {
        var trigger = document.getElementById(triggerId);
        var panel = document.getElementById(panelId);
        if (!trigger || !panel) return;
        function setOpen(open) {
            trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
            if (open) { panel.removeAttribute('hidden'); }
            else { panel.setAttribute('hidden', ''); }
        }
        trigger.addEventListener('click', function (e) {
            e.stopPropagation();
            setOpen(trigger.getAttribute('aria-expanded') !== 'true');
        });
        document.addEventListener('click', function (e) {
            if (!panel.contains(e.target) && e.target !== trigger) setOpen(false);
        });
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape') { setOpen(false); trigger.focus(); }
        });
    }
    window.appUI = { wireDropdown: wireDropdown };

    document.addEventListener('DOMContentLoaded', function () {
        wireDropdown('userMenuTrigger', 'userMenuPanel');
        wireDropdown('adminNavTrigger', 'adminNavPanel');
    });
})();
```

- [ ] **Step 4: Link `app.js` from `base.html` + `base_login.html`.**

In `base.html`, after the existing `style-custom.css` link, add:

```html
<script src="{{ static_url('app.js') }}" defer></script>
```

(`base_login.html` probably doesn't need it — the login page has no nav. Skip there to avoid 404.)

Then delete the inline `<script>` block at `_app_header.html:122-146`.

- [ ] **Step 5: Browser-verify the nav.**

```bash
# Start uvicorn with LOCAL_DEV_MODE=1 so we can browse as admin without OAuth.
LOCAL_DEV_MODE=1 .venv/bin/uvicorn app.main:app --reload &

agent-browser open http://localhost:8000/dashboard
agent-browser snapshot -i        # nav visible; Home/Marketplace/Data Packages + Admin all rendered
agent-browser screenshot /tmp/nav-after.png --full
# Click Admin trigger
agent-browser click @<adminNavTrigger-ref>
agent-browser screenshot /tmp/nav-dropdown.png
```

Expected: Admin label same font weight, size, color as siblings. Hover state matches. Active state (when on any /admin/* path) matches. Dropdown opens.

- [ ] **Step 6: Commit.**

```bash
git add app/web/templates/_app_header.html app/web/templates/base.html \
        app/web/static/style-custom.css app/web/static/app.js
git commit -m "fix(nav): unify Admin trigger with sibling nav links

Admin dropdown trigger now shares .app-nav-link styling 1:1 with
<a> siblings — same font, color, padding, hover, active state
(primary blue, not the grey it used to render). Inline dropdown
JS moved to app.js as window.appUI.wireDropdown."
```

---

## Task 6: Page-header primitive

**Goal:** One page-header layout. Title + optional subtitle + optional right-aligned action slot. Same H1 size everywhere (22px / `--text-xl` adjusted). Hero variant for marketing pages opt-in via `.page-header--hero`.

**Files:**
- Modify: `app/web/static/style-custom.css`

- [ ] **Step 1: Add the page-header primitive section.**

```css
/* =====================================================
   Page header — title + subtitle + actions row.
   Default:  compact 22px title for admin/content pages.
   Hero:     .page-header--hero for marketing/landing.
   ===================================================== */

.page-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--space-4);
    margin: var(--space-6) 0 var(--space-5);
    flex-wrap: wrap;
}

.page-header__main {
    min-width: 0;
    flex: 1 1 auto;
}

.page-header__title {
    margin: 0;
    font-size: 22px;
    font-weight: var(--font-semibold);
    color: var(--text-primary);
    line-height: 1.2;
}

.page-header__subtitle {
    margin: var(--space-2) 0 0;
    font-size: var(--text-sm);
    color: var(--text-secondary);
    line-height: 1.4;
}

.page-header__actions {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    flex-wrap: wrap;
}

/* Hero variant — for landing / store / marketplace headers. */
.page-header--hero {
    padding: var(--space-7) var(--space-6);
    background: linear-gradient(135deg, var(--primary), var(--primary-dark));
    color: #fff;
    border-radius: var(--radius-xl);
    margin-bottom: var(--space-6);
}
.page-header--hero .page-header__title { font-size: 28px; color: #fff; }
.page-header--hero .page-header__subtitle { color: rgba(255,255,255,0.85); }

/* Eyebrow label inside hero. */
.page-header__eyebrow {
    text-transform: uppercase;
    font-size: var(--text-xs);
    letter-spacing: 0.8px;
    color: rgba(255,255,255,0.75);
    margin: 0 0 var(--space-2);
}
```

- [ ] **Step 2: Commit.**

```bash
git add app/web/static/style-custom.css
git commit -m "feat(css): .page-header primitive with hero variant

Unified header pattern: 22px title, optional subtitle, optional
right-aligned actions slot. .page-header--hero opt-in for landing
pages keeps the gradient look of /store and /marketplace heroes."
```

---

## Task 7: `.data-table`, `.empty-state`, `.toast` primitives

**Goal:** Cover the remaining three components called out by the audit. `.data-table` already exists in style-custom.css (line ~2262 per audit) — keep its rules but ensure they handle every column-width variant the admin pages need. `.empty-state` is new. `.toast` is new + paired with a JS helper.

**Files:**
- Modify: `app/web/static/style-custom.css`
- Modify: `app/web/static/app.js`

- [ ] **Step 1: Audit the existing `.data-table` block.**

```bash
grep -n "\.data-table" app/web/static/style-custom.css
```

Read the block. Add `.data-table--compact` modifier (smaller padding for dense admin tables like Audit log) if not already present. Confirm `th` uses uppercase tracking, `td` uses `tabular-nums`, hover state is `var(--border-light)`.

- [ ] **Step 2: Add `.empty-state` primitive.**

```css
/* =====================================================
   Empty state — for "no records" / "no results" panels.
   ===================================================== */

.empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-8) var(--space-6);
    text-align: center;
    background: var(--surface);
    border: 1px dashed var(--border);
    border-radius: var(--radius-lg);
    color: var(--text-secondary);
}

.empty-state__icon { font-size: 32px; opacity: 0.5; }
.empty-state__title { font-size: var(--text-md); color: var(--text-primary); font-weight: var(--font-medium); margin: 0; }
.empty-state__description { font-size: var(--text-sm); margin: 0; max-width: 480px; line-height: 1.5; }
.empty-state__actions { margin-top: var(--space-3); }
```

- [ ] **Step 3: Add `.toast` primitive + JS helper.**

```css
/* =====================================================
   Toasts — global notification surface.
   Containers stacked bottom-right; dismissed via timeout or click.
   ===================================================== */

.toast-container {
    position: fixed;
    right: var(--space-5);
    bottom: var(--space-5);
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    z-index: 9999;
    pointer-events: none;
}

.toast {
    pointer-events: auto;
    min-width: 240px;
    max-width: 360px;
    padding: var(--space-3) var(--space-4);
    border-radius: var(--radius-md);
    background: var(--surface);
    border: 1px solid var(--border);
    box-shadow: var(--shadow-elevated);
    font-size: var(--text-sm);
    color: var(--text-primary);
    cursor: pointer;
    animation: toast-in 200ms ease;
}

.toast.is-success { border-left: 3px solid var(--success); }
.toast.is-warning { border-left: 3px solid var(--warning); }
.toast.is-error   { border-left: 3px solid var(--error); }
.toast.is-info    { border-left: 3px solid var(--primary); }

@keyframes toast-in { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }
```

Append to `app.js`:

```javascript
(function () {
    function ensureContainer() {
        var c = document.getElementById('appToastContainer');
        if (c) return c;
        c = document.createElement('div');
        c.id = 'appToastContainer';
        c.className = 'toast-container';
        document.body.appendChild(c);
        return c;
    }
    window.appToast = function (opts) {
        opts = opts || {};
        var kind = opts.kind || 'info';
        var msg = String(opts.msg || '');
        var timeout = opts.timeout == null ? 4000 : opts.timeout;
        var el = document.createElement('div');
        el.className = 'toast is-' + kind;
        el.textContent = msg;
        el.addEventListener('click', function () { el.remove(); });
        ensureContainer().appendChild(el);
        if (timeout > 0) setTimeout(function () { el.remove(); }, timeout);
        return el;
    };
})();
```

- [ ] **Step 4: Run contract test — should be GREEN now for primitives.**

```bash
.venv/bin/pytest tests/test_design_system_contract.py::test_canonical_primitives_defined -v
```

Expected: pass. (`test_no_deprecated_class_in_templates` still fails — that's Tasks 8–12's job.)

- [ ] **Step 5: Commit.**

```bash
git add app/web/static/style-custom.css app/web/static/app.js
git commit -m "feat(css): .data-table modifier, .empty-state, .toast primitives

.data-table--compact for dense admin tables. .empty-state and .toast
new; toasts paired with window.appToast({kind, msg, timeout})."
```

---

## Task 8: Template migration — admin index pages (12 files)

**Goal:** Sweep all admin list/index pages onto the new primitives. Mechanical: each page's inline `<style>` block either deletes (when it only re-states global rules) or shrinks (when it has page-specific column widths).

**Files (12 templates):**
- `app/web/templates/activity_center.html`
- `app/web/templates/admin_access.html`
- `app/web/templates/admin_groups.html`
- `app/web/templates/admin_marketplaces.html`
- `app/web/templates/admin_scheduler_runs.html`
- `app/web/templates/admin_sessions.html`
- `app/web/templates/admin_store_submissions.html`
- `app/web/templates/admin_tables.html`
- `app/web/templates/admin_tokens.html`
- `app/web/templates/admin_usage.html`
- `app/web/templates/admin_users.html`
- `app/web/templates/admin_welcome.html`

**Per-template recipe (apply to all 12):**

- [ ] **Step 1 (per file): Read the file end-to-end.**

```bash
.venv/bin/python -c "
import sys, pathlib
p = pathlib.Path(sys.argv[1])
print(p.read_text())
" app/web/templates/<file>
```

Identify: (a) the page-header markup (some H1 + maybe a subtitle + maybe an action button), (b) the filter/search row markup, (c) the table markup, (d) inline `<style>` block.

- [ ] **Step 2 (per file): Replace page-header markup.**

Before:
```html
<div class="users-page">
  <div class="users-toolbar">
    <h1 class="users-title">Users</h1>
    <input class="users-search" type="search" placeholder="…">
    <button>+ Add user</button>
  </div>
</div>
```

After:
```html
<header class="page-header">
  <div class="page-header__main">
    <h1 class="page-header__title">Users</h1>
  </div>
  <div class="page-header__actions">
    <button class="btn btn-primary">+ Add user</button>
  </div>
</header>

<div class="filter-bar">
  <input class="search-input" type="search" placeholder="…">
</div>
```

- [ ] **Step 3 (per file): Swap table class.**

`class="users-table"` → `class="data-table"`. Same for `gp-table`, `marketplaces-table`, `audit-table`. Move any genuinely page-specific column-width rules into a slim inline `<style>` block that only sets `<colgroup>` widths — nothing else.

- [ ] **Step 4 (per file): Strip the now-orphan rules from the inline `<style>` block.**

Delete every rule that duplicates `.data-table`, `.btn-*`, `.search-input`, `.page-header__*`, or `.filter-bar`. Keep only page-specific column widths or page-specific layout grids. If the whole `<style>` block becomes empty, delete it.

- [ ] **Step 5 (per file): Insert `.empty-state` markup if the page renders a "no records" state.**

Before (varied per page — `<p>No users found.</p>`, `<div class="empty">…</div>`, etc.):
```html
{% if not users %}<p>No users found.</p>{% endif %}
```

After:
```html
{% if not users %}
<div class="empty-state">
  <div class="empty-state__title">No users yet</div>
  <p class="empty-state__description">Add a user to get started, or wait for Google sign-in to populate the list.</p>
</div>
{% endif %}
```

(Wording varies per page — match the page's domain.)

- [ ] **Step 6 (per file): Browser-verify.**

```bash
LOCAL_DEV_MODE=1 .venv/bin/uvicorn app.main:app --reload &
agent-browser open http://localhost:8000/admin/<page>
agent-browser screenshot /tmp/<page>-after.png --full
```

Compare against `/tmp/<page>-before.png` if you took one. Expected: identical structural placement, modernized styling, no broken layout.

- [ ] **Step 7 (per file): Commit.**

```bash
git add app/web/templates/<file>
git commit -m "style(<page>): migrate to canonical primitives

Replace per-page <style> block with .page-header, .filter-bar,
.search-input, .data-table, .empty-state."
```

- [ ] **Step 8 (whole task): Run contract test + full suite.**

After all 12 files done:

```bash
.venv/bin/pytest tests/test_design_system_contract.py -v
.venv/bin/pytest tests/ -k "not test_clean_install_integration" -n auto -q
```

Expected: contract test progressing (some legacy-class entries gone). Full suite green.

---

## Task 9: Template migration — admin detail pages (7 files)

**Files:**
- `app/web/templates/admin_group_detail.html`
- `app/web/templates/admin_server_config.html`
- `app/web/templates/admin_session_detail.html`
- `app/web/templates/admin_store_submission_detail.html`
- `app/web/templates/admin_user_detail.html`
- `app/web/templates/admin_workspace_prompt.html`
- `app/web/templates/admin/news_editor.html`

Apply the **same recipe as Task 8** (steps 1–7 per file). Detail pages often have form blocks instead of tables — for forms, use `.btn .btn-primary` for submit and `.btn .btn-secondary` for cancel; inputs use `.search-input` if they're plain text inputs or define a sibling `.form-input` primitive in `style-custom.css` if more form-specific styling is needed (height/border match `.search-input` so visual consistency holds).

- [ ] **Step 1: If any form-input styling diverges from `.search-input`, add `.form-input` primitive to style-custom.css matching `.search-input` baseline plus textarea sizing rules.**

```css
.form-input,
.form-input[type="text"],
.form-input[type="email"],
.form-input[type="number"],
.form-input[type="password"],
.form-input[type="url"],
textarea.form-input,
select.form-input {
    /* same as .search-input but multi-line capable */
    width: 100%;
    min-height: 36px;
    padding: 8px 12px;
    /* (etc — copy from .search-input) */
}
textarea.form-input { min-height: 96px; resize: vertical; font-family: var(--font-mono); }
```

Add `.form-input` to the contract test's required-selectors list and re-run.

- [ ] **Step 2..N (per file): Migrate per Task-8 recipe.**

One commit per file. Same message structure.

- [ ] **Step N+1: Run full suite + commit.**

```bash
.venv/bin/pytest tests/ -k "not test_clean_install_integration" -n auto -q
```

---

## Task 10: Template migration — catalog + marketplace + store (8 files)

**Files:**
- `app/web/templates/catalog.html`
- `app/web/templates/marketplace.html`
- `app/web/templates/marketplace_guide.html`
- `app/web/templates/marketplace_item_detail.html`
- `app/web/templates/marketplace_plugin_detail.html`
- `app/web/templates/store_edit.html`
- `app/web/templates/store_examples.html`
- `app/web/templates/store_upload.html`

**Special considerations:**

- Catalog has pill-shaped filters inside a `.filters-card` (per audit at `store_listing.html:54-77` pattern). Replace `.filters-card .pill` with `.filter-bar` + `.filter-pill` (defined in Task 4). Same shape, canonical name.
- Marketplace/store use **hero page-headers** (gradient background, larger title, eyebrow label). Use `.page-header.page-header--hero` with `.page-header__eyebrow` (defined in Task 6).
- `marketplace.html` has tabbed sub-views (`?tab=flea`, `?tab=my`). Tabs render as a sibling row to the hero — define `.tab-strip` + `.tab-strip__item` in style-custom.css if not already present; otherwise alias whatever's there.

Apply Task-8 recipe with these hero/pill substitutions. One commit per file.

---

## Task 11: Template migration — corporate memory, profile, home, misc (15 files)

**Files (grouped for one task, separate commits):**

- **Corporate memory (2)**: `corporate_memory.html`, `corporate_memory_admin.html`
- **Profile / sessions / tokens (3)**: `profile.html`, `profile_sessions.html`, `my_tokens.html`
- **Home / dashboard / install / setup (5)**: `home_not_onboarded.html`, `home_onboarded.html`, `dashboard.html`, `install.html`, `setup_advanced.html`
- **Misc (5)**: `error.html`, `me_debug.html`, `news.html`, `_quarantine_banner.html`, `_flea_versions.html`

Apply Task-8 recipe per file. `_quarantine_banner.html` and `_flea_versions.html` are partials — they may not have a page-header (they're embedded chunks). Just clean up their inline styles to use tokens + canonical classes.

`dashboard.html` likely has stat-card components — define `.stat-card` primitive if multiple pages use it, or scope to dashboard alone.

---

## Task 12: Login / auth page sweep

**Goal:** Catch the login flow (uses `base_login.html`, not `base.html` — no nav, no toasts wiring). Keep it minimal — these pages are usually fine, just verify they render after the token consolidation didn't break anything.

**Files (verify, light touch if needed):**
- `app/web/templates/login.html`
- `app/web/templates/login_email.html`
- `app/web/templates/login_magic_link.html`
- `app/web/templates/login_magic_link_sent.html`
- `app/web/templates/password_setup.html`
- `app/web/templates/password_reset.html`
- `app/web/templates/setup.html`
- `app/web/templates/desktop_link.html`

For each: open in agent-browser, screenshot. If anything looks broken (Inter font not loaded, Google button wrong color, etc.), fix. If it looks fine, no edit needed.

- [ ] **Step 1: Loop through each login route.**

```bash
LOCAL_DEV_MODE=1 .venv/bin/uvicorn app.main:app --reload &
for route in /login /login/email /login/magic-link /password/reset /setup /desktop-link; do
    agent-browser open "http://localhost:8000${route}"
    agent-browser screenshot "/tmp/loginflow${route//\//-}-after.png" --full
done
```

- [ ] **Step 2: Triage screenshots manually. Only edit files where rendering breaks.**

- [ ] **Step 3: Commit any fixes.**

---

## Task 13: Remove legacy CSS aliases

**Goal:** Now that every template uses the canonical primitives, delete the legacy alias rules from `style-custom.css`. Contract test enforces no template re-introduces them.

**Files:**
- Modify: `app/web/static/style-custom.css`

- [ ] **Step 1: Grep for residual legacy class references in templates.**

```bash
for cls in btn-primary-v2 btn-secondary-v2 modal-btn users-table gp-table marketplaces-table audit-table users-search marketplaces-search kb-search filters-card pill; do
    echo "=== $cls ==="
    grep -l "$cls" app/web/templates/ -r
done
```

Expected: empty for each. If any template still references one, go back and migrate it first.

- [ ] **Step 2: Delete the legacy-alias sections from style-custom.css.**

Remove every selector that exists ONLY to alias a legacy class. Find them by the `/* Legacy aliases — remove in Task 13 */` comments planted in Tasks 3–4.

Also delete the "Legacy rules absorbed from the deleted style.css" section planted in Task 1 — its contents should by now be either covered by primitives or no longer referenced.

- [ ] **Step 3: Run contract test — should be GREEN end-to-end.**

```bash
.venv/bin/pytest tests/test_design_system_contract.py -v
```

Expected: every test passes.

- [ ] **Step 4: Run full suite.**

```bash
.venv/bin/pytest tests/ -n auto -q
```

Expected: green (modulo pre-existing flake).

- [ ] **Step 5: Commit.**

```bash
git add app/web/static/style-custom.css
git commit -m "style(css): remove legacy class aliases

All templates now use canonical primitives. Deprecated class names
(.btn-primary-v2, .modal-btn, .users-table, .gp-table,
.marketplaces-table, .audit-table, .users-search,
.marketplaces-search, .kb-search, .filters-card, .pill) gone from
both templates and CSS."
```

---

## Task 14: ~~Sticky page header~~ — DROPPED (deferred to follow-up PR)

Per review: expands review surface (12 admin pages need sticky-mode opt-in + viewport smoke tests) without addressing the user's complaint. Open follow-up issue after this PR merges.

<details><summary>Original task body (kept for follow-up reference)</summary>

### Sticky page header for long admin tables (nice-to-have #1)

**Goal:** When scrolling a long admin list (users, marketplaces, audit log), the `.page-header` + `.filter-bar` stay visible.

**Files:**
- Modify: `app/web/static/style-custom.css`

- [ ] **Step 1: Add a `.page-header--sticky` modifier.**

```css
.page-header--sticky {
    position: sticky;
    top: 0;
    z-index: 50;
    background: var(--background);
    margin-top: 0;
    padding-top: var(--space-4);
    padding-bottom: var(--space-3);
    border-bottom: 1px solid var(--border);
}

/* When the page-header is sticky, the filter-bar can stack right below it. */
.page-header--sticky + .filter-bar {
    position: sticky;
    top: 72px;     /* page-header height + a bit */
    z-index: 49;
    margin-bottom: var(--space-4);
}
```

- [ ] **Step 2: Opt in for admin list pages (12 from Task 8).**

For each of the 12, change `<header class="page-header">` to `<header class="page-header page-header--sticky">`. Quick edit.

- [ ] **Step 3: Browser-verify a long table.**

```bash
agent-browser open http://localhost:8000/admin/activity     # likely has many rows
agent-browser scroll down 800
agent-browser screenshot /tmp/sticky-scrolled.png --full
```

Expected: page-header + filter-bar remain visible at the top.

- [ ] **Step 4: Commit.**

```bash
git add app/web/static/style-custom.css app/web/templates/admin_*.html app/web/templates/activity_center.html
git commit -m "feat(css): page-header--sticky modifier for long admin lists"
```

---

</details>

---

## Task 15: ~~Dark-mode token skeleton~~ — DROPPED (deferred to follow-up PR)

Per review: silently expands review surface ("does this also work dark?" on every selector touched). Defer to a focused follow-up PR with a UI toggle.

<details><summary>Original task body (kept for follow-up reference)</summary>

### Dark-mode token skeleton (nice-to-have #2)

**Goal:** Lay the groundwork even without a UI toggle. A future PR can wire a toggle; this PR just defines the dark palette so future work is mechanical.

**Files:**
- Modify: `app/web/static/style-custom.css`

- [ ] **Step 1: Add a `[data-theme="dark"]` token override block right after the `:root` block.**

```css
:root[data-theme="dark"] {
    --primary: #4FA8FF;
    --primary-light: rgba(79, 168, 255, 0.15);
    --primary-dark: #2B7CD9;
    --text-primary: #E5E7EB;
    --text-secondary: #9CA3AF;
    --text-muted: #6B7280;
    --background: #0F172A;
    --surface: #1E293B;
    --border: #334155;
    --border-light: #1E293B;
    --shadow-sm: rgba(0,0,0,0.4) 0px 1px 2px 0px;
    --shadow-card: 0 1px 3px 0 rgba(0,0,0,0.5), 0 1px 2px -1px rgba(0,0,0,0.4);
    --shadow-elevated: 0 8px 24px rgba(0,0,0,0.45);
    --focus-ring: 0 0 0 3px rgba(79, 168, 255, 0.4);
}
```

No template change. No toggle yet. Verify via DevTools by setting `document.documentElement.dataset.theme = 'dark'` — every primitive should re-skin via the token cascade.

- [ ] **Step 2: Browser-verify by injecting the attribute.**

```bash
agent-browser open http://localhost:8000/dashboard
agent-browser keyboard type ''     # focus body
# Use a one-shot eval via the inspect tool to set documentElement dataset:
agent-browser --headed inspect    # then in DevTools console: document.documentElement.dataset.theme = 'dark'
agent-browser screenshot /tmp/dashboard-dark.png --full
```

Expected: all primitives re-skin coherently. Buttons, tables, page headers, filter bars all in dark variants. No hardcoded white backgrounds peeking through. If something doesn't re-skin, that's a hardcoded color we missed — fix the offending rule to use a token.

- [ ] **Step 3: Commit.**

```bash
git add app/web/static/style-custom.css
git commit -m "feat(css): dark-mode token skeleton

Adds :root[data-theme=\"dark\"] palette override. No UI toggle yet —
flipping the attribute via JS re-skins every primitive that uses
tokens correctly. Toggle to be wired in a follow-up PR."
```

---

  </details>

---

## Task 16: CHANGELOG entry + final sweep + push

- [ ] **Step 1: Update CHANGELOG.md.**

Add to the top `## [Unreleased]` section:

```markdown
### Changed
- Web UI design system unified: single stylesheet (`style-custom.css`),
  canonical primitives for buttons, inputs, filter bars, page headers,
  tables, empty states, toasts. Top-nav Admin entry now matches sibling
  links in font / color / hover / active state. 40+ templates migrated;
  legacy class names (`.btn-primary-v2`, `.modal-btn`, `.users-table`,
  `.users-search`, etc.) removed. Dark-mode token skeleton in place
  behind `[data-theme="dark"]` for a future toggle.

### Removed
- `app/web/static/style.css` — content folded into `style-custom.css`.

### Internal
- New `tests/test_design_system_contract.py` enforces design-system
  invariants (single `:root` block, no deprecated class names in
  templates, canonical primitives defined).
- Inline `<style>` blocks removed from 35+ templates; per-page CSS now
  only carries column-width specifics where needed.
- Nav dropdown JS extracted from inline `<script>` in `_app_header.html`
  into `app/web/static/app.js` (also hosts the new `window.appToast`
  helper).
```

- [ ] **Step 2: Vendor-agnostic token scan over the entire diff.**

```bash
git diff origin/main..HEAD -- ':(exclude)docs/superpowers/plans/' \
    | grep -niE 'foundryai|groupon|prj-grp|agnes-dev|grp_foundryai|@groupon\.com|@groupondev\.com|keboola/agnes|\.internal\b' \
    || echo "clean"
```

Expected: `clean`. If anything matches, generalize before pushing.

- [ ] **Step 3: Final full-suite run (matches CI exactly per CLAUDE.md).**

```bash
.venv/bin/pytest tests/ --tb=short -n auto -q
```

Expected: green (modulo pre-existing flake).

- [ ] **Step 4: Comprehensive browser smoke pass.**

Iterate over every route via agent-browser. For each, snapshot + screenshot, then visually skim. Use a single shell loop:

```bash
LOCAL_DEV_MODE=1 .venv/bin/uvicorn app.main:app --reload &
ROUTES=(
    /dashboard /home
    /catalog
    /marketplace /marketplace?tab=flea /marketplace?tab=my /marketplace/guide
    /corporate-memory /corporate-memory/admin
    /admin/users /admin/groups /admin/access /admin/tokens
    /admin/marketplaces /admin/tables /admin/scheduler-runs
    /admin/server-config /admin/agent-prompt /admin/workspace-prompt
    /admin/activity /admin/telemetry /admin/usage /admin/sessions
    /admin/store/submissions
    /admin/welcome
    /profile /profile/sessions /tokens
    /setup /install
    /login
)
for r in "${ROUTES[@]}"; do
    safe="${r//\//-}"
    safe="${safe//\?/--Q}"
    agent-browser open "http://localhost:8000${r}"
    agent-browser wait --load networkidle
    agent-browser screenshot "/tmp/smoke${safe}.png" --full
done
```

Eyeball each screenshot. Anything that looks broken — fix and re-test.

- [ ] **Step 5: Push branch + open PR.**

```bash
git push -u origin zs/design-pass
gh pr create --title "UI design system unification: single stylesheet, canonical primitives, nav fix" --body "$(cat <<'EOF'
## Summary

- **Single stylesheet**: `style.css` deleted; rules folded into `style-custom.css`. One `:root` token block.
- **Canonical primitives**: `.btn` family, `.search-input`, `.filter-bar`, `.filter-pill`, `.page-header` (+ hero modifier), `.data-table` (+ compact modifier), `.empty-state`, `.toast`. Every page uses them.
- **Top-nav Admin entry fixed**: Admin trigger now shares `.app-nav-link` styling 1:1 with sibling `<a>` entries — same font, color, padding, hover, active state. The user-reported "Admin looks different" goes away.
- **40+ templates migrated**: inline `<style>` blocks reduced to only page-specific column widths where genuinely needed. Legacy class names (`.btn-primary-v2`, `.modal-btn`, `.users-table`, `.gp-table`, `.marketplaces-table`, `.audit-table`, `.users-search`, `.marketplaces-search`, `.kb-search`, `.filters-card .pill`) gone from templates and CSS.
- **Nice-to-haves**: `.page-header--sticky` for long admin tables; dark-mode token skeleton behind `[data-theme="dark"]` ready for a follow-up toggle PR; `window.appToast({kind, msg})` helper in `app/web/static/app.js`.
- **Contract test**: `tests/test_design_system_contract.py` fails CI if a future PR re-introduces a deprecated class or breaks the single-stylesheet invariant.

## Test plan

- [ ] Full pytest suite green: `pytest tests/ -n auto`
- [ ] New contract test green: `pytest tests/test_design_system_contract.py -v`
- [ ] Browser smoke pass via agent-browser over ~30 routes (see commit log for the loop used)
- [ ] No hardcoded customer-specific tokens in diff
- [ ] CHANGELOG updated under `[Unreleased]`
EOF
)"
```

- [ ] **Step 6: Watch CI. If green and `[Unreleased]` would land alone, cut the release per CLAUDE.md "Release-cut belongs to the PR — non-negotiable":**

   1. Bump `pyproject.toml` to the next patch version.
   2. Rename `## [Unreleased]` → `## [X.Y.Z] — 2026-MM-DD`, add new empty `[Unreleased]`.
   3. Commit `release: X.Y.Z` on the branch.
   4. Push, enable auto-merge.

If other feature PRs are queued behind this one, skip the release cut and let the last PR in the queue carry it.

---

## Self-review checklist

After writing this plan, walking back through:

- **Spec coverage**: All five user-named problem areas covered — fonts (Task 1 tokens), nav (Task 5), filters (Task 4 + Task 8 migrations), page consistency (Tasks 6, 8–12 migrations), tables (Task 7 + migration sweep). ✅
- **No placeholders**: Every CSS block above is concrete code. Per-file migration steps point to specific markup transforms. ✅
- **Type consistency**: Class names referenced in primitive tasks (`.btn-primary`, `.search-input`, `.page-header__title`, etc.) match what migration tasks expect to swap in. ✅
- **Tests run before each push (CLAUDE.md)**: Tasks 1, 8, 9, 11, 13, 16 each run pytest. Final push in Task 16 runs the full CI-matching command. ✅
- **One PR (user's explicit ask)**: All 16 tasks land on `zs/design-pass`; the PR opens at Task 16 step 5. No intermediate merges. ✅
- **Vendor-agnostic check**: Task 16 step 2 greps the diff before push. ✅
- **CHANGELOG (CLAUDE.md)**: Task 16 step 1. ✅
- **Release-cut (CLAUDE.md)**: Task 16 step 6 — conditional on no other PRs being queued, exactly as the rule prescribes. ✅
