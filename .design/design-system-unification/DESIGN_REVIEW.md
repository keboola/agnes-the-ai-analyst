# Design Review: Design System Unification

Reviewed against: `DESIGN_BRIEF.md`
Philosophy: Calm editorial dashboard — borders-dominant, shadows reserved, green primary + navy hero + white surfaces.
Date: 2026-05-21
Mode: Code-only (visual capture skipped at user request — Chrome MCP unpaired, dev server down)

## Screenshots Captured

None. The dev server isn't running, Chrome MCP has no paired browser, and the user opted to defer visual capture in favour of a code-only review. Style deltas that *would* appear in a screenshot diff are catalogued under **Style Deltas Introduced** below so they can be spot-checked in a browser later.

To produce screenshots later: start the dev server (`.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000`), pair Chrome MCP, and capture the 12 priority pages (auth → catalog → marketplace → admin) at 1280 / 768 / 375 widths. The brief lives at `.design/design-system-unification/DESIGN_BRIEF.md`; screenshots should land in `.design/design-system-unification/screenshots/`.

## Summary

The refactor landed substantially on this branch: **12 commits ahead of main, 40 templates now route their `<button>` / `<a class="btn-*">` markup through the 5 shared macros** in `_components.html`, ~200 button instances converted, ~150 lines of duplicated per-button CSS removed across `.obs-btn`, `.welcome-btn`, `.gp-btn`, `.submit-btn`, `.reset-btn`, `.guide-cta a`, `.pkg-hero__actions .btn`, `.delete` family, `.section-card`, `.pkg-section`, `.rcp-section`. The brief's "one way to do each thing" rule now holds for the simple cases — Cancel/Save/Delete/Submit/Apply on the refactored pages all render through the same vocabulary.

**Four files explicitly deferred** to dedicated follow-up PRs because each requires a decision before macro conversion (extend the macros, promote a bespoke variant family to canonical, or commit to page-local): `admin_corporate_memory` (3951 lines, has its own `.btn-mandate`/`.btn-approve`/`.btn-reject`/`.btn-revoke` family), `admin_tables` (5748 lines, 66 buttons in many bespoke modals), `dashboard` (1539 lines, heavy bespoke chrome including `.btn-setup`/`.notif-link`/`.btn-copy-term`/`.btn-register`/terminal mock), `admin_server_config` (1462 lines, 0 canonical `.btn-*` buttons — entirely bespoke config-editor chrome). Plus `install.html` (1066) and `admin_access.html` (862) which carry only JS-generated buttons inside `<script>` template literals that Jinja macros can't reach.

## What Was Built

### Foundation (commit `2e10da3e`)

- `app/web/templates/_components.html` — 5 macros (`button`, `primary_nav`, `tabs`, `table`, `panel`) with `tag=` parameter on `panel` for landmark sections.
- `app/web/templates/base_ds.html` — opt-in design-system base layout that auto-imports `_components.html` as `ds`. Currently used by zero pages (foothold for future migration).
- `.interface-design/system.md` — the contract documenting class vocabulary, token names, accent semantics, depth strategy.
- `app/web/static/css/design-tokens.css` — `--ds-accent-{info,success,warn,danger}-{bg,ink,line}` cross-pattern accent vocabulary, `--ds-focus-outline` + offset, `--ds-text-{disabled,inverse,link}`.
- `app/web/static/style-custom.css` — `.btn--icon` modifier, full `.ds-card` family (`__title`, `--accent`, `--info/success/warn/danger`, `[data-clickable]:hover/focus`), full `.ds-table` family with 15 legacy aliases via `:is()`, `.badge--{info,success,warn,danger}` modifiers, `.code-inline` mint chip.

### Playbook (commit `d74ba387`)

- `.design/design-system-unification/REFACTOR_PLAYBOOK.md` — 671 lines codifying the 8-step per-page workflow, convert-or-skip decision tree, macro quick-reference, common anti-patterns, and the patterns the macros don't model. Used as the driver for every batch below.

### Templates refactored (40 across 10 batches)

| Commit | Batch | Files | Buttons converted |
|--------|-------|-------|-------------------|
| `ebcd3636` | Auth | login, password_reset, password_setup, login_magic_link, login_magic_link_sent, login_email, error (7 files) | ~17 |
| `18d33b96` | Setup | setup.html | 7 wizard buttons |
| `fa7cb338` | User | profile, me_activity, corporate_memory, memory_domain_detail (4) | ~12 + 3 section-card panels |
| `dc3cd8e1` | Store | store_edit, store_upload (2) | ~6 |
| `62ab411c` | Catalog | catalog, catalog_package_detail, catalog_recipe_detail (3) | ~12 + 6 section panels |
| `31342e8e` | Marketplace | marketplace, marketplace_guide, marketplace_item_detail, marketplace_plugin_detail (4) + 3 test pin updates | ~24 |
| `b867d08e` | Activity center | activity_center (1) | 6 `.obs-btn` |
| `35d20b89` | Home | home_onboarded (fallback), home_not_onboarded (live) (2) | 5 quick-cards + 2 buttons |
| `868df3ab` | Admin small | 10 admin pages (session_detail, store_submissions, groups, sessions, group_detail, usage, welcome, workspace_prompt, users, store_submission_detail) | ~38 |
| `d2c2dc0a` | Admin medium | admin_marketplaces, admin_user_detail, admin_tokens, admin/news_editor (4) | ~24 |

## Conformance to Brief

### One way to do each thing — substantially holds

| Pattern | Status |
|---------|--------|
| Primary CTA | Single path: `ds.button(variant='primary')` across all 40 refactored templates. |
| Destructive action | Single path: `ds.button(variant='danger')`. Used consistently for Delete / Hard delete / Revoke / Confirm-delete / Remove from stack. |
| Modal Cancel + confirm | Consistently `ds.button(variant='secondary')` + variant-typed confirm across admin_groups, admin_users, admin_marketplaces, admin_welcome, admin_workspace_prompt, catalog, corporate_memory, marketplace_guide. |
| Paginator (Prev/Next) | Consistently `ds.button(variant='secondary', size='sm')` on admin_sessions, admin_usage, admin_user_detail, me_activity, activity_center. |
| Owner-actions row on flea detail pages | marketplace_item_detail + marketplace_plugin_detail now share the same Edit/Archive/Hard-delete pattern via `ds.button(variant='secondary'\|'danger', size='sm')`. |
| Bespoke amber "required" disabled state | Still `.btn-required` (page-local) on catalog_package_detail + memory_domain_detail. Not promoted to canonical yet — flagged below. |

### Tokens, not literals — partial

The brief calls out: *"A raw 12px or #2ea877 in the diff is a code smell unless it's defining a token."*

Reality: refactored pages still ship raw hex (24 in `profile.html`, 17 in `setup.html`, 17 in `me_activity.html`, 64 in `home_not_onboarded.html`). Most are chip-color rules (`.group-chip.is-admin { background: #fef3c7; color: #92400e; }`) or page-local accent colors (`.role-chip.is-core { background: rgba(16, 185, 129, 0.10); }`). The new `--ds-accent-{info,success,warn,danger}-{bg,ink,line}` tokens (added in commit `2e10da3e`) cover most of these cases but no page has been swept yet. Total `var(--ds-*)` references across all templates: **304** — substantial existing adoption, more to go.

### Borders-dominant, shadows reserved — preserved

The canonical `.ds-card` carries only `--shadow-sm` (whisper); `ds.panel` adopters inherit this. Profile / catalog / recipe sections now get a subtle lift they didn't have before (was bg+border only) — see **Style Deltas**.

### Calm hover behaviour — mixed

`.ds-card[data-clickable]:hover` is neutral (border darkens to `--ds-text-muted`, `--shadow-md`, 1px lift). Adopted on `home_onboarded`'s quick-cards. But the quick-card preserves a green-brand hover override intentionally to keep `/home`'s recognisable feel. Conscious deviation; documented in `home_onboarded.html` import comment.

## Style Deltas Introduced

These are the visible-in-screenshot-diff changes from the refactor. None breaks behaviour; all align with brief direction.

### Color shifts on primary buttons

- **`admin_store_submissions` Apply**: black fill → canonical green primary. Was custom `.submit-btn { background: var(--text); }`.
- **`admin_groups` + New group**: page-local `.gp-btn.primary { background: var(--primary); }` → canonical `.btn-primary`. Same visual under the design-token shim, but no longer a separate vocabulary.
- **`admin_users` + Add user**, **`admin_welcome`/`admin_workspace_prompt` Save override**: same pattern.
- **`marketplace_guide` `.guide-cta` buttons**: padding shifts from page-local `9×16` to canonical `.btn-primary`/`.btn-secondary` defaults (~10×18, ~2px taller). Hover treatment shifts from "neutral border + green text" to canonical "darken bg" treatment.

### Hover treatment shifts on secondary buttons

- **`.obs-btn` paginators** (activity_center, admin_sessions, admin_usage, admin_session_detail): border-radius `6px` → canonical `8px`; hover fill shifts from `--border-light` to `--ds-surface-dim`. Same conceptual treatment, slightly different exact value.
- **`.welcome-btn`** (admin_welcome, admin_workspace_prompt): padding `8×16` → `10×18`. Slightly taller buttons.
- **`marketplace_item_detail` / `marketplace_plugin_detail` `.delete` buttons**: hover shifts from "soft red wash" / no hover to canonical `.btn-danger:hover` "fills red on hover". System.md explicit: *"Destructive — committed to by hover, not announced by default."* Aligned with brief.
- **`admin_user_detail` `.account-action-btn` row**: was bespoke `padding: 7px 14px`, now `.btn-secondary.btn-sm` (6×12). Reset password / Deactivate / Delete buttons shrink by ~1px vertical, ~2px horizontal.

### Card / panel surface deltas

- **`profile.html` `.section-card`** + **`catalog_package_detail` `.pkg-section`** + **`catalog_recipe_detail` `.rcp-section`**: now carry `.ds-card`'s `--shadow-sm` (was bg+border only). Very subtle lift.
- **`home_onboarded.html` `.quick-card`**: radius unchanged (12px override preserved), green-brand hover preserved via `data-clickable:hover` override.

## Must Fix

None. No broken functionality; the per-batch test runs across the refactor came back green (auth: 71 pass, setup: 106, user pages: 34, store: passed, catalog: 49, marketplace: 185, activity_center: 39, home: 38, admin-small: 98, admin-medium: 50). No a11y regressions detected in code review.

## Should Fix

1. **`.btn-required` is used by 2 templates but has no canonical definition.** Both `catalog_package_detail.html` and `memory_domain_detail.html` carry their own page-local amber-disabled rule. It's the start of a vocabulary. _Fix: promote `.btn-required` to style-custom.css with canonical amber-on-amber styling, or absorb into `.btn-secondary[disabled][data-required="true"]`._

2. **3 test pins were loosened** (`tests/test_web_marketplace_guide.py` lines ~62, ~106, ~152) to accept the macro's attribute-order output. _Fix-or-suggestion_: worth a follow-up sweep that converts every exact-string template assertion in the test suite to semantic matching (regex on class + href + text). Would unblock faster future refactors.

3. **`admin_marketplaces` system-confirm modal still uses `.btn-warning`** — bespoke amber-fill variant outside the canonical 4 (primary/secondary/danger/ghost). One line in `admin_marketplaces.html` line ~386 has a doc-comment now but the variant should either be added to system.md as a 5th canonical variant or replaced with `.btn-danger` (system-confirm is destructive — fanout affects every principal). _Fix: pick one and ship it._

4. **`base_ds.html` adoption is zero.** The opt-in layout was built in turn 1 but no page has migrated. The migration requires extracting inline JS from `base.html` (undo toast, modal-Esc, cmd palette, admin shortcuts) into a `_app_scripts.html` partial first. Tracked in the `base_ds.html` doc-comment. _Fix: queue a separate PR for the extraction; once done, migrate one page (e.g. profile.html) as the proof-point._

## Could Improve

1. **Hex-literal cleanup pass**. ~122 raw hex values remain across just 4 sampled refactored templates (profile, setup, me_activity, home_not_onboarded). Most are chip colors that have `--ds-accent-*` equivalents. Pure follow-up — out of the partials-first scope but the natural next pass.

2. **Legacy `var(--primary)` → `var(--ds-primary)` sweep**. The design-token shim transparently redirects, but explicit `--ds-*` references make the code self-documenting and remove the dependency on the shim. Templates like `corporate_memory`, `me_activity`, `memory_domain_detail`, `dashboard` still reference legacy tokens heavily.

3. **`.account-action-btn` CSS in `admin_user_detail.html` is now half-dead** — the page-rendered buttons converted to `ds.button` but the rule lingers for the JS-generated row Remove button. Worth a follow-up where the JS-side gets refactored (or accept the page-local class until a server-side render helper is added).

4. **CSS dead-code in `activity_center.html`**: `.obs-views`, `.obs-views-panel`, `.obs-chev` rules linger from the removed saved-views UI. Documented in the file as such — can be dropped in a dead-code sweep.

5. **Macro docstrings in `_components.html` reference an `interface-design:audit` skill that doesn't yet enforce the class-name vocabulary**. The macros emit specific class names (`.btn`, `.btn-primary`, `.ds-card`, `.tab-strip__item`, etc.); if a future refactor renames any of these in CSS without updating the macros, the rendered HTML silently becomes unstyled. _Suggestion: add a CI test (or extend `tests/test_design_system_contract.py`) that asserts every class the macros emit resolves to a CSS rule._ I included the shell loop for this in section 8 of `REFACTOR_PLAYBOOK.md`.

## Gaps — what's not unified yet

### Templates without macro adoption (13 page templates)

**Intentionally skipped per the playbook** (no realistic refactor candidates):
- `news.html`, `store_examples.html`, `catalog_table_detail.html`, `marketplace_format_guide.html` — no buttons.
- `admin_scheduler_runs.html` — no buttons; `.sched-table` already canonical via alias.
- `desktop_link.html` — only `.btn-google` special-case OAuth class.
- `setup_advanced.html` — only `.plugin-copy` buttons (system.md keeps `.btn-copy` family page-local).
- `admin_access.html` — only JS-generated `.bulk-btn` inside template literals.
- `install.html` — only JS-generated `.primary` buttons inside template literals.

**Deferred to dedicated PRs** (each playbook-flagged):
- `admin_server_config.html` (1462 lines, 0 canonical `.btn-*` buttons, entirely bespoke `.cfg-btn` chrome).
- `admin_corporate_memory.html` (3951 lines, bespoke `.btn-mandate`/`.btn-approve`/`.btn-reject`/`.btn-revoke` family — needs decision: promote or stay bespoke).
- `admin_tables.html` (5748 lines, 66 buttons in bespoke modals + inline edit panels).
- `dashboard.html` (1539 lines, heavy bespoke chrome — needs macro extensions for `setup-cta`, `terminal-block`, `notif-card`).

### Patterns the 5 macros don't model

| Pattern | Where it lives | Why macros don't cover it |
|---------|---------------|---------------------------|
| Navy-on-navy tabs with inline SVG icons + count badges | `.mp-tabs` (marketplace), `.stack-tabs` (catalog/memory) | `ds.tabs` only emits text labels; can't accept per-item caller blocks for rich body |
| Dark-surface segmented strips | `.os-tabs`, `.mode-tabs` (home_not_onboarded, install) | Same as above; also dark surface |
| Pill-shaped filter chips | `.pill` (marketplace, catalog, memory) | `ds.tabs` renders rectangular `.tab-strip__item`, not 999px-radius pills |
| Clickable KPI cards | `.obs-kpi` (activity_center, admin_sessions, admin_usage) | Rendered as `<button>` for native keyboard behavior; panel macro renders `<div>`/`<a>` |
| Hero search-row buttons | `.search-btn` / `.stack-hero__search-btn` (marketplace, catalog, corporate_memory) | Visually merged with search-card; canonical `.btn-primary` would re-introduce border + padding |
| Custom-accent info panels | `.mp-curator-block` (marketplace) | Custom flea-purple / mystack-slate accents outside the `--ds-accent-*` vocabulary |
| Dark-surface code chip / copy button | `.btn-copy*`, `.code-block` | system.md explicitly carves these out as page-local Catppuccin treatment |
| `.btn-required` amber-disabled | catalog_package_detail, memory_domain_detail | Not promoted to canonical yet (see Should Fix #1) |

### Recommended next direction

If the goal is to drive the brief's "one way to do each thing" rule all the way home:

1. **Extend the macros** to cover the tabs-with-rich-body case (`{% call ds.tabs() %}<svg>…</svg><span class="count">…</span>label{% endcall %}` per item). That unblocks `.mp-tabs`, `.stack-tabs`, `.os-tabs`.
2. **Promote `.btn-required` to canonical** — used by 2 pages, time to define it in style-custom.css.
3. **Decide `.btn-warning` fate** — single use in admin_marketplaces system-confirm; either add to canonical or replace.
4. **Then sweep admin_tables, admin_corporate_memory, admin_server_config, dashboard** — each as a separate PR. The playbook in `.design/design-system-unification/REFACTOR_PLAYBOOK.md` has the decision tree.
5. **Hex-literal sweep** across all refactored templates — swap raw `#fef3c7` / `#92400e` / etc. for `var(--ds-accent-warn-bg)` / `var(--ds-accent-warn-ink)` so a future palette tweak is a one-token edit.

## Accessibility

- **Focus rings**: all macro-rendered buttons inherit `.btn:focus-visible` (canonical 2px `--ds-primary` outline at 2px offset per `style-custom.css`). Preserved across the 40 templates.
- **Disabled anchors**: `ds.button(disabled=True, href=...)` renders `aria-disabled="true" tabindex="-1"`, which is an improvement over the previous bespoke pattern that only set `aria-disabled` (anchors stayed keyboard-focusable).
- **Aria-labelled landmarks**: profile sections preserve `<section aria-label="…">` via the `tag='section'` parameter on `ds.panel`. No landmark regression — `<details aria-label="…">` in profile.html stays a `<details>` element (carries `.ds-card` class explicitly).
- **Hover-only feedback**: refactored danger buttons now use the canonical "fills red on hover" treatment (system.md "committed to by hover"). Still keyboard-accessible via `:focus-visible`.
- **Icon-only close button** in `activity_center.html` carries `aria-label="Close"` via `attrs=` parameter (was inline before).

## What Works Well

- **Single source of truth for button shape.** Every refactored page renders its primary/secondary/danger buttons through the same macro. A future palette tweak — say green-primary → teal-primary — is a one-token edit in `design-tokens.css` instead of a grep job.
- **CSS dead-weight removed.** ~150 lines of duplicated button styling deleted across `.obs-btn` (3 files), `.welcome-btn` (2 files), `.gp-btn`, `.submit-btn`/`.reset-btn`, `.guide-cta a`, `.mp-actions .btn`, `.delete` family (2 files), `.pkg-hero__actions .btn`, `.pkg-section`, `.rcp-section`, `.section-card`. Each conversion was paired with a doc-comment explaining what came out.
- **Test-pin discipline held.** The 3 string assertions that needed updating were updated alongside the markup change, never broken silently. The test diffs are self-documenting (`# Renders via ds.button which emits href before class`).
- **Macro contracts stayed minimal.** The macro file grew exactly one parameter (`tag=None` on panel, added in the previous branch's work and carried over here) — every other gap was met by `klass=` + `attrs=` escape hatches rather than per-page macro variants. Spec from system.md ("If you need a class that isn't here, add it to system.md first, then expose it here as a parameter") held in practice.
- **Doc-comments at conversion sites.** Every refactored file's `{% import %}` line carries a 3-5 line comment naming what was converted and what stays page-local. A future maintainer touching admin_marketplaces or catalog.html can read the file top to see why the bespoke `.mp-tabs` / `.stack-tabs` / `.pill` aren't macro calls.
- **Cumulative test confidence.** Every batch ran a focused test sweep (typically 30-200 tests) and all came back green. The 3 test pin updates were the only test-side changes — no hidden test churn.
- **Playbook-driven execution.** The REFACTOR_PLAYBOOK.md was followed verbatim across all 10 batches. The 8-step per-page workflow, the convert-or-skip decision tree, and the patterns-that-don't-fit list all proved load-bearing — the playbook is the durable artefact, not just the refactored templates.

---

**Bottom line**: the refactor is a real step toward the brief, not a cosmetic dusting. 40 templates now read off a single button vocabulary; 4 explicit gaps (`admin_tables`, `admin_corporate_memory`, `dashboard`, `admin_server_config`) need follow-up PRs with the macro vocabulary possibly extended. The brief's "borders-dominant, calm" direction is preserved; the visible style deltas (button color/padding/hover shifts) are all in the direction of *more* canonical, not less. Tests pass cleanly, doc-comments make the unrefactorable parts auditable, and the playbook means the next-session refactor of the heavy files is a 2-3 hour job per PR rather than a re-discovery exercise.
