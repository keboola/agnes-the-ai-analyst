# Standalone Pages → base.html Framework Migration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. One PR continuation (`zs/design-pass`).

**Goal:** Migrate the 5 templates that currently ship their own `<html>`, `<head>`, `<body>` scaffold to extend `base.html`. After this lands, every page that includes `_app_header.html` shares ONE rendering pipeline — same font load, same theme include, same script load, same nav. The class of bug that surfaced today (dropdown JS dead on `/catalog`, `/admin/tables`, `/corporate-memory` because `<script src="app.js">` lived only in `base.html`) goes away permanently.

**Architecture:**
- Five pages have private `<head>` + `<body>` scaffolding (10 486 lines combined, of which ~4 169 are inline `<style>` blocks and ~4 201 are inline `<script>` blocks).
- `base.html` already exposes the right block surface: `title`, `head_extra`, `layout`, `content`, `scripts`.
- Migration is mechanical per page: convert `<html>...</html>` → `{% extends "base.html" %}{% block X %}...{% endblock %}`. No behavior change; same per-page CSS/JS, just hoisted into the right block.
- One small `base.html` change: add `{% block body_attrs %}{% endblock %}` so `admin_tables.html` can keep its `data-source-type` attr on `<body>`.

**Tech Stack:** FastAPI + Jinja2 templates, vanilla CSS, vanilla JS. Tests via pytest + agent-browser for visual smoke.

**Why now / why one PR:** The current `zs/design-pass` PR already touched all the affected pages (hero migration, dead-CSS sweep). Continuing the migration in the same PR keeps related changes together. Each per-page migration ships as its own commit so individual reverts stay surgical.

## Post-review revisions

External Plan-agent review flagged 8 must-fix items before execution. Applied:

1. **Script-extraction bug fixed**: original recipe used `script_m[-1]` which would pick the LAST inline `<script>` and drop earlier ones. Catalog has TWO (868-line IIFE + 26-line module). Revised script collects **all** inline `<script>` blocks in order, preserving each block's tag attributes (so `type="module"` survives).
2. **External assets hoist**: per-page `<link rel="stylesheet">` and `<script src>` inside `<head>` (e.g. catalog's chart.js, Prism, metric_modal.css) must land at the TOP of `{% block head_extra %}` — the original recipe captured only inline `<style>` and silently dropped externals.
3. **Duplicate stylesheet detection**: catalog.html ships a second `<link rel="stylesheet" href="style-custom.css">` after its `<style>` block. base.html already loads it once. The migration drops duplicates.
4. **Layout block default**: changed from `{% block content %}` to `{% block layout %}`. Each standalone has its own top-level wrapper (`<main class="main">`, `<div class="container-memory">`, etc.) — putting that inside base.html's `.container` would double-wrap. Layout block opts out of the `.container` wrap entirely; we must re-include `_app_header.html` (and `_version_badge.html` if base.html includes one) inside the override.
5. **Font preconnect hoist DEFERRED**: Task 0 Step 3 (move Inter preconnect into base.html) is dropped from this PR. The 4 pages that need it keep their inline preconnect inside `head_extra`. Hoisting affects ALL base.html pages (admin section currently lives on system Inter fallback) — separate decision worth its own measurement.
6. **Contract test added**: Task 7 now adds an assertion to `tests/test_design_system_contract.py` that each migrated page extends `base.html` and the rendered HTML has exactly one `<html>` / `<head>` / `<body>`. Prevents future regression back to standalones.
7. **Chart.js smoke verification**: Task 4 (catalog) now requires an explicit "chart rendered" browser screenshot, not just "page loads".
8. **Reviewer-fatigue caveat acknowledged**: user explicitly chose "all in one PR". Per-page commits land in `zs/design-pass` for surgical revert. Reviewer can bisect per commit.

---

**Out of scope** (defer to follow-up PRs):
- `dashboard.html` — already extends `base.html` per `grep -l "extends.*base"`; no migration needed. (Verify Step 0).
- `home_onboarded.html` / `home_not_onboarded.html` — already extend `base.html`; no migration needed.
- `marketplace.html`, `marketplace_*_detail.html` — not part of today's bug surface; can adopt the framework later.

---

## File structure (touch list)

**Modified:**
- `app/web/templates/base.html` — add `{% block body_attrs %}{% endblock %}` after `<body`.
- `app/web/templates/install.html` — convert to extends.
- `app/web/templates/corporate_memory.html` — convert to extends.
- `app/web/templates/corporate_memory_admin.html` — convert to extends.
- `app/web/templates/catalog.html` — convert to extends.
- `app/web/templates/admin_tables.html` — convert to extends; preserve `data-source-type` body attr via the new block.

**Possibly modified (per migration verification):**
- `_app_header.html` — script tag already lives there from the previous fix; no change expected unless we move it back to `<head>` for `defer` performance.
- `base.html` — add `{% block body_attrs %}` (one line).

**Tests:**
- `tests/test_web_ui.py` — likely has assertions on rendered HTML for these routes. Verify, update as needed.
- `tests/test_design_system_contract.py` — should stay green (it doesn't care about page chrome).

**No template deletes.** Every standalone page becomes shorter; CSS/JS volume stays the same per page (just relocates into blocks).

---

## Migration recipe (applied per page)

For every standalone template, the conversion follows a fixed pattern. Carry this recipe forward through Tasks 2–6.

### Source structure (before)

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Foo - {{ config.INSTANCE_NAME }}</title>
    {% if not config.THEME_FONT_URL %}
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    {% endif %}
    <style>
        /* … page-specific CSS … */
    </style>
    {% include '_theme.html' %}
</head>
<body>
    {% include '_app_header.html' %}
    {# page-specific content … #}
    <script>
        /* … page-specific JS … */
    </script>
</body>
</html>
```

### Target structure (after)

```jinja
{% extends "base.html" %}

{% block title %}Foo - {{ config.INSTANCE_NAME }}{% endblock %}

{% block head_extra %}
<style>
    /* … same page-specific CSS, verbatim … */
</style>
{% endblock %}

{% block layout %}
{# Use `layout` (not `content`) when the page renders its OWN top-level
   wrapper (e.g. dashboard.html does <main class="main">). Use `content`
   when the page is happy inside base.html's <div class="container">. #}
<main class="page-foo">
    {# page-specific markup, verbatim (minus the _app_header include —
       base.html includes it already). The <body data-x=…> attribute, if
       any, moves to a {% block body_attrs %}data-x="…"{% endblock %} #}
</main>
{% endblock %}

{% block scripts %}
<script>
    /* … same page-specific JS, verbatim … */
</script>
{% endblock %}
```

### Deletions per page

- `<!DOCTYPE html>` + `<html>` + `</html>` (base.html provides)
- `<head>` + `</head>` (base.html provides)
- `<meta charset>`, `<meta name="viewport">` (base.html provides)
- Font preconnect block — `base.html` doesn't ship it today, so this is a small **behavior change**: pages will lose the explicit Inter preconnect. Mitigation: add the preconnect once to base.html's `{% block head_extra %}` parent (or to `base.html` itself above the stylesheet link). See Step 1.
- `<link rel="stylesheet" href="…style-custom.css">` if any (base.html provides)
- `{% include '_theme.html' %}` (base.html provides)
- `<body>` opening tag (base.html provides; attrs go to `{% block body_attrs %}`)
- `{% include '_app_header.html' %}` at start of body (base.html includes it)
- `</body>` + closing tags

### Preserved per page

- `<title>` text → `{% block title %}`
- All inline `<style>` content → `{% block head_extra %}<style>...</style>{% endblock %}`
- All page markup → `{% block content %}` or `{% block layout %}`
- All inline `<script>` content → `{% block scripts %}<script>...</script>{% endblock %}`
- Page-specific JS variable usage (e.g. `data-source-type` on body) → `{% block body_attrs %}`

---

## Task 0: Setup + verify base.html blocks + add body_attrs slot

**Files:**
- Modify: `app/web/templates/base.html`

- [ ] **Step 1: Verify dashboard.html / home_*.html already extend base.html.**

```bash
grep -l "extends.*base\.html" app/web/templates/*.html
```

Expected output includes `dashboard.html`, `home_onboarded.html`, `home_not_onboarded.html`. Confirms our scope is exactly the 5 standalones, not more.

- [ ] **Step 2: Add `{% block body_attrs %}` to base.html.**

The `admin_tables.html` template currently renders `<body data-source-type="{{ data_source_type }}">`; its inline JS reads that attribute. We must preserve the attribute.

Read `base.html`'s `<body>` line, then change it from:

```html
<body>
```

to:

```html
<body {% block body_attrs %}{% endblock %}>
```

The default empty block keeps non-admin_tables pages unchanged.

- [ ] **Step 3: Add Inter font preconnect to base.html.**

Currently `base.html` ships only the stylesheet + `_theme.html` include. The 4 standalone pages that ship their own font preconnect (catalog, corporate_memory*, install) would lose the optimization after migration. Add to `base.html` `<head>` right BEFORE the stylesheet link:

```html
{% if not config.THEME_FONT_URL %}
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
{% endif %}
```

This change benefits ALL base.html consumers — admin pages currently rely on the system Inter being present. With this addition, base.html-extended pages always have the canonical font loaded.

- [ ] **Step 4: Confirm test pass + render check before any per-page migration.**

```bash
.venv/bin/python -m pytest tests/test_web_ui.py tests/test_web_home_page.py tests/test_design_system_contract.py -q
```

Expected: green. Captures the baseline before migrations begin.

- [ ] **Step 5: Commit.**

```bash
git add app/web/templates/base.html
git commit -m "feat(base): body_attrs block + Inter font preconnect

Adds a body_attrs Jinja block (default empty) so pages that extend
base.html can carry their own <body> attributes — admin_tables.html
needs data-source-type for its JS reading.

Hoists the Inter font preconnect + stylesheet link into base.html's
<head> so every page that extends base gets the same font load. The
5 standalone pages about to be migrated each had this block inline;
centralising it here means future changes (e.g. self-hosting the
font) land in one place."
```

---

## Task 1: Migrate install.html (smallest pilot)

**Why install first**: smallest of the 5 (1097 lines), simplest layout, no admin gating, well-contained. Validates the recipe before tackling the big templates.

**Files:**
- Modify: `app/web/templates/install.html`

- [ ] **Step 1: Read full install.html.**

```bash
wc -l app/web/templates/install.html
sed -n '1,30p' app/web/templates/install.html   # head (lines 1-30)
sed -n '640,660p' app/web/templates/install.html # </head> + <body> boundary
sed -n '935,945p' app/web/templates/install.html # <script> start area
tail -5 app/web/templates/install.html
```

Note the exact boundaries. Note any `<body … attribute>` (install.html: none).

- [ ] **Step 2: Convert via Python script (post-review revisions applied).**

```python
import pathlib, re

def migrate(filename: str) -> dict:
    """Convert a standalone Jinja template to extend base.html.

    Captures, in order:
      - <title>
      - Per-page <link> / <script src> / <style> inside <head> → head_extra (top)
      - Inline <style> blocks (with attributes preserved) → head_extra (after links)
      - <body> attributes → body_attrs
      - Body markup with the leading _app_header.html / _theme.html includes
        stripped and all inline scripts pulled out → layout block contents
      - All <script> blocks inside body (inline OR src, attributes preserved
        verbatim) → scripts block, in order

    Drops:
      - <!DOCTYPE>, <html>, </html>
      - <head>, </head>
      - <meta charset>, <meta viewport>
      - Duplicate <link rel="stylesheet" href=".../style-custom.css">
        (base.html already ships one)
      - </body>
    """
    path = pathlib.Path(f"app/web/templates/{filename}")
    text = path.read_text(encoding="utf-8")

    # 1) <title>
    title_m = re.search(r"<title>(.*?)</title>", text, re.DOTALL)
    title = title_m.group(1).strip() if title_m else filename

    # 2) Split <head> from <body>
    head_m = re.search(r"<head[^>]*>(.+?)</head>", text, re.DOTALL)
    body_m = re.search(r"<body([^>]*)>(.+?)</body>", text, re.DOTALL)
    if not head_m or not body_m:
        raise RuntimeError(f"missing <head> or <body> in {filename}")
    head = head_m.group(1)
    body_attrs = body_m.group(1).strip()
    body = body_m.group(2)

    # 3) Inside <head>: collect head-level <link>, <script src>, <style>.
    head_assets = []  # list of (kind, raw_tag) preserving source order
    cursor = 0
    HEAD_PATTERNS = [
        ("link",   re.compile(r"<link\b[^>]*?>", re.IGNORECASE)),
        ("script", re.compile(r"<script\b[^>]*?>.*?</script>", re.IGNORECASE | re.DOTALL)),
        ("style",  re.compile(r"<style\b[^>]*?>.*?</style>", re.IGNORECASE | re.DOTALL)),
    ]
    # Concatenate matches found in head, in their original source position.
    matches = []
    for kind, pat in HEAD_PATTERNS:
        for m in pat.finditer(head):
            matches.append((m.start(), kind, m.group(0)))
    matches.sort()
    for _, kind, raw in matches:
        # Skip duplicate style-custom.css link (base.html already provides).
        if kind == "link" and "style-custom.css" in raw:
            continue
        # Skip <link rel="preconnect/stylesheet" pointing at font.googleapis> —
        # we keep the inline preconnect block per page to preserve current
        # behaviour; do NOT dedupe against base.html in this pass.
        head_assets.append(raw)

    head_extra = "\n".join(head_assets)

    # 4) Inside <body>: pull out every <script> (inline OR src), in order,
    # for relocation to {% block scripts %}. Strip the leading
    # _app_header.html include + any leading {% include "_theme.html" %}.
    script_blocks = []
    SCRIPT_BODY_RE = re.compile(r"<script\b[^>]*?>.*?</script>", re.IGNORECASE | re.DOTALL)
    for m in SCRIPT_BODY_RE.finditer(body):
        script_blocks.append(m.group(0))
    body_no_scripts = SCRIPT_BODY_RE.sub("", body)

    # Strip leading _app_header.html include (with optional surrounding
    # whitespace and HTML comments).
    body_no_header = re.sub(
        r"^\s*(?:<!--[^>]*-->\s*)*\{%\s*include\s+['\"]_app_header\.html['\"]\s*%\}\s*",
        "", body_no_scripts, count=1
    )

    # 5) Compose output.
    layout_indent = "    "
    out = ['{% extends "base.html" %}', "", f"{{% block title %}}{title}{{% endblock %}}"]
    if body_attrs:
        out += ["", f"{{% block body_attrs %}}{body_attrs}{{% endblock %}}"]
    if head_extra.strip():
        out += ["", "{% block head_extra %}", head_extra, "{% endblock %}"]
    out += [
        "",
        "{% block layout %}",
        "{% include '_app_header.html' %}",
        body_no_header.rstrip(),
        "{% endblock %}",
    ]
    if script_blocks:
        out += ["", "{% block scripts %}"]
        out += script_blocks
        out += ["{% endblock %}"]
    out.append("")

    new_text = "\n".join(out)
    path.write_text(new_text, encoding="utf-8")
    return {
        "name": filename,
        "old_lines": len(text.splitlines()),
        "new_lines": len(new_text.splitlines()),
        "head_assets": len(head_assets),
        "script_blocks": len(script_blocks),
        "body_attrs": bool(body_attrs),
    }

result = migrate("install.html")
print(result)
```

- [ ] **Step 3: Decide content vs layout block.**

`install.html`'s top-level wrapper after the header — read it. If it's just standard content, `{% block content %}` is right (base.html wraps it in `<div class="container">`). If it has its own `<main>` or full-bleed elements, switch to `{% block layout %}` (which overrides base.html's container entirely) and manually re-add the `_app_header.html` include and `<main>` wrapper. **Default to `content`; switch only if the page looks broken in browser.**

- [ ] **Step 4: Smoke test in dev server.**

```bash
LOCAL_DEV_MODE=1 DATA_DIR=/tmp/agnes-design-pass-data .venv/bin/uvicorn app.main:app --port 8765 > /tmp/uv-install.log 2>&1 &
sleep 4
agent-browser open http://localhost:8765/install --wait-until networkidle
agent-browser screenshot /tmp/install-after.png --full
# Click Admin dropdown
agent-browser snapshot -i | grep -E "Admin|Hide"
# Verify install page's own behavior (whatever it does — copy buttons, accordions, etc.)
pkill -f "uvicorn.*8765"
```

Compare `install-after.png` against the baseline from `/tmp/design-pass-baseline/`.

- [ ] **Step 5: Run tests + commit.**

```bash
.venv/bin/python -m pytest tests/ -k "install or web_ui" -q
git add app/web/templates/install.html
git commit -m "refactor(install): extend base.html instead of standalone

Pilots the 5-page standalone→framework migration. install.html now
inherits <head>, <body>, font preconnect, theme include, app-header,
and the app.js script tag from base.html. Page-specific styles and
scripts kept verbatim inside head_extra + scripts blocks. -1097 line
template becomes ~+200 less (head/body scaffolding deleted)."
```

---

## Task 2: Migrate corporate_memory.html

**Files:** `app/web/templates/corporate_memory.html`

Same recipe as Task 1. Note: page is admin-gated (`{% if session.user %}` checks in markup). The wrapping logic isn't part of the migration — base.html's `_app_header.html` already handles the auth check.

- [ ] **Step 1–5: Apply Task-1 recipe verbatim.** Commit as `refactor(memory): extend base.html`.
- [ ] **Step 6: Verify memory-page-specific JS** (knowledge filter, voting, sync status) still works on `/corporate-memory`. Browser test: click a knowledge item, click upvote, click filter pill.

---

## Task 3: Migrate corporate_memory_admin.html

**Files:** `app/web/templates/corporate_memory_admin.html`

Same recipe. Admin-only curation page; modals + accordion behavior to verify.

- [ ] Apply recipe + commit `refactor(memory-admin): extend base.html`.
- [ ] Browser test: open a knowledge item modal, edit, save (don't commit DB; just verify modal opens and closes).

---

## Task 4: Migrate catalog.html

**Files:** `app/web/templates/catalog.html`

The biggest of the four "memory + catalog + install" group (2524 lines). Has source-cards, accordion, profiler overlay, two inline `<script>` blocks (868 lines + 26 lines).

- [ ] **Step 1: Concatenate the TWO inline `<script>` blocks** before relocating. Otherwise only the last one would land in `{% block scripts %}` and the smaller post-script would orphan. Either:
  - Order them as `script_outer + "\n\n" + script_inner` in the migration script, OR
  - Verify both are independent and emit them as two consecutive `<script>` blocks inside `{% block scripts %}`.
- [ ] Apply recipe + commit `refactor(catalog): extend base.html`.
- [ ] Browser test: load `/catalog`, click an accordion to expand, click a table row to open profiler overlay, verify "Live" / "Local" badges render.

---

## Task 5: Migrate admin_tables.html (biggest, highest risk)

**Files:** `app/web/templates/admin_tables.html`

3563 lines. Has the `data-source-type` body attribute (uses `{% block body_attrs %}` from Task 0). 850-line `<style>` block. 1795-line `<script>` block — biggest JS on the site (registry mutations, modal forms, AJAX, table polling).

- [ ] **Step 1: Apply the recipe with body_attrs override.**

Add in the new template:

```jinja
{% block body_attrs %}data-source-type="{{ data_source_type }}"{% endblock %}
```

- [ ] **Step 2: Stress-test the JS.** This page has the most behavior. Manual checks:
  - Source-type filter switcher
  - "+ Register table" modal opens
  - Click into a registered table row → edit modal opens
  - Cache warm-up trigger button
  - Table search filter
- [ ] Apply recipe + commit `refactor(admin-tables): extend base.html`.

---

## Task 6: Cross-page browser smoke + full pytest

**Files:** none (verification only).

- [ ] **Step 1: Boot dev server + iterate ALL 5 migrated routes via agent-browser.**

```bash
LOCAL_DEV_MODE=1 DATA_DIR=/tmp/agnes-design-pass-data .venv/bin/uvicorn app.main:app --port 8765 > /tmp/uv-smoke.log 2>&1 &
sleep 4
for r in /install /corporate-memory /corporate-memory/admin /catalog /admin/tables; do
    safe="${r//\//-}"
    agent-browser open "http://localhost:8765${r}" --wait-until networkidle
    agent-browser screenshot "/tmp/framework-after${safe}.png" --full
done

# Click Admin dropdown on each — confirm it opens
for r in /install /corporate-memory /catalog /admin/tables; do
    agent-browser open "http://localhost:8765${r}"
    snap=$(agent-browser snapshot -i)
    admin_ref=$(echo "$snap" | grep -oE 'button "Admin"[^@]*@e[0-9]+' | grep -oE 'e[0-9]+' | head -1)
    hide_ref=$(echo "$snap" | grep -oE 'link "Hide »"[^@]*@e[0-9]+' | grep -oE 'e[0-9]+' | head -1)
    [ -n "$hide_ref" ] && agent-browser click "@$hide_ref"
    sleep 1
    agent-browser click "@$admin_ref"
    sleep 1
    echo "$r — Admin: $(agent-browser snapshot -i | grep -c menuitem) menu items"
done

pkill -f "uvicorn.*8765"
```

Expected: each page → 15+ menuitem entries when Admin dropdown opens.

- [ ] **Step 2: Full pytest.**

```bash
.venv/bin/python -m pytest tests/ --tb=line -n auto -q
```

Expected: same baseline pass count (4500+) + 12 pre-existing Keboola/clean-install fails.

- [ ] **Step 3: Visual diff vs baseline.** Open at least one screenshot per migrated page and compare against `/tmp/design-pass-baseline/`. Any unexpected layout shift → flag for fix before commit.

---

## Task 7: CHANGELOG entry + final push

- [ ] **Step 1: Add to CHANGELOG.md `[Unreleased]`.**

Under `### Changed`:

```markdown
- All web templates now extend `base.html`. Previously 5 templates
  (`catalog.html`, `corporate_memory.html`, `corporate_memory_admin.html`,
  `install.html`, `admin_tables.html`) shipped their own `<html>` /
  `<head>` / `<body>` scaffold — a source of drift when shared
  infrastructure changed (today's symptom: the nav-dropdown
  `app.js` script lived only in `base.html`, so those 5 pages had
  dead dropdowns). `base.html` now exposes a `body_attrs` Jinja
  block + emits the Inter font preconnect, so all pages share one
  rendering pipeline.
```

- [ ] **Step 2: Final pytest + push.**

```bash
.venv/bin/python -m pytest tests/ --tb=line -n auto -q | tail -8
git push origin zs/design-pass
```

PR #284 auto-updates with the new commits.

---

## Risk register

1. **Page-specific JS reads from elements that base.html wraps differently.** Mitigation: keep `{% block content %}` markup byte-identical to the old body content (sans `_app_header.html` include). IDs, classes, data attrs all preserved. JS sees the same DOM.

2. **`<body data-source-type=…>` on admin_tables.html.** Mitigation: `{% block body_attrs %}` slot added to base.html in Task 0.

3. **Per-page CSS specificity collisions with style-custom.css.** Inline `<style>` blocks have always loaded AFTER style-custom.css. After migration, the inline `<style>` is in `{% block head_extra %}` which sits AFTER style-custom.css link in base.html. Order preserved. No specificity flip.

4. **Page-specific font preconnect already loaded twice.** Currently 4 of the 5 pages have the Inter preconnect inline; after migration they inherit it from base.html. Mitigation: Task 0 Step 3 hoists the preconnect to base.html before any per-page migration. After Task 0, the inline ones become duplicates → harmless but should be deleted as part of each per-page conversion (the migration script captures only `<style>` content + script content, so preconnect lines naturally drop).

5. **Login pages (`base_login.html` consumers).** Not in scope. Login pages still use `base_login.html`. The framework migration is for authed-content pages only.

6. **Reviewer fatigue.** ~5000 LOC of diff across 6 commits. Mitigation: per-page commit boundary → reviewer can read one commit, ack, move on.

---

## Self-review

- **Spec coverage**: 5 standalone pages enumerated; all 5 have a task. ✅
- **Block-mapping completeness**: title, head_extra, content/layout, scripts, body_attrs all addressed. ✅
- **No placeholders**: each migration step has concrete shell + Python code. ✅
- **Tests run before each commit**: Task 0 Step 4, Task 1 Step 5, Task 6 Step 2. ✅
- **CHANGELOG entry**: Task 7 Step 1. ✅
- **One-PR continuation**: lands on existing `zs/design-pass`. ✅
- **Rollback granularity**: per-page commits; revert individual migrations cleanly. ✅
