# UI Overhaul: dead-code cleanup, API↔UI parity, design-system unification

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the FastAPI/Jinja2 web UI back to a state where every page is wired, every API endpoint with admin/operator value has a UI surface, the design system is one stylesheet, and the navigation is honest about what exists.

**Architecture:** Nine independently shippable PRs. Each PR is small enough to review in one sitting and reverts cleanly. Phase 0 collects decisions that block 2 of the 9 phases. Phases 1-3 are no-new-feature housekeeping (delete / fix / unify). Phases 4-7 add admin surfaces for already-existing API endpoints. Phases 8-9 finish the design-system migration.

**Tech Stack:** FastAPI, Jinja2 templates, vanilla CSS (no framework), pytest. No new runtime deps planned.

**Source:** Three parallel audit agents (visual/CSS, API↔UI coverage, UX/IA devil's-advocate) on 2026-04-30. Findings cross-referenced. Specific orphans, dead routes, and stub-data renders verified by re-reading `app/web/router.py` and grepping templates.

---

## Phase 0: Decisions needed before execution

Each item below either changes scope or determines whether a phase ships at all. Mark each `[BUILD]` or `[DELETE]` and note any preference. None of these are mine to decide.

- [ ] **D1 — Activity Center.** `app/web/templates/activity_center.html` (2 552 lines) renders entirely against undefined stub data passed by `app/web/router.py:530`. The `_SilentUndefined` filter at `app/web/router.py:39-50` silently turns missing fields into empty strings, so the page never errors but also never shows real data. Same pattern for the dashboard widget (`dashboard.html:2342-2412`).
   - `[DELETE both]` — drop the route, the template, and the dashboard widget. Phase 1 scope.
   - `[KEEP behind feature flag]` — guard with an `INSTANCE_FEATURES.activity_center` flag in `app/instance_config.py`, default off. Defer real-data implementation to a future plan.

- [ ] **D2 — Notification scripts UI.** `app/api/scripts.py` exposes 5 endpoints (`list / deploy / run-by-id / run-ad-hoc / delete`). Login marketing copy (`login.html:54`) and dashboard onboarding both promise "automated scripts". No UI exists. Server-side Python execution without an audit/admin surface is an operator hazard.
   - `[BUILD /admin/scripts]` — Phase 7 ships. Effort L (table + deploy form + run-with-output + per-script log tail).
   - `[DELETE the marketing claim + leave API CLI-only]` — pull "Instant Automation" / scripts language from login + dashboard. Phase 1 scope, Phase 7 dropped.

- [ ] **D3 — Catalog profiler split.** The profiler overlay in `app/web/templates/catalog.html` is ~1000 lines of CSS+HTML+JS embedded inline. Splitting it speeds catalog renders and isolates the data-quality view.
   - `[OWN PAGE at /catalog/{table}/profile]` — separate route, separate template. Heavier change, cleaner result.
   - `[JINJA PARTIAL]` — extract to `templates/_profiler_overlay.html` and include from `catalog.html`. Lighter change, same overlay UX.
   - `[LEAVE]` — defer; do CSS-only hoist in Phase 8 instead.

- [ ] **D4 — Email auth pages consolidation.** Five templates today: `login_email.html`, `login_magic_link.html`, `login_magic_link_sent.html`, `password_setup.html`, `password_reset.html`. Could become two (request + token-landing).
   - `[MERGE]` — Phase 2 scope add.
   - `[LEAVE]` — out of plan.

- [ ] **D5 — Style.css full retirement.** Phase 3 hoists table + font rules globally; Phase 9 finishes the migration by deleting `app/web/static/style.css` entirely. Some legacy login chrome (`.btn-google`, login form rules) lives only in style.css today.
   - `[KILL style.css in Phase 9]` — port the ~5 unique rules into `style-custom.css`, rename `-v2` suffixes off everywhere. Risk: visual regression on login / legacy admin pages.
   - `[LEAVE side-by-side]` — keep both files, drop only the duplicated `body { font-family }` rule. Lower risk, accepts permanent two-stylesheet split.

- [ ] **D6 — Plan execution mode.**
   - `[Subagent-driven]` — fresh subagent per phase, two-stage review between phases.
   - `[Inline]` — execute phases in this session, checkpoint after each phase.

---

## Phase 1: Dead-code cleanup + silent-bug fixes

**Goal:** Delete clearly dead pages, fix the 3 orphan API fetches, wire up live data where templates currently hardcode false. Net code change is negative.

**Branch:** `cleanup/ui-dead-code-and-orphans`

**Files:**
- Delete: `app/web/templates/admin_permissions.html` (1 223 lines)
- Modify: `app/web/router.py` (drop `/admin/permissions` route + drop or shrink `/activity-center` route per D1; add nav-link regression hook)
- Conditional delete (per D1): `app/web/templates/activity_center.html` (2 552 lines)
- Modify: `app/web/templates/dashboard.html` (remove SSH-key new-user block at `:2419-2509`; remove activity-center widget at `:2342-2412` per D1; fix `/api/sync-settings` → `/api/sync/settings` at `:2687`; fix `/api/${channel}/unlink` to telegram-only at `:2790`; replace hardcoded `telegram_status={"linked": False}` with live `/api/telegram/status` fetch — also remove the dummy at `app/web/router.py:342`)
- Modify: `app/web/templates/admin_tokens.html` (add `last_used_ip` column — column already returned by `GET /auth/admin/tokens`)
- Conditional modify (per D2): `app/web/templates/login.html` (drop "Instant Automation" feature card)
- Test: `tests/web/test_route_integrity.py` (new — regression test per agent rec #10)

**Verification approach:** API tests where endpoints touched; manual browser sweep at the end of the phase.

- [ ] **Step 1: Verify admin_permissions is truly orphaned**

```bash
grep -rn "admin/permissions\|admin_permissions" app/ tests/ docs/ cli/ services/ --include="*.py" --include="*.html" --include="*.md" | grep -v "app/web/templates/admin_permissions.html\|app/web/router.py"
```

Expected: zero references outside the template itself and its router registration. If anything else surfaces (e.g. a CLI command that opens the page, an import), STOP and reassess scope.

- [ ] **Step 2: Verify activity_center is truly stub-driven (per D1)**

```bash
grep -nE "activity_summary\.|activity\." app/web/router.py
grep -nE "activity_summary\b|activity\b" app/web/templates/activity_center.html app/web/templates/dashboard.html | head -30
```

Confirm: every field the templates read is absent from the dict the route passes, and `_SilentUndefined` filter is responsible for the silent render. Document the matched undefined fields in the commit message.

- [ ] **Step 3: Write the route-integrity regression test first**

Create `tests/web/test_route_integrity.py`:

```python
"""
Regression test: every link in _app_header.html must point to a registered route,
and every registered HTML route must be reachable from the nav (or be on a
documented allowlist of detail/wizard pages reachable from elsewhere).
"""
from pathlib import Path
import re
from fastapi.routing import APIRoute
from app.main import app


# Routes intentionally not in the top nav (reached from other pages or wizards)
ALLOWLIST = {
    "/", "/login", "/setup", "/install", "/desktop-link",
    "/login/email", "/login/magic-link", "/login/magic-link/sent",
    "/password/reset", "/password/setup",
    "/admin/users/{user_id}", "/admin/groups/{group_id}",
    "/profile", "/error",
}


def _registered_html_routes() -> set[str]:
    paths: set[str] = set()
    for route in app.routes:
        if isinstance(route, APIRoute) and "GET" in route.methods:
            # Heuristic: HTML pages have no `/api/` prefix
            if not route.path.startswith("/api/") and not route.path.startswith("/auth/"):
                paths.add(route.path)
    return paths


def _nav_hrefs() -> set[str]:
    header = Path("app/web/templates/_app_header.html").read_text()
    return set(re.findall(r'href="(/[^"#?]+)"', header))


def test_every_nav_link_resolves():
    nav = _nav_hrefs()
    routes = _registered_html_routes()
    missing = nav - routes - ALLOWLIST
    assert not missing, f"Nav links to non-registered routes: {missing}"


def test_every_registered_page_is_reachable_or_allowlisted():
    nav = _nav_hrefs()
    routes = _registered_html_routes()
    orphans = routes - nav - ALLOWLIST
    assert not orphans, (
        f"Registered HTML routes neither in nav nor on allowlist: {orphans}. "
        "Either add to nav, add to ALLOWLIST with rationale, or drop the route."
    )
```

- [ ] **Step 4: Run the new test — expect it to FAIL with current orphans**

Run: `pytest tests/web/test_route_integrity.py -v`
Expected: `test_every_registered_page_is_reachable_or_allowlisted` fails listing `/admin/permissions` (and `/activity-center` if D1 = KEEP behind flag without removing the route, otherwise also fails on `/activity-center`).

This proves the test catches what the audit found.

- [ ] **Step 5: Delete `admin_permissions.html` + its route**

```bash
git rm app/web/templates/admin_permissions.html
```

Edit `app/web/router.py` — remove the `@router.get("/admin/permissions", …)` handler and the `permissions` import path it depends on. Keep `app/api/permissions.py` for now (it backs `dataset_permissions`, which the legacy access-request flow still uses); a follow-up plan will retire it once the access-request inbox lives in `admin_access.html`.

- [ ] **Step 6: Apply D1 outcome to activity_center**

If D1 = `[DELETE both]`:
```bash
git rm app/web/templates/activity_center.html
```
Drop `@router.get("/activity-center", …)` from `app/web/router.py`. Drop the dashboard widget block (`dashboard.html:2342-2412`) — verify exact line range before deleting since the file has shifted with prior edits.

If D1 = `[KEEP behind flag]`:
- Add `INSTANCE_FEATURES.activity_center: bool = False` to `app/instance_config.py`.
- Wrap the route handler with an early `raise HTTPException(404)` when flag is off.
- Wrap the dashboard widget in `{% if instance_features.activity_center %}…{% endif %}`.
- Add to ALLOWLIST in the regression test with comment.

- [ ] **Step 7: Strip dashboard SSH-key new-user flow**

Edit `app/web/templates/dashboard.html` and remove the `{% if not username_available or not user_info.exists %}…{% endif %}` block at `:2419-2509`. Verify after deletion that the surrounding template still parses (no orphan `{% endif %}` etc.).

Replace with a minimal welcome empty-state:

```jinja
{% if not user_info.exists %}
  <div class="welcome-pending">
    <h2>Welcome to {{ instance_name }}</h2>
    <p>Your account is being set up by an administrator. Once your access is configured, this page will show your data sources and tools.</p>
  </div>
{% endif %}
```

- [ ] **Step 8: Fix the 3 orphan API fetches**

Edit `app/web/templates/dashboard.html`:
- Line ~2687: `fetch('/api/sync-settings')` → `fetch('/api/sync/settings')` (the path with the slash).
- Line ~2790: the `/api/${channel}/unlink` call. Today `channel` can only be `telegram` (the only `*/unlink` endpoint that exists). Restrict the JS so non-telegram channels don't trigger the call, or hardcode the path. Simplest:
  ```js
  if (channel !== 'telegram') {
    showError('Unlink not supported for ' + channel);
    return;
  }
  await fetch('/api/telegram/unlink', {method: 'POST', …});
  ```

Edit `app/web/templates/admin_permissions.html` — N/A, file is being deleted in Step 5.

- [ ] **Step 9: Wire telegram status to the live endpoint**

Edit `app/web/router.py` around line 342: replace the hardcoded `telegram_status={"linked": False}` with a real call into the telegram service or a fetch on the page.

Cleanest: drop the server-side dummy entirely; let the dashboard JS hit `GET /api/telegram/status` on load and update the pill via DOM. Edit `app/web/templates/dashboard.html` to add the fetch + DOM update. Verify there's no other consumer of the route's `telegram_status` template var (`grep telegram_status app/web/templates/`).

- [ ] **Step 10: Add `last_used_ip` column to admin tokens table**

Read `app/web/templates/admin_tokens.html` and find the tokens-table `<tr>` template (likely a JS-rendered loop, so look at the JS that builds rows from the API response). Add an `IP` column that reads `last_used_ip || '—'`. Verify by reading the API response shape from `app/api/tokens.py` → confirm the field is `last_used_ip` (snake_case from the SQL row).

- [ ] **Step 11: Apply D2 outcome to login marketing**

If D2 = `[DELETE the claim]`: edit `app/web/templates/login.html` and remove the "Instant Automation" / scripts feature card. Also strip the corresponding language from `dashboard.html` if present (grep for "automation" / "scripts" / "Notification scripts" in the dashboard template).

If D2 = `[BUILD /admin/scripts]`: leave the marketing copy; Phase 7 will deliver.

- [ ] **Step 12: Run regression test — expect PASS now**

Run: `pytest tests/web/test_route_integrity.py -v`
Expected: both tests pass. The orphans we deleted are gone; everything in the nav resolves.

- [ ] **Step 13: Run the full web test suite**

Run: `pytest tests/web/ tests/api/ -q --tb=short`
Expected: all pass. Note any pre-existing failures in the commit body.

- [ ] **Step 14: Manual browser sweep**

Start dev server: `uvicorn app.main:app --reload --port 8000`
Click through (logged in as admin):
1. `/dashboard` — renders without the SSH-key block; telegram pill reflects real linked state.
2. `/admin/access`, `/admin/users`, `/admin/groups`, `/admin/tables`, `/admin/marketplaces`, `/admin/tokens` — all render; tokens table shows the IP column with values or em-dashes.
3. `/admin/permissions` — 404 (deleted route).
4. `/activity-center` — 404 if D1 = DELETE; renders empty if D1 = KEEP-behind-flag (flag default off).
5. Click any link from the nav header — none 404.

If any UI explicitly says "this is broken", STOP and reassess.

- [ ] **Step 15: Update CHANGELOG and commit**

Add to `CHANGELOG.md` under `## [Unreleased]`:

```markdown
### Removed
- Legacy `/admin/permissions` page and template (orphaned after RBAC v13 migration; access-request workflow will move into `/admin/access` in a follow-up).
- Dashboard SSH-key new-user onboarding flow (legacy auth model, replaced by Google OAuth + magic-link + PAT).
- (if D1 = DELETE) `/activity-center` page and dashboard widget — the route was rendering against undefined stub data.

### Fixed
- Dashboard JS no longer 404s on `/api/sync-settings` (correct path is `/api/sync/settings`).
- Dashboard JS no longer attempts `/api/<non-telegram>/unlink` for unsupported channels.
- Dashboard telegram status pill now reflects live `/api/telegram/status` instead of hardcoded "not linked".

### Added
- `last_used_ip` column on `/admin/tokens` table (data was already exposed by API).
- Regression test `tests/web/test_route_integrity.py` ensuring nav links and registered HTML routes stay in sync.
```

Commit:

```bash
git add -p   # stage selectively, do not include unintentional changes
git commit -m "cleanup(ui): remove dead pages, fix 3 orphan fetches, wire live telegram status"
```

---

## Phase 2: Navigation + destructive-confirm consistency

**Goal:** Make the nav honest (link to Catalog, Corporate Memory, Metrics) and stop using raw `confirm()` for destructive actions where a styled modal exists elsewhere in the codebase.

**Branch:** `feat/ui-nav-and-confirm-consistency`

**Files:**
- Modify: `app/web/templates/_app_header.html` (add 3 nav links; reorganize admin sub-menu so Tokens + Marketplaces sit inside the Admin dropdown rather than as top-level peers).
- Modify: `app/web/templates/dashboard.html`, `my_tokens.html`, `corporate_memory_admin.html` — replace raw `confirm()` for destructive ops with the modal pattern already used in `admin_groups.html` / `admin_users.html`.
- Conditional (per D4): merge five email-auth templates into two.
- Test: extend `tests/web/test_route_integrity.py` to assert Catalog / Corporate Memory / Metrics each appear in the nav.

- [ ] **Step 1: Audit current nav + confirm usage**

```bash
grep -nE 'href="/' app/web/templates/_app_header.html
grep -rn 'confirm(' app/web/templates/ --include="*.html"
```

Capture the existing modal-confirm pattern from `admin_groups.html` (likely a JS function building a modal `<div>` and returning a Promise<boolean>). Note its name.

- [ ] **Step 2: Extract the confirm modal into a shared partial**

If the modal is currently inlined per-page, extract to `app/web/templates/_confirm_modal.html` plus a global JS helper `app/web/static/js/confirm_modal.js` exposing `window.confirmDestructive(title, body, confirmLabel)` returning `Promise<boolean>`. Include from `base.html`.

This is a small refactor — concrete steps:

```bash
# 1. Find which template currently has the canonical modal markup
grep -nE 'class="confirm-modal|modal.*confirm' app/web/templates/admin_groups.html app/web/templates/admin_users.html
```

Read the matched lines, copy the modal `<div>` markup into `_confirm_modal.html`, copy the JS into `confirm_modal.js`. Update the source templates to `{% include "_confirm_modal.html" %}` and delete their inline copies.

- [ ] **Step 3: Replace `confirm()` calls**

Files to edit:
- `dashboard.html` — telegram unlink confirm at ~`:2788`.
- `my_tokens.html` — token revoke confirms.
- `corporate_memory_admin.html` — any raw `confirm()`.

Pattern:
```js
// Before
if (!confirm("Revoke token?")) return;
// After
if (!await confirmDestructive("Revoke token", "This token will stop working immediately.", "Revoke")) return;
```

- [ ] **Step 4: Add Catalog, Corporate Memory, Metrics to the top nav**

Edit `app/web/templates/_app_header.html`. Add three `<a>` entries between the existing Dashboard and Admin sections. For Metrics, point at `/catalog#metrics` if Phase 6 isn't shipped yet, or `/admin/metrics` once Phase 6 lands — choose the right destination per the order of phases.

- [ ] **Step 5: Reorganize the admin sub-menu**

Today, "All tokens" and "Marketplaces" are top-level peers of the Admin dropdown. Move them inside the dropdown alongside Users / Groups / Resource access / Registered tables. One pattern across all admin links.

- [ ] **Step 6: Extend route-integrity test**

Append to `tests/web/test_route_integrity.py`:

```python
def test_required_top_level_links_present():
    nav = _nav_hrefs()
    required = {"/catalog", "/corporate-memory"}
    missing = required - nav
    assert not missing, f"Top nav missing required entries: {missing}"
```

- [ ] **Step 7: Apply D4 if MERGE chosen**

(Only if D4 = MERGE.) Combine `login_email.html` + `login_magic_link.html` into `login_email.html` (single "request a magic link" form). Combine `login_magic_link_sent.html` + `password_reset.html` confirmation views into `login_email_sent.html`. Combine `password_setup.html` + token-landing variants into `password_set.html`. Update routes in `app/web/router.py` and any redirect logic in `app/auth/`.

This is the riskiest step in Phase 2 — only do it if D4 = MERGE; otherwise skip.

- [ ] **Step 8: Run tests + manual sweep**

```bash
pytest tests/web/ tests/api/ -q --tb=short
```
Expected: all pass. Manually click every nav link logged in as admin and as analyst; trigger one destructive action on each modified page to verify the new modal appears and behaves.

- [ ] **Step 9: CHANGELOG + commit**

```markdown
### Changed
- Top navigation now includes Catalog, Corporate Memory, and Metrics links.
- Admin sub-menu groups Tokens and Marketplaces under Admin (previously top-level).
- Destructive actions (token revoke, telegram unlink, memory delete) use the styled confirm modal everywhere instead of `window.confirm()`.
```

Commit: `feat(ui): nav links for catalog/memory/metrics, unified destructive-confirm modal`

---

## Phase 3: Design-system unification — tables + fonts + inline-style hoist

**Goal:** Stop the bleeding in the visual layer. Add a single global table rule, replace every hardcoded font family with a CSS variable, hoist 14 admin templates' inline `<style>` blocks into shared stylesheets. Visual regression is the main risk; manual sweep is the verification gate.

**Branch:** `style/ui-design-system-pass-1`

**Files:**
- Modify: `app/web/static/style-custom.css` (new sections: `.data-table`, hoisted admin-page rules, replaced font hardcodes).
- Modify: `app/web/static/style.css` (delete `body { font-family … }` at `:24`; replace mono hardcodes at `:287, :387` with `var(--font-mono)`).
- Modify: `app/web/static/css/metric_modal.css` (replace 4 hardcoded `'Monaco', 'Menlo'` with `var(--font-mono)`; delete fallback chains where `:root` already provides them).
- Modify: `app/web/static/js/metric_modal.js` (`:110` — replace `font-family: monospace` injected string with a class reference).
- Modify (delete inline `<style>` blocks): `admin_users.html`, `admin_tables.html`, `admin_groups.html`, `admin_group_detail.html`, `admin_marketplaces.html`, `admin_tokens.html`, `admin_user_detail.html`, `admin_access.html`, `my_tokens.html`, `profile.html`, `corporate_memory.html`, `corporate_memory_admin.html`. The page-specific blocks in `catalog.html` (the profiler), `dashboard.html` (hero), `install.html` (one-time wizard), and `_theme.html` stay.
- Modify: `app/web/templates/catalog.html` — replace per-cell inline `font-family: var(--font-mono); font-size: 11px` (around `:2169`) with a single class `.cat-cell-mono`; add `.table-row-desc { -webkit-line-clamp: 2; display: -webkit-box; overflow: hidden; }` to fix description wrapping.

- [ ] **Step 1: Inventory hardcoded font declarations**

```bash
grep -rnE "font-family\s*:" app/web/static/ app/web/templates/ \
  | grep -vE 'var\(--font-(primary|mono)\)' \
  | grep -vE '/\*|^[^:]+:[0-9]+:\s*--font-' \
  > /tmp/font-hardcodes.txt
wc -l /tmp/font-hardcodes.txt
```

This is the worklist for Step 4. Expected size: ~30 lines based on the audit.

- [ ] **Step 2: Define the global table rule**

Append to `app/web/static/style-custom.css`:

```css
/* === Tables === */
.data-table {
  width: 100%;
  border-collapse: collapse;
  table-layout: fixed;
  font-variant-numeric: tabular-nums;
}
.data-table th,
.data-table td {
  padding: 8px 12px;
  text-align: left;
  border-bottom: 1px solid var(--border-subtle);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.data-table tbody tr:hover {
  background: var(--surface-hover);
}
.data-table .cell-mono {
  font-family: var(--font-mono);
  font-size: 12px;
}
.data-table .cell-wrap {
  white-space: normal;
  overflow: visible;
}
.data-table .cell-truncate-2 {
  white-space: normal;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
```

- [ ] **Step 3: Apply `.data-table` to existing admin tables**

For each of: `admin_users.html` (`.users-table`), `admin_tables.html` (`.registry-table`), `admin_marketplaces.html` (`.marketplaces-table`), `admin_tokens.html`, `my_tokens.html`, add the `data-table` class alongside the existing class. Then delete the inline `<style>` rules that duplicate `.data-table` columns. Where a column legitimately needs to wrap (description, JSON blob), apply `cell-wrap` or `cell-truncate-2`.

- [ ] **Step 4: Replace hardcoded fonts with vars**

Walk `/tmp/font-hardcodes.txt` from Step 1. For each hit:
- If it's `'Monaco', 'Menlo', monospace` or similar → `var(--font-mono)`.
- If it's `'Inter', system-ui, …` or similar → `var(--font-primary)`.
- If it's the single `body { font-family: -apple-system… }` rule at `style.css:24` → DELETE the rule (the variable on body in `style-custom.css` already handles it).
- If it's `font-family: inherit` in a template — DELETE the rule entirely; it's noise.

Templates with the most repetition: `my_tokens.html` (11 redeclarations), `admin_marketplaces.html` (6).

- [ ] **Step 5: Fix the metric_modal.js inline injection**

Read `app/web/static/js/metric_modal.js:110`. Replace the inline `font-family: monospace` with `class="metric-mono"` and add `.metric-mono { font-family: var(--font-mono); }` to `app/web/static/css/metric_modal.css`.

- [ ] **Step 6: Hoist admin-page `<style>` blocks**

For each of the 12 admin/profile templates listed in the Files block above:
1. Cut the `<style>` block out of the template.
2. Group the rules by component (header, table, modal, search input, badge).
3. Append the rules to the matching section in `app/web/static/style-custom.css`, prefixed by a comment `/* hoisted from <template-name>.html */`.
4. Verify the page still renders via the manual sweep at the end of the phase.

If two templates had different rules for the same selector, the hoisted version wins; document the divergence in the commit message and fix any visual regressions in the sweep.

- [ ] **Step 7: Fix catalog table-row description wrapping**

Edit `app/web/templates/catalog.html`:
- Find `.table-row-desc` (around `:541` in the `<style>` block) and add `-webkit-line-clamp: 2; display: -webkit-box; -webkit-box-orient: vertical; overflow: hidden;`.
- Find the per-cell inline mono styles (around `:2169`) and replace with `class="cat-cell-mono"`. Add the class definition once in the same `<style>` block (it's the page-specific block we're keeping, so adding here is fine).

- [ ] **Step 8: Run tests**

```bash
pytest tests/web/ tests/api/ -q --tb=short
```
Expected: all pass. CSS changes don't have unit tests.

- [ ] **Step 9: Manual visual sweep — the verification gate for this phase**

Start dev server. As admin, walk through every page touched:
1. `/dashboard`, `/catalog`, `/corporate-memory`, `/profile`, `/my-tokens`
2. `/admin/users`, `/admin/groups`, `/admin/tables`, `/admin/marketplaces`, `/admin/tokens`, `/admin/access`, `/admin/users/{id}`, `/admin/groups/{id}`

For each: monospace columns are still monospace (no font fallback to Times); tables don't overflow horizontally; long descriptions truncate to 2 lines instead of pushing rows open; no obvious regressions vs. main.

Take screenshots of `/catalog` (the reported pain point) and `/admin/tables` before/after.

- [ ] **Step 10: CHANGELOG + commit**

```markdown
### Changed
- Tables across admin and analyst pages share a single `.data-table` style: `table-layout: fixed`, ellipsis truncation, hoverable rows, tabular-nums for timestamps. Long descriptions in the data catalog now truncate to 2 lines instead of pushing rows wider.
- Font families everywhere reference `--font-primary` / `--font-mono` variables; ~30 hardcoded `'Monaco'`/`'SF Mono'`/`'Inter'` literals removed.
- 12 admin/profile templates' inline `<style>` blocks hoisted into the global `style-custom.css` (~600 lines of duplicated CSS removed).

### Internal
- Deleted the duplicate `body { font-family: ... }` rule in `style.css:24`; v2 design tokens now apply uniformly.
```

Commit: `style(ui): unify tables + fonts, hoist 12 admin templates' inline CSS`

---

## Phase 4: `/admin/sync` — sync status + manual trigger

**Goal:** Replace ad-hoc sync controls scattered across the dashboard with a dedicated sync admin page that shows last-sync per source, sync_history, failures, and a trigger button.

**Branch:** `feat/admin-sync-page`

**Files:**
- Create: `app/web/templates/admin_sync.html`
- Modify: `app/web/router.py` — add `GET /admin/sync` route requiring `Depends(require_admin)`.
- Modify: `app/api/sync.py` — confirm endpoints have JSON shapes the page needs (last sync time, history with status + duration + error). Add a thin endpoint `GET /api/sync/history?source=&limit=` if needed (read-only, admin-gated).
- Modify: `app/web/templates/_app_header.html` — add Sync to admin sub-menu.
- Modify: `app/web/templates/dashboard.html` — remove ad-hoc sync controls (or leave a "Sync now" shortcut that links to `/admin/sync`).
- Test: `tests/web/test_admin_sync_page.py`, `tests/api/test_sync_history_endpoint.py`.

- [ ] **Step 1: Survey the data already exposed**

Read `app/api/sync.py`, `src/repositories/sync_state.py` (and sync_history if it has its own repo). List the columns. Note which API endpoint returns each field.

If a `GET /api/sync/history` endpoint doesn't exist, design it now:

```
GET /api/sync/history?source={name}&limit={int}
Response: list of {source, started_at, finished_at, status, rows_extracted, error_message}
Auth: require_admin
```

- [ ] **Step 2: TDD — failing test for the history endpoint**

Create `tests/api/test_sync_history_endpoint.py`:

```python
def test_sync_history_admin_only(client, admin_token):
    r = client.get("/api/sync/history", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    if body:
        first = body[0]
        for k in ("source", "started_at", "status"):
            assert k in first

def test_sync_history_rejects_non_admin(client, analyst_token):
    r = client.get("/api/sync/history", headers={"Authorization": f"Bearer {analyst_token}"})
    assert r.status_code == 403
```

Run: `pytest tests/api/test_sync_history_endpoint.py -v` → expect FAIL (route not registered).

- [ ] **Step 3: Implement the endpoint**

In `app/api/sync.py`, add the handler. Read from `sync_history` table via the existing repository; if the repo doesn't have a `list_history()` method, add it (3-line SQL `SELECT … ORDER BY started_at DESC LIMIT ?`).

Run the test → expect PASS.

- [ ] **Step 4: Build the template**

Create `app/web/templates/admin_sync.html` extending `base.html`. Sections:
1. **Per-source summary** — table of (source, last sync, status, next sync, [Sync now] button).
2. **Recent history** — `.data-table` with columns Started / Source / Status / Duration / Rows / Error. Use the new endpoint.
3. **Manual trigger** — POSTs `/api/sync/trigger`, shows result inline.

Use `.data-table` from Phase 3. Keep page-specific CSS minimal (or zero).

- [ ] **Step 5: TDD — failing test for the page**

`tests/web/test_admin_sync_page.py`:

```python
def test_admin_sync_page_renders_for_admin(admin_client):
    r = admin_client.get("/admin/sync")
    assert r.status_code == 200
    assert "Sync" in r.text

def test_admin_sync_page_forbidden_for_analyst(analyst_client):
    r = analyst_client.get("/admin/sync")
    assert r.status_code in (302, 403)  # depends on existing redirect logic
```

Run → expect FAIL.

- [ ] **Step 6: Register the route + nav link**

Edit `app/web/router.py`:

```python
@router.get("/admin/sync", response_class=HTMLResponse)
def admin_sync(request: Request, user=Depends(require_admin)):
    return templates.TemplateResponse(request, "admin_sync.html", {"user": user})
```

Edit `_app_header.html` to include the Sync link in the admin sub-menu.

Run the test → expect PASS.

- [ ] **Step 7: Trim dashboard ad-hoc sync controls**

In `dashboard.html`, find the "Sync now" controls (search for `/api/sync/trigger`). Either delete them (canonical home is now `/admin/sync`) or replace with a small button that links to `/admin/sync` for the admin role. Run the manual sweep on `/dashboard` to confirm.

- [ ] **Step 8: Manual end-to-end check**

```bash
uvicorn app.main:app --reload --port 8000
```
- Log in as admin → visit `/admin/sync`. Confirm summary, history, and trigger button render with real data.
- Click "Sync now" — POST should return a JSON response and the row should refresh.
- Log in as analyst → confirm `/admin/sync` redirects/403s.

- [ ] **Step 9: CHANGELOG + commit**

```markdown
### Added
- `/admin/sync` page: per-source last sync, recent history (status, duration, rows, error), and a manual trigger button. Replaces ad-hoc sync controls on the dashboard.
- `GET /api/sync/history?source=&limit=` endpoint (admin only) returning sync_history rows.

### Changed
- Dashboard no longer surfaces sync controls directly; the canonical sync console is `/admin/sync`.
```

Commit: `feat(admin): add /admin/sync page and history endpoint`

---

## Phase 5: `/admin/settings` — instance settings + sync subscriptions

**Goal:** Build a single settings page that exposes the 6 currently-UI-less endpoints from `app/api/settings.py` and the per-table sync subscriptions in `app/api/sync.py`.

**Branch:** `feat/admin-settings-page`

**Files:**
- Create: `app/web/templates/admin_settings.html`
- Modify: `app/web/router.py` — add `GET /admin/settings` route.
- Modify: `app/web/templates/_app_header.html` — add Settings to admin sub-menu.
- Test: `tests/web/test_admin_settings_page.py`.

- [ ] **Step 1: Map endpoints to page sections**

Walk `app/api/settings.py` and `app/api/sync.py`. Identify the union of fields exposed:
- `GET /api/settings` → user's sync settings + permissions overview.
- `PUT /api/settings/dataset` → toggle dataset sync.
- `GET /api/sync/settings` → bulk dataset settings.
- `POST /api/sync/settings` → bulk update.
- `GET /api/sync/table-subscriptions` → per-table prefs.
- `POST /api/sync/table-subscriptions` → update per-table prefs.

Document each in the page as a section. If `settings.py` and `sync.py` define overlapping concepts, pick one and deprecate the other in a follow-up plan.

- [ ] **Step 2: TDD — failing route test**

`tests/web/test_admin_settings_page.py`:

```python
def test_admin_settings_renders(admin_client):
    r = admin_client.get("/admin/settings")
    assert r.status_code == 200

def test_admin_settings_forbidden_for_analyst(analyst_client):
    r = analyst_client.get("/admin/settings")
    assert r.status_code in (302, 403)
```

Run → FAIL.

- [ ] **Step 3: Build the template**

`app/web/templates/admin_settings.html` extending `base.html`. Sections per Step 1. Each section uses `<form>` POSTing JSON to the existing endpoint via `fetch`. Use `.data-table` for the table-subscriptions list.

- [ ] **Step 4: Register route + nav**

`app/web/router.py`:

```python
@router.get("/admin/settings", response_class=HTMLResponse)
def admin_settings(request: Request, user=Depends(require_admin)):
    return templates.TemplateResponse(request, "admin_settings.html", {"user": user})
```

Add to `_app_header.html`.

Run the test → PASS.

- [ ] **Step 5: Manual end-to-end check**

Toggle a dataset sync setting; verify the API call lands and the page reflects the new state on reload. Toggle a per-table subscription. Verify analyst can't reach the page.

- [ ] **Step 6: CHANGELOG + commit**

```markdown
### Added
- `/admin/settings` page exposing instance-level sync settings, dataset sync toggles, and per-table subscriptions (previously only reachable via API/CLI).
```

Commit: `feat(admin): add /admin/settings page wiring existing settings API`

---

## Phase 6: `/admin/metrics` — metric definitions list + view + import

**Goal:** Surface the `metric_definitions` table in the UI. Today the only path is `da metrics` CLI; ops can't inspect or update metrics from the browser.

**Branch:** `feat/admin-metrics-page`

**Files:**
- Create: `app/web/templates/admin_metrics.html`, `app/web/templates/admin_metric_detail.html`.
- Modify: `app/web/router.py` — `GET /admin/metrics`, `GET /admin/metrics/{id}`.
- Modify: `app/web/templates/_app_header.html` — Metrics admin sub-menu.
- Modify: `app/web/templates/catalog.html` — link the catalog "Metrics" accordion entries to `/admin/metrics/{id}` for admins (analysts still see the inline modal).
- Test: `tests/web/test_admin_metrics_page.py`.

- [ ] **Step 1: Read the API**

`app/api/metrics.py` exposes 5 endpoints. Confirm the response shapes for `GET /api/metrics` and `GET /api/metrics/{id}` (id, name, sql, description, tags, business rules?). Note any missing fields you need on the page; if a field exists in the DB but isn't returned, extend the endpoint (admin-only) — don't add a parallel endpoint.

- [ ] **Step 2: TDD — failing route tests**

`tests/web/test_admin_metrics_page.py`:

```python
def test_metrics_list_admin_only(admin_client, analyst_client):
    r1 = admin_client.get("/admin/metrics")
    assert r1.status_code == 200
    r2 = analyst_client.get("/admin/metrics")
    assert r2.status_code in (302, 403)
```

Run → FAIL.

- [ ] **Step 3: Build list page**

`admin_metrics.html`: `.data-table` of (name, description, last updated, [View]). Link to detail page.

- [ ] **Step 4: Build detail page**

`admin_metric_detail.html`: shows name, description, SQL (in `<pre class="cell-mono">`), tags, business rules. Read-only for now (CLI is still source of truth for edits — surface a "Managed via `da metrics import`" notice; no edit form yet).

- [ ] **Step 5: Register routes + nav**

```python
@router.get("/admin/metrics", response_class=HTMLResponse)
def admin_metrics(request: Request, user=Depends(require_admin)):
    return templates.TemplateResponse(request, "admin_metrics.html", {"user": user})

@router.get("/admin/metrics/{metric_id}", response_class=HTMLResponse)
def admin_metric_detail(metric_id: str, request: Request, user=Depends(require_admin)):
    return templates.TemplateResponse(request, "admin_metric_detail.html", {"user": user, "metric_id": metric_id})
```

Add nav entry. Run test → PASS.

- [ ] **Step 6: Link from catalog**

In `catalog.html`, for the "Business Metrics" data-source accordion entries, add an admin-only "Edit in admin" link to `/admin/metrics/{id}` (use the `is_admin` template var).

- [ ] **Step 7: Manual sweep**

Visit `/admin/metrics` as admin; click through to a detail; verify SQL renders monospace; verify analyst can't reach the page.

- [ ] **Step 8: CHANGELOG + commit**

```markdown
### Added
- `/admin/metrics` and `/admin/metrics/{id}` pages: read-only browser surface for `metric_definitions` (list, view SQL + business rules). Editing remains via `da metrics import` until a follow-up plan introduces in-place editing.
```

Commit: `feat(admin): add /admin/metrics list + detail pages`

---

## Phase 7: `/admin/scripts` (CONDITIONAL on D2)

Only ships if D2 = `[BUILD]`. If D2 = `[DELETE the marketing claim]`, this phase is skipped — Phase 1 already removed the claim.

**Goal:** Operator UI for the 5 endpoints in `app/api/scripts.py` (list / deploy / run-by-id / run-ad-hoc / delete). High blast radius (server-side Python execution); needs explicit confirms and clear "what does this do" framing.

**Branch:** `feat/admin-scripts-page`

**Files:**
- Create: `app/web/templates/admin_scripts.html`, `app/web/templates/admin_script_detail.html`.
- Modify: `app/web/router.py` — `GET /admin/scripts`, `GET /admin/scripts/{id}`.
- Modify: `_app_header.html` — Scripts admin sub-menu.
- Test: `tests/web/test_admin_scripts_page.py`.

- [ ] **Step 1: Audit the API**

Read `app/api/scripts.py`. Confirm: who can run scripts, what gets logged, where output goes, any sandboxing. If there's no audit log, add one in this phase (the endpoint should write to `audit_log` per `src/repositories/`). The UI surfacing this without an audit trail is a hazard.

- [ ] **Step 2: TDD — list page**

```python
def test_scripts_admin_only(admin_client, analyst_client):
    r1 = admin_client.get("/admin/scripts")
    assert r1.status_code == 200
    r2 = analyst_client.get("/admin/scripts")
    assert r2.status_code in (302, 403)
```

Run → FAIL.

- [ ] **Step 3: Build list page**

`.data-table` of (name, deployed_by, deployed_at, last_run, [Detail]). Add "Deploy new" button opening a modal with file upload (calls `POST /api/scripts/deploy`). Use the `confirmDestructive` modal from Phase 2 for delete actions.

- [ ] **Step 4: Build detail page**

Shows script source (`<pre>`), metadata, run history (last N from audit_log filtered for this script), [Run now] button, [Delete] button (destructive confirm).

The Run flow: POST returns output; render in a `<pre>` below the button. If the run is long, this is a future improvement — for now, a synchronous response is acceptable.

- [ ] **Step 5: Register routes + nav + run audit**

If `audit_log` writes don't already happen in `app/api/scripts.py`, add them in this PR — every deploy / run / delete writes a row.

- [ ] **Step 6: Manual sweep**

Deploy a tiny script, run it, see output, delete it. Verify audit_log has 3 entries.

- [ ] **Step 7: CHANGELOG + commit**

```markdown
### Added
- `/admin/scripts` page: list deployed Python scripts, deploy new, run on demand, delete. Every action writes to `audit_log`. Replaces "drop a `.py` file in `~/user/notifications/`" with an auditable browser surface.
```

Commit: `feat(admin): add /admin/scripts page with audit trail`

---

## Phase 8: Catalog refactor — split profiler, hoist remaining inline CSS

**Goal:** Bring `catalog.html` from 2 749 lines down by extracting the profiler overlay (~1000 lines of CSS+HTML+JS) and pushing the metric-modal inline JS to the existing `static/js/metric_modal.js` (already extracted, the inline copy is a duplicate).

**Branch:** `refactor/catalog-split-profiler`

**Files:** depends on D3.

- [ ] **Step 1: Audit catalog.html sections**

Read the file in chunks. Document the section boundaries (CSS block / source-cards / profiler overlay / request-access modal / metric modal / two JS blocks). Confirm the profiler overlay is self-contained (uses no shared template state besides the table id).

- [ ] **Step 2: Apply D3**

If D3 = `[OWN PAGE]`:
1. Create `app/web/templates/profile.html` (rename: probably `profile_table.html` to avoid collision with the user profile page) and a `GET /catalog/{table}/profile` route in `app/web/router.py`.
2. Move the profiler `<div>` + CSS + JS from `catalog.html` into the new template.
3. Replace the in-page overlay in `catalog.html` with a link `/catalog/{table}/profile` on each row.
4. Add `app/web/static/css/profiler.css` for the hoisted CSS.

If D3 = `[JINJA PARTIAL]`:
1. Create `app/web/templates/_profiler_overlay.html` and move the profiler `<div>` markup there.
2. `{% include "_profiler_overlay.html" %}` from `catalog.html`.
3. Move the profiler CSS to `app/web/static/css/profiler.css`; load via `base.html`.
4. Move the profiler JS to `app/web/static/js/profiler.js`; load via `base.html`.

If D3 = `[LEAVE]`: skip the split. Just move the profiler CSS to `app/web/static/css/profiler.css` and the JS to `app/web/static/js/profiler.js`. Leave the overlay markup in `catalog.html`. Reduces catalog.html by ~500 lines without behavioral change.

- [ ] **Step 3: Delete the duplicated metric-modal JS in catalog.html**

`app/web/static/js/metric_modal.js` already exists and is loaded. Find the inline duplicate in `catalog.html` (search for `function openMetricModal` or similar) and delete it. Verify the modal still works.

- [ ] **Step 4: Manual sweep**

Open `/catalog`. Click a table row → profiler overlay (or new page) loads with all six tabs (Overview / Columns / Insights / Missing Values / Relationships / Sample). Click a metric → metric modal opens. No regressions.

- [ ] **Step 5: CHANGELOG + commit**

```markdown
### Changed
- Catalog page slimmed by ~1000 lines: profiler overlay extracted (per D3 outcome), and the duplicated metric-modal JS in `catalog.html` removed in favor of the existing `static/js/metric_modal.js`. No UX changes.
```

Commit: `refactor(ui): slim catalog.html, extract profiler`

---

## Phase 9: Final design-system consolidation (CONDITIONAL on D5)

Only ships if D5 = `[KILL style.css]`. Otherwise skipped.

**Goal:** Delete `app/web/static/style.css`. Port the few unique rules into `style-custom.css`. Drop `-v2` suffixes throughout. Add a tablet breakpoint.

**Branch:** `style/retire-style-css`

- [ ] **Step 1: Audit what `style.css` still uniquely provides**

```bash
# What rules in style.css are NOT redefined in style-custom.css?
# Heuristic: extract selectors, diff.
grep -nE '^[a-zA-Z\.\#\:][^{]*\{' app/web/static/style.css \
  | awk -F'{' '{print $1}' | sort -u > /tmp/style-selectors.txt
grep -nE '^[a-zA-Z\.\#\:][^{]*\{' app/web/static/style-custom.css \
  | awk -F'{' '{print $1}' | sort -u > /tmp/custom-selectors.txt
comm -23 /tmp/style-selectors.txt /tmp/custom-selectors.txt
```

Result is the worklist. Likely candidates: `.btn-google`, login form rules, `.badge-*` family, copy-button family.

- [ ] **Step 2: Port unique rules into `style-custom.css`**

For each surviving rule, copy into the matching section of `style-custom.css`. If two definitions differ, prefer the v2 visual; document the chosen variant in the commit.

- [ ] **Step 3: Drop `-v2` suffixes**

Rename selectors:
```bash
# Find all -v2 selectors
grep -rnE '\.btn-[a-z-]*-v2|\.header-v2|\.container-v2' app/web/static/style-custom.css app/web/templates/
```
Use `sed -i ''` (BSD/macOS) or scripted Edit calls to rename consistently across CSS + templates.

- [ ] **Step 4: Delete `style.css` + drop the `<link>` from base templates**

```bash
git rm app/web/static/style.css
```

Edit `app/web/templates/base.html` and `app/web/templates/base_login.html`: remove the `<link rel="stylesheet" href="/static/style.css">` lines.

- [ ] **Step 5: Add a tablet breakpoint**

Append to `style-custom.css`:

```css
@media (max-width: 1024px) {
  .container-v2 { padding-inline: 16px; }   /* tighten gutter */
  .data-table { font-size: 13px; }
  .data-table th, .data-table td { padding: 6px 8px; }
  .nav-secondary { display: none; }   /* hide secondary nav at tablet */
}
```

Adjust selectors to whatever Phase 9 rename produces.

- [ ] **Step 6: Manual sweep — every page**

This is the riskiest phase. Walk every page (logged in as both admin and analyst) at three viewports: 1440px, 1024px, 800px. Capture before/after screenshots of `/login`, `/dashboard`, `/catalog`, `/admin/access`, `/admin/users`, `/profile`. Compare for regressions.

- [ ] **Step 7: CHANGELOG + commit**

```markdown
### Removed
- `app/web/static/style.css`. The v1 stylesheet is retired; all rules merged into `style-custom.css` (renamed to drop the `-v2` suffix).

### Changed
- Added tablet breakpoint (`@media (max-width: 1024px)`) for admin tables and navigation.

### Internal
- Single CSS file is now the source of truth for the web UI design system.
```

Commit: `style(ui): retire style.css, drop -v2 suffix, add tablet breakpoint`

---

## Self-review — gaps and consistency check

**Spec coverage** — every audit finding maps to a phase:

| Audit finding | Phase |
|---|---|
| Activity center renders against undefined | Phase 1 (D1) |
| admin_permissions.html orphan | Phase 1 |
| SSH-key new-user flow legacy | Phase 1 |
| 3 orphan API fetches | Phase 1 |
| Telegram status hardcoded | Phase 1 |
| `last_used_ip` missing in template | Phase 1 |
| Login marketing claim about scripts | Phase 1 (D2) |
| Catalog/Memory/Metrics not in nav | Phase 2 |
| Raw `confirm()` inconsistency | Phase 2 |
| 5 email-auth pages | Phase 2 (D4) |
| No global table rule | Phase 3 |
| 30+ hardcoded font families | Phase 3 |
| 12 admin templates with inline `<style>` | Phase 3 |
| Catalog `.table-row-desc` wrap | Phase 3 |
| metric_modal.js inline font | Phase 3 |
| `body { font-family }` in style.css | Phase 3 |
| Sync trigger CLI-only | Phase 4 |
| Sync status no UI | Phase 4 |
| Settings page missing | Phase 5 |
| Per-table subscriptions no UI | Phase 5 |
| Metrics CRUD CLI-only | Phase 6 |
| Scripts API no UI | Phase 7 (D2) |
| Catalog profiler 1000-line inline | Phase 8 (D3) |
| Duplicate metric-modal JS in catalog | Phase 8 |
| Two competing stylesheets | Phase 9 (D5) |
| `-v2` suffix migration | Phase 9 (D5) |
| Tablet breakpoint missing | Phase 9 (D5) |

**Type/path consistency** — admin route prefix is `/admin/<thing>`; admin templates are `admin_<thing>.html`; admin API endpoints use `Depends(require_admin)`. Test files mirror under `tests/web/` and `tests/api/`. Nav additions match route paths.

**Placeholder scan** — no "TBD" / "implement later". Each task either contains the actual edits, the actual grep, or a verification step with expected output. A few steps reference helper functions defined in earlier steps (e.g. `confirmDestructive` from Phase 2 used in Phase 7) — that's intentional dependency, not a placeholder.

**Out-of-scope items left for future plans:**
- Retiring `app/api/permissions.py` and the legacy `dataset_permissions` table (depends on moving access-request inbox into `/admin/access`).
- In-place editing of metric definitions from `/admin/metrics` (Phase 6 ships read-only; edits stay on `da metrics import`).
- Async / streamed output for long-running script runs (Phase 7 is synchronous).
- A11y audit pass (search input labels, color-only signals, ARIA) — flagged by the visual audit; should be a separate plan.

---

## Execution handoff

Plan complete and saved here. **Two questions back to you before we start cutting:**

1. **D1–D5** (the 5 product decisions in Phase 0) — pick or comment.
2. **D6** — execution mode: subagent-driven (one fresh subagent per phase, two-stage review between phases) vs inline (this session, checkpoint after each phase).

Default if you say "just go": D1=DELETE, D2=DELETE the claim, D3=JINJA PARTIAL, D4=LEAVE, D5=KILL style.css, D6=inline (checkpoint between phases). I'll cut Phase 1 first and pause for review before continuing.
