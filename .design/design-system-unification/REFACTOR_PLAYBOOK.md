# Template Refactor Playbook

Step-by-step instructions for converting `app/web/templates/*.html` to use the shared partials in `_components.html` and the design tokens documented in `.interface-design/system.md`.

This playbook is the distillation of ~30 page refactors across 8 prior iterations. Follow it page-by-page, ship small PRs, and the giant admin files become tractable.

---

## 0. Prerequisites

Before you start, confirm these files exist (they're the inputs the refactor depends on — they don't change during the work):

| File | Purpose |
|------|---------|
| `app/web/templates/_components.html` | The 5 macros: `button`, `primary_nav`, `tabs`, `table`, `panel` |
| `app/web/templates/base_ds.html` | Canonical design-system base layout — most pages now `{% extends %}` it (or `base_page.html`); it **auto-imports `ds`**, so the Step-2 import below is redundant there (see #367/#482) |
| `.interface-design/system.md` | The contract — class names, token names, accent vocabulary |
| `app/web/static/css/design-tokens.css` | All `--ds-*` tokens + accent vocabulary + focus-outline + text hierarchy |
| `app/web/static/style-custom.css` | Canonical `.btn-*` family, `.ds-card` family, `.ds-table` family + 15 legacy aliases, `.tab-strip`, `.badge--*`, `.code-inline` |

If a `.ds-card` rule is missing from `style-custom.css`, every `ds.panel(...)` call you write emits a class the CSS doesn't style. Verify with:

```bash
grep -E "^\.(ds-card|ds-table|btn--icon|badge--info)" app/web/static/style-custom.css | head -5
```

You should see at least 5 hits. If not, the CSS layer isn't set up — fix that before touching templates.

---

## 1. Pick a page

Start small. Order of operations across the codebase:

1. **Auth pages first** (login, password_reset, password_setup, login_magic_link, login_magic_link_sent, login_email, error, desktop_link) — tiny files, no test pins, easy wins to validate your loop.
2. **Setup wizards** (setup.html, setup_advanced.html).
3. **User pages** (profile.html, me_activity.html, corporate_memory.html, memory_domain_detail.html).
4. **Store flows** (store_edit.html, store_upload.html, store_examples.html).
5. **Catalog** (catalog.html, catalog_package_detail.html, catalog_recipe_detail.html, catalog_table_detail.html).
6. **Marketplace** (marketplace.html, marketplace_guide.html, marketplace_item_detail.html, marketplace_plugin_detail.html, marketplace_format_guide.html).
7. **Activity center** (activity_center.html).
8. **Home** (home_onboarded.html, home_not_onboarded.html).
9. **Admin — small** (admin_scheduler_runs, admin_session_detail, admin_store_submissions, admin_groups, admin_sessions, admin_group_detail, admin_usage, admin_welcome, admin_workspace_prompt, admin_users, admin_store_submission_detail).
10. **Admin — medium** (admin_marketplaces, admin_user_detail, admin_access, admin_tokens, admin/news_editor).
11. **Admin — large** (admin_server_config 1462 lines).
12. **Admin — massive** (admin_corporate_memory 3951, admin_tables 5748) — each is a dedicated PR.

Skip these (no realistic refactor candidates):
- `news.html`, `store_examples.html`, `catalog_table_detail.html`, `marketplace_format_guide.html` — no buttons.
- `admin_scheduler_runs.html` — only `.sched-table` (already canonical via alias).
- `desktop_link.html` — only `.btn-google` (special-case OAuth class).
- All `_` partials — out of scope.

---

## 2. Per-page workflow (8 steps)

For each page, run this loop:

### Step 1 — Audit

Read the file end-to-end first (don't skip — context matters for the macro choices). Then grep for the conversion candidates:

```bash
grep -nE '<button|<a[^>]*class="btn|<section[^>]*class="(section|pkg|rcp|td)-|class="[a-z-]*-btn"' app/web/templates/SOMEPAGE.html
```

Categorise each match:

| Category | Action |
|----------|--------|
| `<button class="btn btn-primary">X</button>` | **Convert** → `ds.button` |
| `<a class="btn btn-secondary" href="...">X</a>` | **Convert** → `ds.button(href=...)` |
| `<button class="some-btn">X</button>` (bespoke) | **Convert** if it's structurally a primary/secondary/danger CTA; **skip** if styling is genuinely page-local (search hero buttons, terminal copy buttons, dark-surface buttons) |
| `<button class="btn-copy">Copy</button>` | **Skip** — system.md keeps `.btn-copy` family page-local for dark surfaces |
| Buttons in a JS string template, assigned to a node | **Skip** — Jinja can't reach JS strings |
| `<section class="section-card aria-label="...">` | **Convert** to `ds.panel(tag='section', ...)` |
| `<div class="...-card">` (white surface + border + radius) | **Convert** to `ds.panel(...)` if it's a panel; skip if it's a bespoke chrome (hero, sidebar) |
| `<button class="some-tab">` (tab pattern) | **Convert** to `ds.tabs` ONLY if items are plain text. If items carry inline SVG icons + count badges, **skip** — the macro doesn't model rich body yet |
| `<table class="data-table">` etc. | **Already canonical** via alias — no conversion needed |

### Step 2 — Add the macro import

At the very top of `{% block content %}`, before any other markup (**skip this import on `base_ds.html` / `base_page.html` pages — they auto-import `ds`**; keep it only on the remaining legacy `base.html` pages):

```jinja
{% block content %}
{# Design-system component macros — Cancel/Save buttons and section
   panels render via `ds.button` and `ds.panel`. Bespoke `.some-chrome`
   patterns (search bar, KPI cards, etc.) stay page-local. #}
{% import "_components.html" as ds %}
```

Always include a 3-5 line doc-comment naming **what converts** and **what stays page-local**. Future maintainers will read this before re-opening the file.

### Step 3 — Convert buttons

#### Simple button

```jinja
{# Before #}
<button class="btn btn-primary" id="save-btn">Save</button>

{# After #}
{{ ds.button('Save', variant='primary', id='save-btn') }}
```

#### Form submit

```jinja
{# Before #}
<button type="submit" class="btn btn-primary btn-block">Sign In</button>

{# After #}
{{ ds.button('Sign In', variant='primary', type='submit', klass='btn-block') }}
```

#### Anchor styled as button

```jinja
{# Before #}
<a href="/foo" class="btn btn-secondary">Cancel</a>

{# After #}
{{ ds.button('Cancel', variant='secondary', href='/foo') }}
```

#### Disabled state

```jinja
{# Before #}
<button class="btn btn-primary" disabled>In flight…</button>

{# After #}
{{ ds.button('In flight…', variant='primary', disabled=True) }}
```

When the source had a `disabled` attribute, pass `disabled=True`. The macro adds `aria-disabled="true" tabindex="-1"` for anchors so they're not keyboard-focusable.

#### Modal Cancel with `data-close-modal`

```jinja
{# Before #}
<button class="btn btn-secondary" data-close-modal="confirm-modal">Cancel</button>

{# After #}
{{ ds.button('Cancel', variant='secondary',
             attrs='data-close-modal="confirm-modal"') }}
```

`attrs` is a raw-string escape hatch for any HTML attribute the macro doesn't model directly — `data-*`, `title`, `onclick`, inline `style`, `hidden`, etc.

#### Inline onclick + title

```jinja
{# Before #}
<button class="btn btn-danger" id="del-btn" type="button"
        title="Hard delete">Delete</button>

{# After #}
{{ ds.button('Delete', variant='danger', type='button', id='del-btn',
             attrs='title="Hard delete"') }}
```

#### Icon-only button (close, copy, etc.)

```jinja
{# Before #}
<button class="obs-btn" id="close-btn" type="button" aria-label="Close">✕</button>

{# After #}
{% call ds.button(variant='ghost', size='sm', icon_only=True,
                  type='button', id='close-btn',
                  attrs='aria-label="Close"') %}✕{% endcall %}
```

Use `{% call %}` form when the button body is rich content (icon glyph, SVG, span). `icon_only=True` adds the `.btn--icon` modifier which squares the button.

#### Button with icon + text

```jinja
{# Before #}
<a href="/auth/google" class="btn btn-secondary btn-block">
    <svg width="20" height="20">…</svg>
    Sign in with Google
</a>

{# After #}
{% call ds.button(variant='secondary', href='/auth/google',
                  klass='btn-block') %}
    <svg width="20" height="20">…</svg>
    Sign in with Google
{% endcall %}
```

#### Button with a `<span>` chip inside

```jinja
{# Before #}
<button class="btn btn-secondary" type="button">
    + New Recipe <span class="admin-only-hint">admin-only</span>
</button>

{# After #}
{% call ds.button(variant='secondary', type='button') %}
    + New Recipe <span class="admin-only-hint">admin-only</span>
{% endcall %}
```

### Step 4 — Convert panels / cards

The `ds.panel` macro renders `<div class="ds-card">` by default. Override the tag with `tag='section'` (for landmarks) or `tag='details'` if you need a disclosure widget (not yet supported — use a manual `<details class="ds-card section-card">`).

#### Section card with h3 title

```jinja
{# Before #}
<section class="section-card" aria-label="Account details">
    <h3>Account</h3>
    <div class="account-grid">...</div>
</section>

{# After #}
{% call ds.panel(title='Account', tag='section',
                 klass='section-card',
                 attrs='aria-label="Account details"') %}
    <div class="account-grid">...</div>
{% endcall %}
```

The macro renders `<h3 class="ds-card__title">Account</h3>`. Drop the original `<h3>` from the body.

#### Section card with h2 (eyebrow-style heading)

If the page's eyebrow heading is `<h2>` (not `<h3>`) and styled via a page-local `.section-card h2` rule, **don't use the title parameter** — keep the `<h2>` inside the body:

```jinja
{% call ds.panel(tag='section', klass='rcp-section') %}
    <h2>Query template</h2>
    <pre>...</pre>
{% endcall %}
```

This preserves the page-local h2 styling exactly.

#### Clickable card (link card)

```jinja
{# Before #}
<a class="quick-card" href="/dashboard">
    <span class="ico">📊</span>
    <div class="ttl">Dashboard</div>
    <div class="desc">Sync state, ...</div>
</a>

{# After #}
{% call ds.panel(href='/dashboard', klass='quick-card') %}
    <span class="ico">📊</span>
    <div class="ttl">Dashboard</div>
    <div class="desc">Sync state, ...</div>
{% endcall %}
```

The macro renders `<a class="ds-card quick-card" href="/dashboard" data-clickable>...</a>`. `data-clickable` enables the hover lift.

#### Accent card (info/warn/success/danger)

```jinja
{# Before #}
<div class="card-error">
    <strong>Quarantined:</strong> ...
</div>

{# After #}
{% call ds.panel(accent='danger') %}
    <strong>Quarantined:</strong> ...
{% endcall %}
```

Or for warn:

```jinja
{% call ds.panel(accent='warn', title='Cron paused') %}
    The scheduler hasn't run since 12:42.
{% endcall %}
```

### Step 5 — Drop the page-local CSS that's now redundant

After converting a button group, the page-local CSS that previously styled the bespoke class (`.welcome-btn`, `.obs-btn`, `.gp-btn.primary`, etc.) becomes dead weight. Drop it.

**Rule of thumb**: if a CSS rule sets the same things the canonical `.btn-*` family sets (background, color, border, padding, border-radius, font-weight), and the markup no longer uses that class, **delete the rule**.

```css
/* Before: page-local .gp-btn family duplicates .btn-* */
.gp-btn {
    padding: 8px 14px; border-radius: 8px;
    font-size: 13px; font-weight: 500;
    border: 1px solid var(--border); background: var(--surface);
}
.gp-btn:hover { background: var(--border-light); }
.gp-btn.primary {
    background: var(--primary); color: #fff; border-color: var(--primary);
}

/* After — replace the whole block with a comment */
/* `.gp-btn` / `.gp-btn.primary` rules deleted — toolbar button now
   renders via `ds.button(variant='primary')` which carries the
   canonical .btn family from style-custom.css. */
```

**Keep** these (don't drop):

- Container layout for the group (`display: flex; gap; margin-bottom; justify-content`) — that's layout, not button styling.
- Page-local hover overrides you want to preserve (e.g., `home_onboarded`'s green-brand hover on `.quick-card[data-clickable]:hover`).
- Page-local CSS for child elements (`.quick-card .ico`, `.section-card h3` — layout/typography of stuff inside the card, not the card itself).

#### CSS-trim pattern for section cards

```css
/* Before */
.section-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px 24px;
    margin-bottom: 16px;
}

/* After — .ds-card supplies bg/border/shadow; only the diffs stay */
.section-card {
    border-radius: var(--radius-xl);  /* 12 overrides .ds-card's 8 */
    padding: var(--space-5) var(--space-6);
    margin-bottom: var(--space-4);
}
```

### Step 6 — Replace literals with tokens (when convenient)

When you're already in the file, swap exact-match literals:

| Literal | Token |
|---------|-------|
| `8px` | `var(--space-2)` |
| `12px` | `var(--space-3)` |
| `16px` | `var(--space-4)` |
| `24px` | `var(--space-6)` |
| `40px` | `var(--space-9)` |
| `8px` (radius) | `var(--radius-lg)` |
| `12px` (radius) | `var(--radius-xl)` |
| `16px` (radius) | `var(--radius-2xl)` |
| `10px` font-size | `var(--text-xs)` |
| `12px` font-size | `var(--text-sm)` |
| `14px` font-size | `var(--text-base)` |
| `font-weight: 500` | `var(--font-medium)` |
| `font-weight: 600` | `var(--font-semibold)` |
| `font-weight: 700` | `var(--font-bold)` |
| `'Inter', system-ui, ...` | `var(--ds-font)` |
| `ui-monospace, Menlo, ...` | `var(--ds-font-mono)` |

**Don't force a swap** if the literal doesn't match exactly (e.g., `18px` has no token — leave as literal). Forcing `var(--space-5)` (20px) in place of `18px` changes the visual.

### Step 7 — Handle test pins

After your edits, run the focused test suite:

```bash
.venv/Scripts/python.exe -m pytest tests/test_web_ui.py tests/test_design_system_contract.py tests/test_<page-name>.py --tb=short -q
```

If a test fails on an exact-string assertion like:

```python
assert '<a class="btn btn-secondary" data-actions-for="curated" href="..."' in body
```

The macro renders `<a href="..." class="btn btn-secondary" data-actions-for="curated"` (href first). Update the test to be order-agnostic:

```python
import re
m = re.search(
    r'<a\b[^>]*\bclass="btn btn-secondary[^"]*"[^>]*>'
    r'\s*Submit a skill or plugin\s*</a>',
    body,
)
assert m
html = m.group(0)
assert 'data-actions-for="curated"' in html
assert 'href="/marketplace/guide/curated"' in html
```

Or if it pinned `class="primary" href="..."` (legacy single-class), update to match the macro output:

```python
assert '<a href="/store/new" class="btn btn-primary"' in body
```

Document the change in the test with a comment explaining the macro emits href before class.

### Step 8 — Render-check + verify

Spin up the dev server and visit the page in your browser. Look for:

- **Buttons render with the right colors** — no unstyled white-on-white buttons (means a class is missing from CSS).
- **Padding looks right** — canonical `.btn-primary` is 10×20; if your previous bespoke button was 8×16, you'll see a ~2px shift. Acceptable.
- **Hover behavior matches the page's character** — neutral darken (secondary/ghost) or red-fill (danger) or `--ds-surface-dim` fill.
- **Focus ring appears** on keyboard tab — 2px green outline at 2px offset.
- **Disabled buttons** don't respond to clicks and have reduced opacity.

```bash
.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
# In another shell, hit each refactored page
```

Commit with a focused message:

```
feat(web): refactor templates/<page>.html to shared button + panel macros

- Convert N modal/toolbar buttons to ds.button(variant=...)
- Drop dead .X-btn / .Y-btn CSS rules (now from canonical .btn family)
- Page-local chrome (.foo, .bar) stays — bespoke styling, doc-commented
```

---

## 3. Decision tree — convert or skip?

For each `<button>` / `<a class="btn-*">` you encounter:

```
Is it already using a canonical .btn-* class?
├─ Yes → CONVERT to ds.button. Same class output, less repetition.
└─ No → Is the rendered styling structurally a primary/secondary/danger/ghost CTA?
   ├─ Yes → CONVERT. Drop the bespoke CSS rule. Accept the canonical look.
   └─ No → Is it bespoke for a specific reason?
      ├─ Dark surface (terminal mock, navy hero) → SKIP. Note in import comment.
      ├─ Pill-shaped filter chip → SKIP. ds.tabs renders rectangular.
      ├─ KPI clickable card → SKIP. Macro renders div/anchor, not <button>.
      ├─ Copy button (system.md keeps page-local) → SKIP.
      ├─ Generated inside a JS template literal → SKIP. Can't reach.
      └─ Other → ASK. Document the call in the import comment.
```

For each `<div class="some-card">` you encounter:

```
Is it functionally a panel (white surface + border + radius + padding)?
├─ Yes → Does it have a clear h3/h2 heading?
│  ├─ Yes, h3 heading → CONVERT with title= parameter
│  └─ Yes, h2 (eyebrow style) → CONVERT without title, keep h2 in body
└─ No → Is it bespoke chrome (hero, sidebar, search row, KPI tile)?
   ├─ Yes → SKIP. Note in import comment.
   └─ No → Reconsider. Most divs aren't panels.
```

---

## 4. Common anti-patterns

### Don't pass HTML in the label parameter

```jinja
{# WRONG — Jinja escapes HTML in label. The <span> renders as text. #}
{{ ds.button('Click me <span>x</span>', variant='primary') }}

{# RIGHT — use {% call %} form for rich body #}
{% call ds.button(variant='primary') %}Click me <span>x</span>{% endcall %}
```

### Don't double up classes

```jinja
{# WRONG — passes btn-primary as klass, plus variant adds it again #}
{{ ds.button('Save', variant='primary', klass='btn-primary') }}
{# Renders: <button class="btn btn-primary btn-primary"> #}

{# RIGHT — variant param is the source of truth #}
{{ ds.button('Save', variant='primary') }}
```

### Don't forget type='button' on non-submit buttons

```jinja
{# WRONG — default browser behavior submits the surrounding form #}
{{ ds.button('Cancel', variant='secondary',
             attrs='data-close-modal="my-modal"') }}

{# RIGHT — explicit type prevents accidental form submit #}
{{ ds.button('Cancel', variant='secondary', type='button',
             attrs='data-close-modal="my-modal"') }}
```

The macro defaults to `type='button'`, so this only matters if you pass something else like `type='submit'` intentionally for the form's action.

### Don't refactor JS-generated buttons

If a button's markup is built inside a `<script>` block as a string and then assigned to a DOM node, Jinja never sees that string. Leave it. Either expose a render helper that the macro can generate server-side, or keep it page-local with a note in the import comment.

### Don't drop page-local layout CSS

```css
/* Before */
.foo-actions {
    display: flex;
    gap: 10px;
    margin: 0 0 16px 0;
    justify-content: flex-end;
}
.foo-actions a, .foo-actions button {
    padding: 6px 14px; border-radius: 8px;
    background: white; border: 1px solid var(--border);
    color: var(--text);
}
```

After converting the buttons inside `.foo-actions` to `ds.button(variant='secondary')`:

```css
/* CORRECT — keep the layout, drop the button styling */
.foo-actions {
    display: flex;
    gap: var(--space-2);
    margin: 0 0 var(--space-4) 0;
    justify-content: flex-end;
}
```

Don't drop the entire block — just the per-button rules that now come from `.btn-secondary`.

---

## 5. Patterns the macros DON'T model

When you see these, leave them and write a doc-comment in the macro-import block explaining why:

| Pattern | Where it appears | Why macros can't help |
|---------|------------------|------------------------|
| Tabs with inline SVG icons + count badges | `.mp-tabs` (marketplace), `.stack-tabs` (catalog, memory) | `ds.tabs` only accepts text labels; no caller block per item |
| Dark-surface segmented strips | `.os-tabs`, `.mode-tabs` (home_not_onboarded setup wizard) | Same as above; also navy surface |
| Pill-shaped filter chips | `.pill` (marketplace, catalog, memory) | `.tab-strip__item` is rectangular, not 999px-radius pills |
| Clickable KPI cards | `.obs-kpi` (activity_center, admin_sessions, admin_usage) | Rendered as `<button>` for native keyboard behavior; panel macro renders `<div>`/`<a>` |
| Hero search-row buttons | `.search-btn` / `.stack-hero__search-btn` | Visually merged with search-card; canonical `.btn-primary` re-introduces border + padding |
| Custom-accent info panels (purple/slate) | `.mp-curator-block` (marketplace) | Accent colors outside the `--ds-accent-*` vocabulary |
| Dark-surface copy buttons | `.btn-copy`, `.btn-copy-term` | system.md explicitly carves out for dark-surface Catppuccin treatment |
| `.btn-required` amber-disabled state | catalog_package_detail, memory_domain_detail | No canonical match yet — page-local exception |

If a pattern repeats across 2+ pages and doesn't fit any macro, it's a candidate for **macro extension** — document it in `system.md` as "should I extend the macros?" and tackle separately.

---

## 6. Per-page checklist (paste at the bottom of your PR description)

```markdown
## Refactor checklist — <page-name>.html

- [ ] Read the full file end-to-end
- [ ] Audited every `<button>` and `<a class="btn-*">` — list of N converted, M skipped (with reasons)
- [ ] Added `{% import "_components.html" as ds %}` at top of `{% block content %}`
- [ ] Doc-comment in import block names what converted + what stays page-local
- [ ] Buttons: simple ones use `{{ ds.button(...) }}`, rich-body ones use `{% call ds.button(...) %}...{% endcall %}`
- [ ] Panels: section-cards use `{% call ds.panel(tag='section', ...) %}`
- [ ] Dropped redundant CSS — kept container layout, dropped per-button styling
- [ ] Tokens: swapped exact-match literals (8/12/16/24/40px → space, 8/12/16px radius → radius-lg/xl/2xl)
- [ ] Tests: ran `pytest tests/test_<page-name>* tests/test_web_ui.py tests/test_design_system_contract.py`
- [ ] If a test pin broke: updated the assertion to be order-agnostic (regex) with a comment
- [ ] Rendered the page in a browser, verified buttons + hover + focus + disabled states
- [ ] Commit message names what changed in 1 sentence
```

---

## 7. Macro quick reference

### `ds.button(label, variant, size, icon_only, type, href, id, klass, attrs, disabled)`

| Parameter | Default | Notes |
|-----------|---------|-------|
| `label` | `''` | Plain-text label. For rich body use `{% call %}` form. |
| `variant` | `'primary'` | `'primary'` / `'secondary'` / `'ghost'` / `'danger'` |
| `size` | `None` | `'sm'` for 6×12 padding, omit for default 10×20 |
| `icon_only` | `False` | Adds `.btn--icon` square modifier |
| `type` | `'button'` | `'button'` / `'submit'` / `'reset'` |
| `href` | `None` | Pass to render `<a>` instead of `<button>` |
| `id` | `None` | Element id |
| `klass` | `''` | Extra class names appended (e.g., `'btn-block'`) |
| `attrs` | `''` | Raw HTML attributes (e.g., `'data-foo="bar" title="..."'`) |
| `disabled` | `False` | Adds `disabled` to button; `aria-disabled="true" tabindex="-1"` to anchors |

### `ds.panel(title, accent, clickable, href, klass, attrs, tag)`

| Parameter | Default | Notes |
|-----------|---------|-------|
| `title` | `None` | Renders as `<h3 class="ds-card__title">`. Omit to skip the title row. |
| `accent` | `None` | `'info'` / `'success'` / `'warn'` / `'danger'` — adds left-border accent |
| `clickable` | `False` | Adds `data-clickable` (hover lift). Implied true if `href` is set. |
| `href` | `None` | Pass to render `<a>` instead of `<div>` |
| `klass` | `''` | Extra class names appended |
| `attrs` | `''` | Raw HTML attributes |
| `tag` | `None` | Override default tag. Use `'section'` for aria-labelled landmarks. |

### `ds.tabs(items, aria_label, kind)`

| Parameter | Default | Notes |
|-----------|---------|-------|
| `items` | `[]` | List of dicts: `{label, href, active}` (link kind) or `{label, data_tab, active, id}` (button kind) |
| `aria_label` | `'Sections'` | Accessible label for the `<nav role="tablist">` |
| `kind` | `'link'` | `'link'` renders `<a>` items; `'button'` renders `<button>` for JS panel switchers |

### `ds.table(columns, rows, dense, zebra, caption, empty_msg, klass)`

| Parameter | Default | Notes |
|-----------|---------|-------|
| `columns` | `[]` | List of dicts: `{key, label, num=False, classes=''}` |
| `rows` | `[]` | List of dicts; each cell reads `row[col.key]` |
| `dense` | `False` | Reduced cell padding |
| `zebra` | `False` | Row striping |
| `caption` | `None` | Visible `<caption>` |
| `empty_msg` | `'No records.'` | Shown when `rows` is empty |
| `klass` | `''` | Extra class names |

Use this macro only for simple data tables. For tables with rich cells (links, badges, conditional rendering), keep manual `<table class="ds-table">` markup — the canonical class supplies the styling.

### `ds.primary_nav(items, brand_label, brand_logo_svg, brand_href, subtitle, right_extra)`

Used by `_app_header.html` only. Don't call directly from page templates.

---

## 8. When you finish a batch

Before opening a PR for a batch of refactored pages:

1. **Full focused test sweep**:
   ```bash
   .venv/Scripts/python.exe -m pytest tests/test_web_ui.py tests/test_design_system_contract.py tests/test_web_marketplace_guide.py tests/test_web_home_page.py tests/test_setup_cta_partial.py tests/test_admin_tokens_ui.py tests/test_admin_user_capabilities_ui.py tests/test_user_management.py --tb=short -q
   ```
2. **Class-coverage check** — every class your refactored pages emit should resolve to a CSS rule. A shell loop greps each unique class token from your refactored files against the stylesheet.
3. **Update CHANGELOG.md** with a single bullet under `[Unreleased] / Internal`.
4. **Open the PR** with a description that lists which pages were touched, how many buttons converted per page, and which patterns were left page-local (with reasons).

---

## 9. When the macros don't cover something

If you hit a pattern that 2+ pages need but the macros don't model:

1. **Pause the page refactor.**
2. **Document the pattern** in this playbook under "Patterns the macros DON'T model".
3. **Decide**: extend the macro (one-time cost, unblocks future work) or accept the page-local solution (faster now, more divergence later).
4. **If extending**: write the macro change in `_components.html`, add an example to its doc-comment, update `system.md` with the new contract, then return to the page refactor.

The macro changes that landed across prior iterations (panel `tag=`) all followed this loop.

---

## 10. The unrefactorable

You will hit pages where almost nothing converts. The big three:

- **`admin_corporate_memory.html`** (3951 lines) — has its own `.btn-mandate`, `.btn-approve`, `.btn-reject`, `.btn-revoke` variant family. Either promote these to system.md as canonical, or leave the page bespoke. Don't half-convert.
- **`admin_tables.html`** (5748 lines) — many bespoke modals + inline edit panels. Dedicated PR.
- **`dashboard.html`** (1539 lines) — `.btn-setup`, `.notif-link`, `.btn-copy-term`, `.btn-register`, terminal mock chrome, notification cards. Needs macro extensions before it's tractable.

For each, the right move is a one-off dedicated PR with macro extensions decided up-front. Don't shoehorn.

---

## Done

Refactor one page, ship the PR, repeat. After 5-6 pages the loop becomes muscle memory. After 20 pages the canonical vocabulary is the path of least resistance — new pages reach for `ds.button` reflexively.
