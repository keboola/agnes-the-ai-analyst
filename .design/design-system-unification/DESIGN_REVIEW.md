# Design Review: Design System Unification

Reviewed against: `DESIGN_BRIEF.md`
Philosophy: Calm editorial dashboard — borders-dominant, shadows reserved, green primary + navy hero + white surfaces.
Date: 2026-05-21
Mode: Code-only (visual capture skipped at user request — Chrome MCP unpaired)

## Screenshots Captured

None. Visual capture was skipped explicitly — the refactor is markup-and-style-only with a stated "no visible regression" goal, and the user opted to defer screenshot capture. Style deltas that *would* appear in a diff are catalogued under **Style Deltas Introduced** below so they can be spot-checked in a browser later.

To produce screenshots later: start the dev server (`uvicorn app.main:app --host 127.0.0.1 --port 8000`), pair Chrome MCP, and capture the 11 priority pages at 1280/768/375 widths. The brief lives at `.design/design-system-unification/DESIGN_BRIEF.md`; screenshots should land in `.design/design-system-unification/screenshots/`.

## Summary

The unification landed substantially: ~30 templates now route their `<button>` and `<a class="btn">` markup through 5 shared partials (`button`, `primary_nav`, `tabs`, `table`, `panel`) defined in `app/web/templates/_components.html`. Around 200 button instances were converted, 80+ lines of redundant page-local button CSS were dropped, and three test pins were loosened to accept the macro's attribute-order output. The brief's "one way to do each thing" rule now holds for the simple cases — almost every Cancel/Save/Delete button on the modified pages renders through the same vocabulary.

Three meaningful gaps remain. **Dashboard, admin_corporate_memory, and admin_tables (~10,700 lines combined) are explicitly unrefactored** and continue to use bespoke button vocabularies (`.btn-setup`, `.btn-mandate`, `.btn-approve`, `.cfg-btn`, etc.). These need dedicated PRs. Several rich-content patterns the macros can't model — `.mp-tabs` with inline SVG icons + count badges, `.os-tabs` / `.mode-tabs` on dark surfaces, `.pill` filter chips, the `.btn-required` amber-disabled state — remain page-local with inline doc-comments explaining why.

## What Was Built

### Macros (`app/web/templates/_components.html`, 311 lines)

| Macro | Surface area | Recently added |
|-------|--------------|----------------|
| `button(label, variant, size, icon_only, type, href, id, klass, attrs, disabled)` | `<button>` / `<a>` over the canonical `.btn` family | — |
| `primary_nav(items, brand_label, brand_logo_svg, brand_href, subtitle, right_extra)` | `<header class="app-header">` mirror of `_app_header.html` | — |
| `tabs(items, aria_label, kind='link'|'button')` | `.tab-strip` light-surface tabs only | — |
| `table(columns, rows, dense, zebra, caption, empty_msg, klass)` | `.ds-table` data-driven render | — |
| `panel(title, accent, clickable, href, klass, attrs, tag=None)` | `.ds-card` family | Added `tag='section'` so profile sections stay aria-labelled landmarks (turn 5) |

### Base layout (`base_ds.html`, 119 lines)

Opt-in alternative to `base.html`. Auto-imports `_components.html` as `ds`, keeps the same CSS load order (`style-custom.css` → `design-tokens.css` → `components.css` → `stack_card.css`) so the legacy `--primary` → `--ds-primary` shim still fires. No page has been migrated to it yet — it's a foothold for future work.

### Templates refactored (~30)

40 templates now import `_components.html` (per `grep -l "_components.html"`). Macro call density per file:

- **Top adopters**: `admin_marketplaces.html` (12), `marketplace_plugin_detail.html` (11), `marketplace_item_detail.html` (11), `catalog_package_detail.html` (11), `admin_users.html` (10), `catalog.html` (8), `activity_center.html` (8).
- **Mid-tier (5-7 macros)**: `setup`, `home_onboarded`, `admin_groups`, `profile`, `marketplace_guide`, `admin_workspace_prompt`, `admin_welcome`, `admin_user_detail`, `admin_store_submission_detail`, `admin_group_detail`, `corporate_memory`, `admin_usage`.

## Conformance to Brief

### Tokens, not literals — partial

The brief calls out: *"A raw 12px or #2ea877 in the diff is a code smell unless it's defining a token."*

Reality: refactored pages still ship lots of raw hex (23 in `profile.html`, 17 in `setup.html`, 17 in `me_activity.html`). Most are chip-color rules (`.group-chip.is-admin { background: #fef3c7; color: #92400e; }`) or page-local accent colors (`.role-chip.is-core { background: rgba(16, 185, 129, 0.10); }`). These weren't in scope for the partials-first refactor, but they're the natural next pass — system.md's `--ds-accent-{info,success,warn,danger}-*` tokens already cover most of these cases.

### One way to do each thing — mostly holds

| Pattern | Status |
|---------|--------|
| Primary CTA | Single path: `ds.button(variant='primary')` everywhere refactored. |
| Destructive action | Single path: `ds.button(variant='danger')`. |
| Modal Cancel + confirm | Consistently `ds.button(variant='secondary')` + variant-typed confirm across `admin_groups`, `admin_users`, `admin_marketplaces`, `admin_welcome`, `admin_workspace_prompt`, `catalog.html`, `corporate_memory`. |
| Paginator (Prev/Next) | Consistently `ds.button(variant='secondary', size='sm')` on `admin_sessions`, `admin_usage`, `admin_user_detail`, `me_activity`, `activity_center`. |
| Owner-actions row on flea detail pages | `marketplace_item_detail` + `marketplace_plugin_detail` now share the same Edit/Archive/Hard-delete pattern via `ds.button(variant='secondary'|'danger', size='sm')`. |
| Bespoke amber "required" disabled state | Still bespoke `.btn-required` in `catalog_package_detail` + `memory_domain_detail` — no canonical match in the .btn family. Documented inline. |

### Borders-dominant, shadows reserved — preserved

The canonical `.ds-card` carries `--shadow-sm` (whisper); `ds.panel` adopters inherit this. Profile sections now get a subtle lift they didn't have before (was bg+border only) — see **Style Deltas**.

### Calm hover behaviour — mixed

`.ds-card[data-clickable]:hover` is neutral (border darkens to `--ds-text-muted`, `--shadow-md`, 1px lift). Adopted on `home_onboarded`'s quick-cards via `ds.panel(href=...)`. But the quick-card preserves a green-brand hover override (border-color: `--ds-primary` + greenish shadow) intentionally to keep `/home` recognisable. Conscious deviation; documented in `home_onboarded.html`.

## Style Deltas Introduced

These are the visible-in-screenshot-diff changes from the refactor. None breaks behaviour; all align with brief direction.

### Color shifts on primary buttons

- **`admin_store_submissions` Apply**: black fill → canonical green primary. Was custom `.submit-btn { background: var(--text); }`.
- **`admin_groups` + New group**: was page-local `.gp-btn.primary { background: var(--primary); }` (indigo before shim, green after) — now canonical `.btn-primary`. Visually identical post-shim, but no longer a separate vocabulary.
- **`admin_users` + Add user**, **`admin_welcome`/`admin_workspace_prompt` Save override**: same pattern as above.
- **`marketplace_guide` `.guide-cta` buttons**: padding shifts from page-local `9×16` to canonical `.btn-primary`/`.btn-secondary` default (~10×18, ~2px taller). Hover treatment shifts from "neutral border + green text" to canonical "darken bg" treatment.

### Hover treatment shifts on secondary buttons

- **`.obs-btn` paginators** (activity_center, admin_sessions, admin_usage, admin_session_detail): border-radius `6px` → canonical `8px`; hover fill shifts from `--border-light` to `--ds-surface-dim`. Conceptually identical, slightly different exact value.
- **`.welcome-btn`** (admin_welcome, admin_workspace_prompt): padding `8×16` → `10×18`. Slightly taller buttons.
- **`marketplace_item_detail` / `marketplace_plugin_detail` `.delete` buttons**: hover shifts from "soft red wash" (plugin) / no hover (item) to canonical `.btn-danger:hover` "fills red on hover". System.md explicit: *"Destructive — committed to by hover, not announced by default."* Aligned with brief.

### Card / panel surface deltas

- **`profile.html` `.section-card`** + **`catalog_package_detail` `.pkg-section`** + **`catalog_recipe_detail` `.rcp-section`**: now carry `.ds-card`'s `--shadow-sm` (was bg+border only). Very subtle lift.
- **`home_onboarded.html` `.quick-card`**: radius unchanged (12px override preserved), hover green-brand preserved via `data-clickable:hover` override.

## Must Fix

None. No broken functionality, no test regressions (302+ tests pass across the touched surfaces), no accessibility regressions detected in code review.

## Should Fix

1. **`admin_user_detail.html` paginator buttons**: rendered as `ds.button(variant='secondary', size='sm')` but the previous bespoke `.btn` styling had no size suffix. Visually they go from "standard `.btn`" (8×16-ish) to `.btn-sm` (6×12). May read smaller than expected next to the rest of the page. Decide if `size='sm'` is right or if these should be standard. _Fix: drop `size='sm'` if the previous look needs to be preserved exactly; verify in a browser._

2. **`memory_domain_detail.html` `.btn-required` state**: kept page-local as a doc-comment exception, but it's used on both `memory_domain_detail` and `catalog_package_detail` — that's the start of a vocabulary. _Suggestion: promote to `.btn-required` in style-custom.css with a canonical amber-on-amber treatment, or absorb into a `.btn-secondary[disabled][data-required="true"]` variant._

3. **`store_edit.html` Save button disabled binding**: `disabled=(pending_sub is not none and pending_sub)` is more verbose than the original `{% if pending_sub %}disabled{% endif %}`. Functionally equivalent under Jinja's truthiness rules but worth a smoke test on the pending-submission path. _Fix: render with `pending_sub` defined / undefined and confirm the `disabled` attribute appears correctly._

4. **`login_email.html` "Sign in with Google" button**: converted to `ds.button(variant='secondary', klass='btn-block')` with the rich-SVG body via `{% call %}`. The previous `.btn-secondary` class was already canonical — this conversion adds nothing functional. _Suggestion: revert if the diff is pure churn, or leave as the documentation pattern for "this is how you do icon-buttons via the macro"._

## Could Improve

1. **Hex-literal cleanup pass**. ~57 raw hex values still live in the 32 modified templates (`profile` 23, `setup` 17, `me_activity` 17). Most are chip colors that have `--ds-accent-*` equivalents. Pure follow-up — out of the partials-first scope but the natural next pass.

2. **Legacy `var(--primary)` → `var(--ds-primary)` sweep**. The design-token shim transparently redirects, but explicit `--ds-*` references make the code self-documenting and remove the dependency on the shim. Templates like `corporate_memory`, `me_activity`, `memory_domain_detail`, `dashboard` still reference legacy tokens heavily.

3. **`admin_marketplaces` doc-comment claims 6 modal action sets**; actual count is 5 modals (create / edit / sync / details / confirm-delete) + 1 system-confirm. Minor — update the comment.

4. **Three test pins were loosened** to accept the macro's attribute-order output (`tests/test_web_marketplace_guide.py` lines 62, 106, 152). Worth a separate "test-pin hygiene" pass that converts every exact-string template assertion to semantic matching (regex on class + href + text). Would unblock faster future refactors.

5. **`base_ds.html` adoption is zero**. The opt-in layout was built in turn 1 but no page has migrated to it. The migration requires extracting the inline JS from `base.html` (undo toast, modal-Esc, cmd palette, admin shortcuts) into a `_app_scripts.html` partial first. Tracked in the `base_ds.html` doc-comment.

## Gaps — what's not unified yet

### Templates with literal `.btn .btn-*` classes but no `ds.button` adoption

- `_profile_tokens.html` — partial, out of scope.
- `_profile_troubleshooting.html` — partial, out of scope.
- `admin_tables.html` (5748 lines, 66 buttons) — **needs dedicated PR**.

### Templates with bespoke button vocabularies untouched

- `admin_corporate_memory.html` (3951 lines): `.btn-mandate`, `.btn-approve`, `.btn-reject`, `.btn-revoke` — entire bespoke variant family. **Needs dedicated PR.**
- `admin_tables.html`: same scale, more variants.
- `dashboard.html` (1539 lines): `.btn-setup`, `.notif-link`, `.notif-unlink`, `.btn-copy-term`, `.btn-register` — heavy custom chrome around the terminal mock + setup CTA + notification cards. **Needs dedicated PR**, possibly with macro extensions (an `setup_cta` or `terminal_block` partial).
- `admin_server_config.html` (1462 lines): `.cfg-btn` family + zero canonical buttons. Page-specific config-editor chrome. **Needs dedicated PR.**
- `admin_access.html` (862 lines): `.bulk-btn` actions are JS-generated inside template literals — not reachable from Jinja.
- `install.html` (1066 lines): `.primary` action buttons are JS-generated inside template literals.

### Patterns the 5 macros don't model

| Pattern | Where it lives | Why macros don't cover it |
|---------|---------------|---------------------------|
| Navy-on-navy tabs with inline SVG icons + count badges | `.mp-tabs` (marketplace.html), `.stack-tabs` (catalog/memory) | `ds.tabs` only emits text labels; can't accept per-item caller blocks for rich body |
| Dark-surface segmented strips | `.os-tabs`, `.mode-tabs` (home_not_onboarded, install) | Same as above; also dark surface |
| Pill-shaped filter chips | `.pill` (marketplace.html, catalog.html, corporate_memory.html) | `ds.tabs` renders rectangular `.tab-strip__item`, not 999px-radius pills |
| Clickable KPI cards | `.obs-kpi` (activity_center, admin_sessions, admin_usage) | Rendered as `<button>` for native keyboard behavior; `ds.panel` renders `<div>`/`<a>` |
| Hero search-row buttons | `.search-btn` / `.stack-hero__search-btn` (marketplace, catalog, corporate_memory) | Bespoke styling visually merged with the search-card; canonical `.btn-primary` would re-introduce border + padding |
| Custom-accent info panels | `.mp-curator-block` (marketplace) | Custom flea-purple / mystack-slate accents outside the `--ds-accent-*` vocabulary |
| Dark-surface code chip / copy button | `.btn-copy*`, `.code-block` | system.md explicitly carves these out as page-local Catppuccin treatment |

### Recommended next direction

If the goal is to drive the brief's "one way to do each thing" rule all the way home:

1. **Extend the macros** to cover the tabs-with-rich-body case (`ds.tabs(items=[{label, icon_html, count}])` or a `{% call %}` block per item). That unblocks `.mp-tabs`, `.stack-tabs`, `.os-tabs`.
2. **Add a sixth canonical button variant**: `.btn-required` (amber-disabled), used on `memory_domain_detail` + `catalog_package_detail`. Promote out of "page-local exception" status.
3. **Then sweep admin_tables, admin_corporate_memory, admin_server_config, dashboard, install** — each as a separate PR.

## Accessibility

- **Focus rings**: all macro-rendered buttons inherit `.btn:focus-visible` (canonical 2px `--ds-primary` outline at 2px offset per `style-custom.css`). Preserved.
- **Disabled anchors**: `ds.button(disabled=True, href=...)` renders `aria-disabled="true" tabindex="-1"`, which is an improvement over the previous bespoke pattern that only set `aria-disabled` (anchors stayed focusable).
- **Aria-labelled landmarks**: profile sections preserve `<section aria-label="…">` via the new `tag='section'` parameter on `ds.panel`. No landmark regression.
- **Hover-only feedback**: refactored danger buttons now use the canonical "fills red on hover" treatment (system.md "committed to by hover"). Still keyboard-accessible.

## What Works Well

- **Single source of truth for button shape**. Every refactored page renders its primary/secondary/danger buttons through the same macro. A future palette tweak — say green-primary → teal-primary — is a one-token edit in `design-tokens.css` instead of a grep job.
- **CSS dead-weight removed**. ~80 lines of duplicated button styling deleted across `.obs-btn`, `.welcome-btn`, `.gp-btn`, `.submit-btn`, `.reset-btn`, `.guide-cta a`, `.mp-actions .btn`, `.delete` family, `.pkg-hero__actions .btn`, etc. Each conversion was paired with a doc-comment explaining what came out.
- **Test-pin discipline held**. The three string assertions that needed updating were updated alongside the markup change, never broken silently. The test diffs are self-documenting (`# Renders via ds.button which emits href before class`).
- **Macro contracts stayed minimal**. The `panel` macro grew exactly one parameter (`tag=None`) across 8 turns — every other gap was met by `klass=` + `attrs=` escape hatches rather than a per-page macro variant. Spec from system.md ("If you need a class that isn't here, add it to system.md first, then expose it here as a parameter") held in practice.
- **Doc-comments at conversion sites**. Every refactored file's `{% import %}` line carries a 3-5 line comment naming what was converted and what stays page-local. A future maintainer touching admin_marketplaces or catalog.html can read the file top to see why the bespoke `.mp-tabs` / `.stack-tabs` / `.pill` aren't macro calls.
- **Tests pass cleanly**: 302+ tests across web-UI / design-contract / admin / marketplace / catalog / memory pass. The one Windows-only failure (`test_filename_with_bundle_sentinel_is_escaped` trying to `mkdir('<')`) predates this work.

---

**Bottom line**: the refactor is a real step toward the brief, not a cosmetic dusting. ~30 templates now read off a single button vocabulary; 3 explicit gaps (`admin_tables`, `admin_corporate_memory`, `dashboard`) need follow-up PRs with the macro vocabulary possibly extended. The brief's "borders-dominant, calm" direction is preserved; the visible style deltas (button color/padding/hover shifts) are all in the direction of *more* canonical, not less.
