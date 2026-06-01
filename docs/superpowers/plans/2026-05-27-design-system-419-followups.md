# Design-system #419 follow-ups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every remaining open checkbox on https://github.com/keboola/agnes-the-ai-analyst/issues/419 in a single PR — mechanical sweeps, CI contract test, test-pin sweep, base_ds.html adoption (proof point profile.html), and 7 macro additions to _components.html.

**Architecture:** Per-workstream Jinja/CSS edits + new contract test + macro extensions in app/web/templates/_components.html. No app logic changed. Worktree branch already created: `worktree-zs-design-419-followups` (at `04fcae81`).

**Tech Stack:** Jinja2 templates, custom CSS in `app/web/static/style-custom.css`, design tokens in `app/web/static/css/design-tokens.css`, pytest contract tests under `tests/test_design_system_contract.py`, fixture-based template asserts under `tests/test_web_*.py`.

---

## Files-to-touch summary

```
NEW   tests/_template_assertions.py
EDIT  tests/test_web_marketplace_guide.py
EDIT  tests/test_web_home_page.py
EDIT  tests/test_design_system_contract.py
EDIT  app/web/templates/profile.html
EDIT  app/web/templates/setup.html
EDIT  app/web/templates/me_activity.html
EDIT  app/web/templates/base.html
NEW   app/web/templates/_app_scripts.html
EDIT  app/web/templates/_components.html
EDIT  app/web/static/style-custom.css        (new rules for 7 macros)
EDIT  18 templates with var(--primary) — list in T04
```

Full `var(--primary)` adopter list (18 templates, 129 occurrences — counts verified via `git grep -c`):

```
app/web/templates/_profile_tokens.html              (12)
app/web/templates/admin/news_editor.html             (3)
app/web/templates/admin_corporate_memory.html       (23)
app/web/templates/admin_tables.html                 (10)
app/web/templates/admin_tokens.html                 (10)
app/web/templates/base_ds.html                       (1)   ← only inside a comment block; verify before editing
app/web/templates/catalog.html                       (4)
app/web/templates/catalog_package_detail.html        (3)
app/web/templates/catalog_recipe_detail.html         (4)
app/web/templates/catalog_table_detail.html          (7)
app/web/templates/dashboard.html                     (1)
app/web/templates/install.html                      (15)
app/web/templates/marketplace_guide.html             (2)
app/web/templates/marketplace_item_detail.html       (4)
app/web/templates/marketplace_plugin_detail.html     (8)
app/web/templates/store_edit.html                    (2)
app/web/templates/store_examples.html                (2)
app/web/templates/store_upload.html                 (17)
                                              total: 128 + 1 base_ds comment
```

---

## Hard constraints

- **TDD per task:** write a failing test FIRST, then implement, then assert pass.
- **Frequent commits:** one commit per task, conventional commits style. No co-author trailers (per CLAUDE.md).
- **DRY:** define one shared helper `tests/_template_assertions.py::assert_element` for the test-pin sweep.
- **YAGNI:** macros 6–12 each migrate ONE proof template only. Untouched adopters → TODO list in `_components.html`.
- **Pre-1.0 patch version policy** (CLAUDE.md): no minor bumps; release-cut is the controller's job.
- **No customer-specific tokens in commits/PR/CHANGELOG** (CLAUDE.md vendor-agnostic OSS rule).

---

## Task ordering

```
T01  shared helper tests/_template_assertions.py + TDD seed assertion conversion
T02  test pin sweep — test_web_marketplace_guide.py (6 assertions)
T03  test pin sweep — test_web_home_page.py (17 assertions)
T04  var(--primary) → var(--ds-primary) sweep + contract-test guard
T05  hex sweep — profile.html (3 hex)
T06  hex sweep — setup.html (17 hex)
T07  hex sweep — me_activity.html (16 hex)
T08  CI class-coverage contract test
T09  extract inline JS base.html → _app_scripts.html (no consumer change)
T10  migrate profile.html to extend base_ds.html (proof of adoption)
T11  macro tabs_rich + adopter: marketplace.html
T12  macro segmented_strip + adopter: home_not_onboarded.html (.mode-tabs at line 302)
T13  macro pill_chip + adopter: catalog.html
T14  macro kpi_card + adopter: admin_sessions.html
T15  macro hero_search_btn + adopter: marketplace.html (.search-btn)
T16  macro info_panel_accent + adopter: marketplace.html (.mp-curator-block)
T17  macro code_chip (NO adopter — page-local pattern per body)
T18  smoke checklist — manual URL list, docs only
```

---

## T01 — Shared helper + TDD seed

**Files:**
- Create: `tests/_template_assertions.py`
- Modify: `tests/test_web_marketplace_guide.py` (replace ONE rigid assertion as the seed conversion)

**Step 1 — Write failing test** (place at end of `tests/_template_assertions.py`):
```python
"""Tests for the shared semantic template-assertion helper.

Lives alongside the helper so a broken helper fails its own tests, not
just every caller. Run as part of the normal `pytest` collection.
"""
import re
import pytest

from tests._template_assertions import assert_element, ElementNotFound


def test_assert_element_matches_attr_order_agnostic():
    html = '<a class="btn btn-primary" href="/x">Submit</a>'
    assert_element(html, "a", class_="btn btn-primary", href="/x", text="Submit")


def test_assert_element_matches_when_attrs_reordered():
    html = '<a href="/x" class="btn btn-primary">Submit</a>'
    assert_element(html, "a", class_="btn btn-primary", href="/x", text="Submit")


def test_assert_element_matches_class_subset():
    """An element with extra classes still matches if the required tokens
    are all present. Order of class tokens is irrelevant."""
    html = '<a class="btn-primary btn extra" href="/x">Submit</a>'
    assert_element(html, "a", class_="btn btn-primary", href="/x")


def test_assert_element_text_is_regex_with_whitespace_collapse():
    html = '<a class="btn" href="/x">\n  Submit a skill\n  or plugin\n</a>'
    assert_element(html, "a", class_="btn", href="/x",
                   text=r"Submit a skill or plugin")


def test_assert_element_raises_with_diagnostic_when_class_missing():
    html = '<a class="btn-secondary" href="/x">Submit</a>'
    with pytest.raises(ElementNotFound, match=r"class.*btn-primary"):
        assert_element(html, "a", class_="btn btn-primary", href="/x")
```

**Step 2 — Run and confirm failure:**
```
.venv/bin/pytest tests/_template_assertions.py -x
# expected: ModuleNotFoundError: No module named 'tests._template_assertions'
#           (or ImportError on the names if the file exists but is empty)
```

**Step 3 — Implement** `tests/_template_assertions.py`:
```python
"""Shared semantic HTML assertion helper for template tests.

Replaces brittle exact-string `<tag class="…">` checks. Required tokens
are evaluated as a SUBSET — extra classes on the element don't break
the match. Attribute order is irrelevant; whitespace inside text content
is collapsed before matching. Text is a regex (case-sensitive) — pass a
literal string for substring-style assertions or a regex for shape
matching.

Examples:
    assert_element(body, "a",
                   class_="btn btn-primary",
                   href="/marketplace/guide/flea")

    assert_element(body, "div", class_="guide-fastpath")

    assert_element(body, "a",
                   class_="btn btn-secondary",
                   href="/marketplace/guide/curated",
                   attrs={"data-actions-for": "curated"},
                   text=r"Submit a skill or plugin")
"""
from __future__ import annotations

import re
from typing import Mapping


class ElementNotFound(AssertionError):
    """Raised when assert_element can't find a matching element."""


# Open-tag regex: <TAG ATTRS> capturing the attribute blob. Self-closing
# OK because we only need attributes; closing tag is matched lazily.
_OPEN_TAG_RE = lambda tag: re.compile(
    r"<" + re.escape(tag) + r"\b(?P<attrs>[^>]*)>(?P<body>.*?)</"
    + re.escape(tag) + r"\s*>",
    re.DOTALL,
)

_ATTR_RE = re.compile(r"""(\w[\w:-]*)\s*=\s*(['"])(.*?)\2""", re.DOTALL)
_WS_RE = re.compile(r"\s+")


def _parse_attrs(attr_blob: str) -> dict[str, str]:
    return {m.group(1): m.group(3) for m in _ATTR_RE.finditer(attr_blob)}


def _collapse(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def assert_element(
    html: str,
    tag: str,
    *,
    class_: str | None = None,
    href: str | None = None,
    text: str | None = None,
    attrs: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Find an element matching the predicate, return its attr dict.

    Raises ElementNotFound with a diagnostic message if no element matches.
    """
    required_classes = set((class_ or "").split())
    required_attrs = dict(attrs or {})
    if href is not None:
        required_attrs["href"] = href

    matches = []
    for m in _OPEN_TAG_RE(tag).finditer(html):
        el_attrs = _parse_attrs(m.group("attrs"))
        el_classes = set(el_attrs.get("class", "").split())
        if required_classes and not required_classes.issubset(el_classes):
            continue
        if any(el_attrs.get(k) != v for k, v in required_attrs.items()):
            continue
        if text is not None and not re.search(text, _collapse(m.group("body"))):
            continue
        matches.append(el_attrs)

    if not matches:
        raise ElementNotFound(
            f"no <{tag}> matched "
            f"class={sorted(required_classes)} attrs={required_attrs} "
            f"text={text!r}"
        )
    return matches[0]
```

**Step 4 — Confirm pass:**
```
.venv/bin/pytest tests/_template_assertions.py -v
# expected: 5 passed
```

**Step 5 — Commit:**
```
git add tests/_template_assertions.py
git commit -m "test(web): shared semantic template-assertion helper"
```

---

## T02 — Test pin sweep · test_web_marketplace_guide.py

**Files:**
- Modify: `tests/test_web_marketplace_guide.py` (lines 104, 111, 116, 141, 150, 152)

The 6 rigid `<tag class="…">` assertions to convert (verified via grep at the noted lines):

| Line | Current assertion |
|---:|---|
| 104 | `'<ol class="guide-steps">' in body` |
| 111 | `'<div class="guide-fastpath">' in body` |
| 116 | `'<a href="/marketplace/guide/flea" class="btn btn-primary"' in body` |
| 141 | `'<ol class="guide-steps">' in body` |
| 150 | `'<a href="/store/new" class="btn btn-primary"' in body` |
| 152 | `'<div class="guide-fastpath">' not in body` |

Plus the existing regex block at lines 63-72 (the curated CTA) — leave it as-is; it already matches the helper's semantic intent.

**Step 1 — Write failing test** by changing the current passing assertions to invoke `assert_element` BEFORE the implementation exists. Replace line 104 area first:

```python
# new top-of-file imports
from tests._template_assertions import assert_element, ElementNotFound

# at line ~104:
assert_element(body, "ol", class_="guide-steps")

# at line ~111:
assert_element(body, "div", class_="guide-fastpath")

# at line ~116:
assert_element(body, "a",
               class_="btn btn-primary",
               href="/marketplace/guide/flea")

# at line ~141:
assert_element(body, "ol", class_="guide-steps")

# at line ~150:
assert_element(body, "a",
               class_="btn btn-primary",
               href="/store/new")

# at line ~152: negative case
with pytest.raises(ElementNotFound):
    assert_element(body, "div", class_="guide-fastpath")
```

**Step 2 — Run, confirm pass** (these should already pass since the live templates render the matching markup):
```
.venv/bin/pytest tests/test_web_marketplace_guide.py -v
# expected: 3 passed (no regression vs. before the conversion)
```

(For this task the "failing-first" beat is captured by the T01 helper tests. T02 is a mechanical conversion that must remain green.)

**Step 3 — n/a** (conversion is the implementation).

**Step 4 — Confirm pass:** same command.

**Step 5 — Commit:**
```
git add tests/test_web_marketplace_guide.py
git commit -m "test(web): convert marketplace_guide pins to semantic assert_element"
```

---

## T03 — Test pin sweep · test_web_home_page.py

**Files:**
- Modify: `tests/test_web_home_page.py` (lines 100, 102, 154, 155, 232, 339, 343, 358, 359, 381, 382, 400, 420, 438, 457, 458 — 17 rigid assertions verified by grep)

**Step 1 — Convert all 17.** Pattern:

```python
# was:
assert '<div class="install-hero">' in body
# becomes:
assert_element(body, "div", class_="install-hero")

# was:
assert '<div class="install-hero">' not in body
# becomes:
with pytest.raises(ElementNotFound):
    assert_element(body, "div", class_="install-hero")

# was:
assert '<div class="home-mock" data-setup-minimized' not in body
# becomes:
with pytest.raises(ElementNotFound):
    assert_element(body, "div", class_="home-mock",
                   attrs={"data-setup-minimized": ""})  # presence only — see note

# was:
assert 'class="home-mock"\n' in body or '<div class="home-mock">' in body
# becomes:
assert_element(body, "div", class_="home-mock")
```

For attribute-presence-only assertions (`data-setup-minimized` with no value asserted), accept the attribute being EITHER unset on a match OR present on a non-match. Simpler approach: keep the helper signature checking value equality only, and use a regex search for presence:
```python
assert re.search(r'<div\b[^>]*\bclass="[^"]*\bhome-mock\b[^"]*"[^>]*\bdata-setup-minimized\b', body) is None
```
Add this as a small `assert_attr_present` helper inside `_template_assertions.py` ONLY if more than one site needs it. Otherwise inline the regex.

Add at top of file:
```python
from tests._template_assertions import assert_element, ElementNotFound
```

**Step 2 — Run before each batch:**
```
.venv/bin/pytest tests/test_web_home_page.py -v
# expected: all originally-passing tests still pass
```

**Step 3-4 — n/a / confirm pass.**

**Step 5 — Commit:**
```
git add tests/test_web_home_page.py
git commit -m "test(web): convert home_page pins to semantic assert_element"
```

---

## T04 — var(--primary) → var(--ds-primary) sweep + regression guard

**Files:**
- Modify (18): list above; ~128 raw occurrences. Use `git grep` to locate every line.
- Modify: `tests/test_design_system_contract.py` (add the regression guard test)

**Step 1 — Write failing test** in `tests/test_design_system_contract.py`:

```python
def test_no_unprefixed_primary_token_in_templates() -> None:
    """`var(--primary)` (no --ds- prefix, no hex fallback) inside a template
    rides the legacy blue token. The compat shim in design-tokens.css
    remaps it to --ds-primary, but explicit `var(--ds-primary)` reads
    self-documenting in code review and survives a future shim removal.

    Per #419 follow-up sweep: every template MUST reference --ds-primary
    explicitly. `base.html` and `base_ds.html` are exempt — `base.html`
    only references --primary inside CSS-comment blocks documenting the
    legacy compat shim; `base_ds.html` likewise only mentions it in a
    doc comment block. The check excludes these two by name.
    """
    pattern = re.compile(r"var\(\s*--primary\s*[,)]")
    exempt = {"base.html", "base_ds.html"}
    offenders: list[str] = []
    for path in _all_html():
        if path.name in exempt:
            continue
        if pattern.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path))
    assert not offenders, (
        "`var(--primary)` found — use `var(--ds-primary)` instead:\n"
        + "\n".join(f"  {p}" for p in offenders)
    )
```

**Step 2 — Run and confirm failure:**
```
.venv/bin/pytest tests/test_design_system_contract.py::test_no_unprefixed_primary_token_in_templates -x
# expected: assertion error listing all 18 templates
```

**Step 3 — Implement (mechanical sweep):**

Use a per-file confirmed sed. For each adopter from the list above, run:
```
# DRY-run preview per file
git grep -n 'var(--primary' app/web/templates/<file>.html
# Apply:
sed -i '' 's|var(--primary)|var(--ds-primary)|g; \
           s|var(--primary,|var(--ds-primary,|g; \
           s|var(--primary-dark)|var(--ds-primary-dark)|g; \
           s|var(--primary-light)|var(--ds-primary-light)|g' \
    app/web/templates/<file>.html
```

Note: `var(--primary, #hex)` inside templates — check first; the existing `test_no_legacy_primary_token_with_hex_fallback` test already gates these via the `_LEGACY_TOKEN_FALLBACK_ALLOWLIST`. After T04, delete entries from that allowlist that are now clean (verify by grep).

Two special cases:
1. **`install.html`** (15 occurrences) — check there are no `--primary-dark/--primary-light` references; if so apply the broader sed; otherwise the narrow form is enough.
2. **`base_ds.html`** (1 occurrence) — confirmed it is inside a doc-comment block; LEAVE IT and add `base_ds.html` to the test's `exempt` set as documented above.

**Step 4 — Confirm pass:**
```
.venv/bin/pytest tests/test_design_system_contract.py -v
.venv/bin/pytest tests/test_web_home_page.py tests/test_web_marketplace_guide.py -q
```

**Step 6 — Full suite for incidental breakage:**
```
.venv/bin/pytest -q
```

**Step 5 — Commit:**
```
git add app/web/templates/*.html app/web/templates/admin/news_editor.html tests/test_design_system_contract.py
git commit -m "refactor(web): switch templates to var(--ds-primary) + regression guard"
```

---

## T05 — Hex sweep · profile.html

**Files:**
- Modify: `app/web/templates/profile.html` (3 hex at lines 6, 7, 9)

Verified hex literals:
```
.group-chip.is-admin    { background: #fef3c7; color: #92400e; … }
.group-chip.is-everyone { background: #f3f4f6; color: #4b5563; }
.group-chip.is-custom   { background: #ede9fe; color: #6d28d9; }
```

Token mapping (no new tokens introduced — all map to existing `--ds-accent-*` from design-tokens.css):
- `#fef3c7 / #92400e` → `var(--ds-accent-warn-bg) / var(--ds-accent-warn-ink)` (amber)
- `#f3f4f6 / #4b5563` → `var(--ds-surface-dim) / var(--ds-text-secondary)` (neutral)
- `#ede9fe / #6d28d9` → NEW: this is a violet accent with no canonical token. Decision: alias to `var(--ds-accent-info-bg) / var(--ds-accent-info-ink)` (blue) to stay inside the canonical 4. Acceptable per #419 body ("map to existing tokens, do NOT invent new ones").

**Step 1 — Write failing test** (add to `tests/test_design_system_contract.py`):
```python
def test_profile_template_uses_no_raw_hex() -> None:
    """profile.html is the proof-point page for the design-system token
    discipline (paired with the base_ds.html adoption task). Any raw
    `#RRGGBB` literal here would mean a colour escaped the token system."""
    text = (TEMPLATES / "profile.html").read_text(encoding="utf-8")
    hexes = re.findall(r"#[0-9a-fA-F]{6}\b", text)
    assert not hexes, f"profile.html contains raw hex: {hexes}"
```

**Step 2 — Run and confirm failure:**
```
.venv/bin/pytest tests/test_design_system_contract.py::test_profile_template_uses_no_raw_hex -x
# expected: AssertionError listing #fef3c7, #92400e, #f3f4f6, #4b5563, #ede9fe, #6d28d9
```

**Step 3 — Implement** in `app/web/templates/profile.html` lines 6-9:
```html
<style>
.group-chip.is-admin    { background: var(--ds-accent-warn-bg); color: var(--ds-accent-warn-ink); font-weight: 600; }
.group-chip.is-everyone { background: var(--ds-surface-dim);    color: var(--ds-text-secondary); }
.group-chip.is-custom   { background: var(--ds-accent-info-bg); color: var(--ds-accent-info-ink); }
</style>
```

**Step 4 — Confirm pass:**
```
.venv/bin/pytest tests/test_design_system_contract.py::test_profile_template_uses_no_raw_hex -v
```

**Step 6 — Full suite:**
```
.venv/bin/pytest -q
```

**Step 5 — Commit:**
```
git add app/web/templates/profile.html tests/test_design_system_contract.py
git commit -m "refactor(web): replace profile.html hex literals with --ds-accent tokens"
```

---

## T06 — Hex sweep · setup.html

**Files:**
- Modify: `app/web/templates/setup.html` (17 hex; key occurrences at lines 21-24, 35, 39, 52, 61, 65, 71, 75, 90, 97, 106, 135-136, 147)

Verified token mapping (every hex → existing token; no new tokens):

| Hex | Token |
|---|---|
| `#2563eb` (var(--primary, #2563eb)) | `var(--ds-primary)` (drop fallback) |
| `#e5e7eb` (step-dot inactive) | `var(--ds-border)` |
| `#d1d5db` (input border) | `var(--ds-border)` (same surface) |
| `#6b7280` (paragraph muted text) | `var(--ds-text-muted)` |
| `#f0fdf4` (info bg) | `var(--ds-accent-success-bg)` |
| `#fef2f2` (error bg) | `var(--ds-accent-danger-bg)` |
| `#dc2626` (error fg) | `var(--ds-accent-danger-line)` |
| `#16a34a` (success fg) | `var(--ds-accent-success-line)` |

**Step 1 — Write failing test:**
```python
def test_setup_template_uses_no_raw_hex() -> None:
    text = (TEMPLATES / "setup.html").read_text(encoding="utf-8")
    hexes = re.findall(r"#[0-9a-fA-F]{6}\b", text)
    assert not hexes, f"setup.html contains raw hex: {hexes}"
```

**Step 2 — Run and confirm failure** (expect ~17 listed).

**Step 3 — Implement.** For each line, replace inline-style hex with the token from the mapping table. Inline `style="..."` rules referencing colour-related properties must read tokens (`var(--…)`). Also remove `(--primary, #2563eb)` fallbacks per the T04 contract test.

Worked example (line 21 area):
```html
<!-- before -->
<div id="step-dot-1" style="… background: var(--primary, #2563eb);"></div>
<div id="step-dot-2" style="… background: #e5e7eb;"></div>

<!-- after -->
<div id="step-dot-1" style="… background: var(--ds-primary);"></div>
<div id="step-dot-2" style="… background: var(--ds-border);"></div>
```

JS in `setup.html` (lines 135-136, 147) — same replacement. The JS at line 147 builds inline style strings; substitute the tokens via `var()` directly:
```js
document.getElementById('step-dot-' + i).style.background = i <= n ? 'var(--ds-primary)' : 'var(--ds-border)';
```
(`element.style.background = 'var(--ds-primary)'` works in evergreen browsers — verified pattern, but if older-IE concerns surface, switch to setProperty.)

**Step 4 — Confirm pass:**
```
.venv/bin/pytest tests/test_design_system_contract.py::test_setup_template_uses_no_raw_hex -v
```

**Step 6 — Full suite:**
```
.venv/bin/pytest -q
```

**Step 5 — Commit:**
```
git add app/web/templates/setup.html tests/test_design_system_contract.py
git commit -m "refactor(web): replace setup.html hex literals with --ds-* tokens"
```

---

## T07 — Hex sweep · me_activity.html

**Files:**
- Modify: `app/web/templates/me_activity.html` (16 hex; lines 32-78, 145, 155, 285)

Verified mapping:

| Hex | Token |
|---|---|
| `#6b7280` (muted text) | `var(--ds-text-muted)` |
| `#b91c1c` (error fg) | `var(--ds-accent-danger-ink)` |
| `#2563eb` (chart fill, link) | `var(--ds-primary)` |
| `#fef3c7 / #92400e` (badge-pending) | `var(--ds-accent-warn-bg) / var(--ds-accent-warn-ink)` |
| `#dbeafe / #1e40af` (badge-processed) | `var(--ds-accent-info-bg) / var(--ds-accent-info-ink)` |
| `#d1fae5 / #065f46` (badge-extracted) | `var(--ds-accent-success-bg) / var(--ds-accent-success-ink)` |
| `#e5e7eb / #f3f4f6 / #f9fafb` (chrome) | `var(--ds-border) / var(--ds-surface-dim) / var(--ds-border-light)` |
| `#fff` (surface) | `var(--ds-surface)` |
| `#9ca3af` (subtle inline em) | `var(--ds-text-muted)` |

**Step 1 — Write failing test:**
```python
def test_me_activity_template_uses_no_raw_hex() -> None:
    text = (TEMPLATES / "me_activity.html").read_text(encoding="utf-8")
    hexes = re.findall(r"#[0-9a-fA-F]{3,6}\b", text)
    assert not hexes, f"me_activity.html contains raw hex: {hexes}"
```

(Use 3-or-6 char regex here because the file may contain `#fff`.)

**Step 2 — Run and confirm failure.**

**Step 3 — Implement.** Pay attention to two existing fallback patterns at lines 32, 50, 54, 58, 72, 74, 78 — already of shape `var(--token, #hex)`. Per T04 policy + the existing `test_no_legacy_primary_token_with_hex_fallback`, drop the hex fallback entirely after switching to `--ds-*`:
```css
/* before */
.stats-loading { color: var(--hp-text-muted, #6b7280); … }
/* after */
.stats-loading { color: var(--ds-text-muted); … }
```

The line 285 case (inside a JS template literal):
```js
// before
<td>${r.primary_model || '<em style="color:#9ca3af;">unprocessed</em>'}</td>
// after
<td>${r.primary_model || '<em style="color:var(--ds-text-muted);">unprocessed</em>'}</td>
```

**Step 4 — Confirm pass:**
```
.venv/bin/pytest tests/test_design_system_contract.py::test_me_activity_template_uses_no_raw_hex -v
```

**Step 6 — Full suite:**
```
.venv/bin/pytest -q
```

**Step 5 — Commit:**
```
git add app/web/templates/me_activity.html tests/test_design_system_contract.py
git commit -m "refactor(web): replace me_activity.html hex literals with --ds-* tokens"
```

---

## T08 — CI class-coverage contract test

**Files:**
- Modify: `tests/test_design_system_contract.py` (add one test)

The test renders or statically extracts every static class-name token emitted by the 5+7 = 12 macros in `_components.html` and asserts each resolves to a CSS rule in either `style-custom.css` OR `app/web/static/css/components.css`.

**Approach:** static extraction beats render — macros that take variant args (`button(variant='primary')`) emit `btn-primary` / `btn-secondary` / `btn-ghost` / `btn-danger` / `btn-google`. We enumerate the documented variants in the macro docstring as a tuple in the test, then assemble the expected class set.

**Step 1 — Write failing test:**
```python
def test_macros_emit_only_classes_that_have_css_rules() -> None:
    """Every literal class token assembled by `_components.html` macros
    must resolve to a CSS rule in style-custom.css OR css/components.css.

    Doesn't render — extracts static tokens + the enumerated variant
    surface from the macro docstrings. Catches: a macro emitting a class
    nobody defined, OR a CSS rule being deleted while a macro still
    references it.
    """
    text = (TEMPLATES / "_components.html").read_text(encoding="utf-8")
    css_pool = (
        (STATIC / "style-custom.css").read_text(encoding="utf-8")
        + "\n"
        + (STATIC / "css" / "components.css").read_text(encoding="utf-8")
        + "\n"
        + (STATIC / "css" / "stack_card.css").read_text(encoding="utf-8")
        + "\n"
        + (STATIC / "css" / "marketplace.css").read_text(encoding="utf-8")
    )

    # Static class literals appearing inside class="…" attrs OR
    # `classes.append('…')` calls within the macros. Both patterns map
    # to known surface area.
    literal_classes: set[str] = set()
    for m in re.finditer(r"""class\s*=\s*"([^"{}]+)\"""", text):
        for tok in m.group(1).split():
            literal_classes.add(tok)
    for m in re.finditer(r"""classes\.append\(['"]([\w\-]+)['"]\)""", text):
        literal_classes.add(m.group(1))

    # Variant surface — macros that build class names from kwargs:
    # button: btn-{variant} + (optional) btn-{size} + btn--icon
    # panel:  ds-card--accent + ds-card--{accent}
    # tabs:   tab-strip__item is-active
    # primary_nav: app-nav-link is-active
    # table:  ds-table--dense, ds-table--zebra
    constructed = {
        "btn-primary", "btn-secondary", "btn-ghost", "btn-danger", "btn-google",
        "btn-sm",
        "btn--icon",
        "ds-card--accent",
        "ds-card--info", "ds-card--success", "ds-card--warn", "ds-card--danger",
        "is-active",
        "ds-table--dense", "ds-table--zebra",
        # T11-T17 add: extend this set as each macro lands. Required for T11+.
    }

    expected = literal_classes | constructed
    # Strip Jinja remnants the regex shouldn't have captured but might.
    expected = {c for c in expected if c and "{" not in c and "}" not in c}

    missing = sorted(c for c in expected
                     if "." + c + " " not in css_pool
                     and "." + c + "," not in css_pool
                     and "." + c + "{" not in css_pool
                     and "." + c + ":" not in css_pool
                     and "." + c + "[" not in css_pool
                     and "." + c + "::" not in css_pool
                     and "." + c + "." not in css_pool
                     and "." + c + ">" not in css_pool
                     and "." + c + "\n" not in css_pool)
    assert not missing, (
        "macros emit classes with no backing CSS rule: " + str(missing)
    )
```

**Step 2 — Run and confirm pass on current main** (the 5 existing macros are all backed by CSS):
```
.venv/bin/pytest tests/test_design_system_contract.py::test_macros_emit_only_classes_that_have_css_rules -v
# expected: 1 passed
```

If any unexpected misses surface, treat that as a real bug — the macro emits something the CSS doesn't define. Fix the CSS, not the test.

**Step 5 — Commit:**
```
git add tests/test_design_system_contract.py
git commit -m "test(web): contract test for macro class-coverage against CSS"
```

---

## T09 — Extract inline JS · base.html → _app_scripts.html

**Files:**
- Modify: `app/web/templates/base.html` (lines 82-653 are the inline `<script>`)
- Create: `app/web/templates/_app_scripts.html`

**What moves verbatim** (verified by reading base.html lines 82-653):

1. `window.showUndoToast` — lines 88-150 — global undo-toast helper
2. Modal-Esc handler — lines 156-205 (anonymous IIFE)
3. `.cf-palette-row` swatch hydration — lines 211-294 (IIFE)
4. Admin `g` key two-keystroke navigation — lines 304-374 (IIFE)
5. Admin Cmd/Ctrl-K command palette — lines 381-594 (IIFE)
6. `.stack-tabs` digit shortcuts — lines 600-623 (IIFE)
7. Admin-nav `<details>` open/closed persistence — lines 629-652 (IIFE)

All 7 blocks move INTO the new partial verbatim. The partial is `<script>…</script>` once at the top, then all bodies in order.

**Step 1 — Write failing test** (add to `tests/test_design_system_contract.py`):
```python
def test_app_scripts_partial_carries_inline_helpers() -> None:
    """The 7 inline helpers (undo toast, modal Esc, palette swatches,
    g-shortcut nav, Cmd-K palette, stack-tabs digit shortcut, admin-nav
    details persistence) must be extracted into _app_scripts.html so
    base_ds.html can include them too. base.html must include the
    partial (not duplicate the helpers)."""
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    partial_path = TEMPLATES / "_app_scripts.html"
    assert partial_path.exists(), "_app_scripts.html partial must exist"
    partial = partial_path.read_text(encoding="utf-8")

    markers = [
        "window.showUndoToast",
        "_closeTopmost",                       # modal-Esc IIFE
        ".cf-palette-row",                     # palette swatches
        "agnes-admin-nav-collapsed-sections",  # details persistence
        "adminCmdkOverlay",                    # cmd palette
        "stack-tabs button[data-tab]",         # digit shortcut
        "adminNavMenu",                        # admin shortcuts gate
    ]
    for m in markers:
        assert m in partial, f"_app_scripts.html missing marker: {m}"
        assert m not in base, (
            f"base.html still contains {m!r} — should include _app_scripts.html instead"
        )
    assert "{% include '_app_scripts.html' %}" in base
```

**Step 2 — Run and confirm failure:**
```
.venv/bin/pytest tests/test_design_system_contract.py::test_app_scripts_partial_carries_inline_helpers -x
# expected: AssertionError — _app_scripts.html does not exist
```

**Step 3 — Implement.** Create `app/web/templates/_app_scripts.html` containing a single `<script>` wrapping the verbatim bodies of blocks 1-7 above (in order). Header comment:

```jinja
{#
  Inline app-scripts partial — shared between base.html and base_ds.html.

  Carries the runtime helpers that EVERY authed page needs to behave
  consistently:

    1. window.showUndoToast       — admin delete restore window
    2. modal Escape handler       — closes the topmost visible modal
    3. .cf-palette-row swatches   — instance-theme picker hydration
    4. `g <letter>` admin nav     — Vim-style shortcuts
    5. Cmd/Ctrl-K command palette — admin fuzzy nav
    6. .stack-tabs digit shortcut — `1`/`2`/… to switch tab
    7. admin-nav details persist  — localStorage open/closed state

  Pages opt in implicitly by extending base.html or base_ds.html;
  both layouts include this partial inline at the bottom of <body>.
#}
<script>
  /* … verbatim bodies 1-7 … */
</script>
```

In `app/web/templates/base.html` replace lines 82-653 (the entire `<script>` block, opening tag on 82, closing tag on 653) with:
```jinja
{% include '_app_scripts.html' %}
```

Leave the `{% block scripts %}{% endblock %}` at line 654 untouched.

**Step 4 — Confirm pass:**
```
.venv/bin/pytest tests/test_design_system_contract.py::test_app_scripts_partial_carries_inline_helpers -v
.venv/bin/pytest -q
```

**Step 6 — Manual smoke for unchanged functionality:**

Start dev server: `make dev` (or the project's standard command). Then:

```
# Verify scripts load (no 500s, no console errors)
curl -sI http://localhost:8000/profile | head -5   # expect 302→login or 200

# Once logged in via browser:
- /admin/users    → press `g u` quickly, expect navigation to /admin/users (verifies block 4)
- /admin/users    → Cmd-K, expect overlay (verifies block 5)
- /catalog        → press `1`, expect tab switch (verifies block 6)
- any admin page  → expand/collapse an admin-nav section, reload, expect persisted state (verifies block 7)
- /admin/marketplaces → trigger any delete → expect undo toast in lower-right (verifies block 1)
- open any modal  → Escape → expect close (verifies block 2)
```

**Step 5 — Commit:**
```
git add app/web/templates/base.html app/web/templates/_app_scripts.html tests/test_design_system_contract.py
git commit -m "refactor(web): extract inline base.html JS into _app_scripts.html"
```

---

## T10 — Migrate profile.html to extend base_ds.html

**Files:**
- Modify: `app/web/templates/profile.html` (line 1: `{% extends "base.html" %}` → `{% extends "base_ds.html" %}`)
- Modify: `app/web/templates/base_ds.html` (line ~117 — add `{% include '_app_scripts.html' %}` inside `{% block scripts %}`)

**Step 1 — Write failing test:**
```python
def test_base_ds_includes_app_scripts() -> None:
    base_ds = (TEMPLATES / "base_ds.html").read_text(encoding="utf-8")
    assert "{% include '_app_scripts.html' %}" in base_ds, (
        "base_ds.html must include the shared _app_scripts.html partial"
    )


def test_profile_extends_base_ds() -> None:
    profile = (TEMPLATES / "profile.html").read_text(encoding="utf-8")
    assert profile.lstrip().startswith('{% extends "base_ds.html" %}'), (
        "profile.html must extend base_ds.html (proof-of-adoption page)"
    )
```

**Step 2 — Run and confirm both fail.**

**Step 3 — Implement.**

In `app/web/templates/base_ds.html` BEFORE the closing `{% block scripts %}{% endblock %}` (line 117 area) — insert the include so the shared helpers run on every base_ds adopter:
```jinja
{# Shared app-scripts (undo toast, modal Esc, palette swatches, admin
   shortcuts, Cmd-K palette, stack-tabs shortcuts, admin-nav persist) #}
{% include '_app_scripts.html' %}
{% block scripts %}{% endblock %}
```

In `app/web/templates/profile.html` line 1:
```jinja
{% extends "base_ds.html" %}
```

**Step 4 — Confirm pass:**
```
.venv/bin/pytest tests/test_design_system_contract.py -v
.venv/bin/pytest -q
```

**Step 6 — Manual smoke for /profile:**
```
# After dev server up + logged in:
- Navigate to /profile
- Confirm header renders identically to the old base.html version
- Confirm flash messages render (trigger one if possible)
- Open browser devtools console → no errors
- Press Escape after opening any nested modal/details → expected close behavior
- Click any admin link in the header to confirm header wiring unaffected
- View page source → confirm CSS load order: style-custom → design-tokens → components → stack_card
- View page source → confirm `_app_scripts.html` IIFE markers present (search for "showUndoToast")
```

**Step 5 — Commit:**
```
git add app/web/templates/profile.html app/web/templates/base_ds.html tests/test_design_system_contract.py
git commit -m "refactor(web): migrate profile.html to base_ds.html as proof of adoption"
```

---

## Macro tasks T11-T17 — shared shape

Each macro task follows the same skeleton:

1. **Define the macro** at the bottom of `app/web/templates/_components.html`, with a doc comment block listing: variants, args, example call, intentional-page-local notes.
2. **Add backing CSS** to `app/web/static/style-custom.css` (or, where the rule already exists in `css/marketplace.css` / `css/stack_card.css`, simply confirm so and add a comment in `_components.html` pointing to the existing file).
3. **Migrate one adopter file** to call the macro.
4. **Add a macro-render test** that the macro produces the expected static class set.
5. **Append the macro's constructed variant tokens** to `T08`'s `constructed` set so the class-coverage test stays accurate.
6. **TODO in `_components.html`**: list the OTHER adopters that the macro could absorb in a later sweep.

---

## T11 — Macro `tabs_rich` + adopter marketplace.html

**Goal:** Macro for tabs with rich body (inline SVG icon + label + count badge per item). Backed by the existing `.mp-tabs` (in `app/web/static/css/marketplace.css:102-142`) and `.stack-tabs` (in `app/web/static/css/stack_card.css:221-281`). New macro EMITS `.mp-tabs` markup; adopters that need the navy stack-tab variant pass `variant='stack'` which emits `.stack-tabs` instead.

**Files:**
- Modify: `app/web/templates/_components.html` (append macro)
- Modify: `app/web/templates/marketplace.html` (lines 57-94 — replace the `.mp-tabs` block)

**Step 1 — Write failing test** (add `tests/test_web_components_macros.py` — NEW file):
```python
"""Render tests for the design-system component macros."""
from __future__ import annotations

from jinja2 import Environment, FileSystemLoader


def _env():
    return Environment(loader=FileSystemLoader("app/web/templates"),
                       autoescape=True)


def test_tabs_rich_emits_mp_tabs_with_icons_and_counts():
    src = """
    {% import '_components.html' as ds %}
    {{ ds.tabs_rich(
        items=[
            {'label': 'Curated', 'data_tab': 'curated', 'active': True,
             'count_attr': 'data-count-curated',
             'svg': '<svg class="tab-icon"></svg>'},
            {'label': 'Flea', 'data_tab': 'flea', 'active': False,
             'count_attr': 'data-count-flea',
             'svg': '<svg class="tab-icon"></svg>'},
        ],
        aria_label='Marketplace sections',
    ) }}
    """
    out = _env().from_string(src).render()
    assert 'class="mp-tabs"' in out
    assert 'role="tablist"' in out
    assert 'aria-label="Marketplace sections"' in out
    assert 'data-tab="curated"' in out
    assert 'aria-selected="true"' in out
    assert 'class="count" data-count-curated' in out
    # icon SVG passed through |safe
    assert '<svg class="tab-icon"></svg>' in out


def test_tabs_rich_stack_variant_emits_stack_tabs():
    src = """
    {% import '_components.html' as ds %}
    {{ ds.tabs_rich(items=[{'label': 'X', 'data_tab': 'x', 'active': True}],
                    variant='stack') }}
    """
    out = _env().from_string(src).render()
    assert 'class="stack-tabs"' in out
```

**Step 2 — Run and confirm failure:**
```
.venv/bin/pytest tests/test_web_components_macros.py -x
# expected: UndefinedError or AttributeError — `ds.tabs_rich` does not exist
```

**Step 3 — Implement macro** at bottom of `_components.html`:

```jinja
{# ─────────────────────────────────────────────────────────────────────
   Tabs (rich) — tab strip with inline SVG icon + count badge per item.

   Backed by `.mp-tabs` (default; canonical, used by marketplace.html)
   and `.stack-tabs` (variant='stack'; navy surface used by catalog.html
   and corporate_memory.html). CSS lives in:
     - app/web/static/css/marketplace.css:102-142
     - app/web/static/css/stack_card.css:221-281

   Each item dict:
     label       text label (required)
     data_tab    value for data-tab attribute (JS hook)
     active      bool — adds is-active + aria-selected=true
     count_attr  optional — data-attribute name on the inner .count span
                 (e.g. 'data-count-curated'). The macro does NOT render
                 a count value — the page's JS populates it.
     svg         optional — raw SVG markup (piped through |safe)

   `variant`:    'mp' (default) | 'stack'

   TODO follow-up sweep adopters (not migrated by T11):
     - catalog.html        (.stack-tabs at lines 69-99)
     - corporate_memory.html (.stack-tabs at lines 67-91)
   ───────────────────────────────────────────────────────────────────── #}
{% macro tabs_rich(items=[], aria_label='Sections', variant='mp') -%}
  {%- set root = 'mp-tabs' if variant == 'mp' else 'stack-tabs' -%}
<div class="{{ root }}" role="tablist" aria-label="{{ aria_label }}">
  {%- for item in items %}
  <button type="button" role="tab"
          class="{% if item.active %}is-active{% endif %}"
          {%- if item.data_tab %} data-tab="{{ item.data_tab }}"{% endif -%}
          aria-selected="{{ 'true' if item.active else 'false' }}">
    {%- if item.svg %}{{ item.svg | safe }}{% endif -%}
    {{ item.label }}
    {%- if item.count_attr %}
    <span class="count" {{ item.count_attr }}>0</span>
    {%- endif %}
  </button>
  {%- endfor %}
</div>
{%- endmacro %}
```

**Migrate marketplace.html lines 57-83** (the `.mp-tabs` block, NOT the surrounding `.mp-tabs-row`):

The SVG paths are long. Either (a) leave the SVG markup in the template and pass it into `item.svg`, or (b) extract a `_marketplace_icons.html` partial. Pick (a) for minimal disruption — pass each SVG as raw markup.

Replace lines 58-83:
```jinja
{{ ds.tabs_rich(
    aria_label='Marketplace sections',
    items=[
      {'label': 'Curated Marketplace', 'data_tab': 'curated', 'active': True,
       'count_attr': 'data-count-curated',
       'svg': '<svg class="tab-icon" viewBox="0 0 24 24" fill="none" '
              'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" '
              'stroke-linejoin="round" aria-hidden="true">'
              '<path d="M9 12.75 11.25 15 15 9.75m-3-7.036A11.959 11.959 0 0 1 3.598 6 11.99 11.99 0 0 0 3 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.248-8.25-3.285Z"/></svg>'},
      {'label': 'Flea Market', 'data_tab': 'flea', 'active': False,
       'count_attr': 'data-count-flea',
       'svg': '<svg class="tab-icon" viewBox="0 0 24 24" fill="none" '
              'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" '
              'stroke-linejoin="round" aria-hidden="true">'
              '<path d="M13.5 21v-7.5a.75.75 0 0 1 .75-.75h3a.75.75 0 0 1 .75.75V21m-4.5 0H2.36m11.14 0H18m0 0h3.64m-1.39 0V9.349M3.75 21V9.349m0 0a3.001 3.001 0 0 0 3.75-.615A2.993 2.993 0 0 0 9.75 9.75c.896 0 1.7-.393 2.25-1.016a2.993 2.993 0 0 0 2.25 1.016c.896 0 1.7-.393 2.25-1.016a3.001 3.001 0 0 0 3.75.614m-16.5 0a3.004 3.004 0 0 1-.621-4.72L4.318 3.44A1.5 1.5 0 0 1 5.378 3h13.243a1.5 1.5 0 0 1 1.06.44l1.19 1.189a3 3 0 0 1-.621 4.72M6.75 18h3.75a.75.75 0 0 0 .75-.75V13.5a.75.75 0 0 0-.75-.75H6.75a.75.75 0 0 0-.75.75v3.75c0 .414.336.75.75.75Z"/></svg>'},
      {'label': 'My Stack', 'data_tab': 'my', 'active': False,
       'count_attr': 'data-count-my',
       'svg': '<svg class="tab-icon" viewBox="0 0 24 24" fill="none" '
              'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" '
              'stroke-linejoin="round" aria-hidden="true">'
              '<path d="M6.429 9.75 2.25 12l4.179 2.25m0-4.5 5.571 3 5.571-3m-11.142 0L2.25 7.5 12 2.25l9.75 5.25-4.179 2.25m0 0L21.75 12l-4.179 2.25m0 0 4.179 2.25L12 21.75 2.25 16.5l4.179-2.25m11.142 0-5.571 3-5.571-3"/></svg>'},
    ]
) }}
```

Confirm marketplace.html still imports `ds`:
```
grep -n "import '_components.html'" app/web/templates/marketplace.html
```
If not imported, add at the top after `{% extends %}`: `{% import '_components.html' as ds %}`.

**Step 4 — Confirm pass:**
```
.venv/bin/pytest tests/test_web_components_macros.py -v
.venv/bin/pytest -q
```

**Update T08 constructed set:** add `mp-tabs`, `stack-tabs`, `tab-icon`, `count` to the `constructed` set in the class-coverage test.

**Step 5 — Commit:**
```
git add app/web/templates/_components.html app/web/templates/marketplace.html tests/test_web_components_macros.py tests/test_design_system_contract.py
git commit -m "feat(web): tabs_rich macro + adopt in marketplace.html"
```

---

## T12 — Macro `segmented_strip` + adopter home_not_onboarded.html

**Goal:** Macro for the dark-surface segmented strips used by `.os-tabs` (operating-system tabs in setup-wizard pages) and `.mode-tabs` (permission-level switch in `home_not_onboarded.html` line 302 and `install.html` various lines).

The visual contract is: pill-shape segmented control with active pill highlighted. CSS rules already exist in `app/web/static/css/home.css:294, 2042, 2079`. The macro renders the markup; no new CSS required.

**Files:**
- Modify: `app/web/templates/_components.html` (append)
- Modify: `app/web/templates/home_not_onboarded.html` (lines 302-310 — `.mode-tabs` adopter; ONE concrete adopter as required)

**Step 1 — Write failing test:**
```python
def test_segmented_strip_emits_os_tabs_or_mode_tabs():
    src = """
    {% import '_components.html' as ds %}
    {{ ds.segmented_strip(
        items=[
            {'label': 'macOS', 'value': 'mac', 'active': True},
            {'label': 'Linux', 'value': 'linux', 'active': False},
        ],
        variant='os',
        aria_label='Operating system',
    ) }}
    """
    out = _env().from_string(src).render()
    assert 'class="os-tabs"' in out
    assert 'role="tablist"' in out
    assert 'data-os="mac"' in out
    assert 'aria-selected="true"' in out


def test_segmented_strip_mode_variant():
    src = """
    {% import '_components.html' as ds %}
    {{ ds.segmented_strip(items=[{'label': 'User', 'value': 'user', 'active': True}],
                          variant='mode', aria_label='Permission level') }}
    """
    out = _env().from_string(src).render()
    assert 'class="mode-tabs"' in out
    assert 'data-mode="user"' in out
```

**Step 2-4 — Implement macro:**
```jinja
{# ─────────────────────────────────────────────────────────────────────
   Segmented strip — dark-surface segmented control.

   Backed by `.os-tabs` and `.mode-tabs` rules in
   `app/web/static/css/home.css`. The macro emits the markup; the
   parent page sets up the JS handler that flips `is-active` and
   reveals the matching panel.

   Item dict:  {label, value, active}
   variant:    'os' → emits .os-tabs + data-os="<value>"
               'mode' → emits .mode-tabs + data-mode="<value>"

   TODO follow-up sweep adopters (not migrated by T12):
     - home_not_onboarded.html lines 103, 145, 187, 312, 336 (.os-tabs strips)
     - install.html lines (verify; multiple .os-tabs)
   ───────────────────────────────────────────────────────────────────── #}
{% macro segmented_strip(items=[], variant='os', aria_label='Sections') -%}
  {%- if variant == 'mode' %}
    {%- set root = 'mode-tabs' -%}
    {%- set data_attr = 'data-mode' -%}
  {%- else %}
    {%- set root = 'os-tabs' -%}
    {%- set data_attr = 'data-os' -%}
  {%- endif %}
<div class="{{ root }}" role="tablist" aria-label="{{ aria_label }}">
  {%- for item in items %}
  <button type="button" role="tab"
          class="{% if item.active %}is-active{% endif %}"
          {{ data_attr }}="{{ item.value }}"
          aria-selected="{{ 'true' if item.active else 'false' }}">
    {{ item.label }}
  </button>
  {%- endfor %}
</div>
{%- endmacro %}
```

**Migrate `home_not_onboarded.html` line 302** — read the existing `.mode-tabs` block to capture each button label/value, then replace with:
```jinja
{{ ds.segmented_strip(
    aria_label='Permission level',
    variant='mode',
    items=[
        {'label': '<extract from current markup>', 'value': '<...>', 'active': True},
        ...
    ]
) }}
```

(Sub-agent: open `home_not_onboarded.html` at line 302 first, transcribe each button into the dict list 1:1, including the `active` flag of the currently-marked button.)

**Update T08 constructed set:** add `os-tabs`, `mode-tabs`.

**Step 5 — Commit:**
```
git add app/web/templates/_components.html app/web/templates/home_not_onboarded.html tests/test_web_components_macros.py tests/test_design_system_contract.py
git commit -m "feat(web): segmented_strip macro + adopt in home_not_onboarded.html"
```

---

## T13 — Macro `pill_chip` + adopter catalog.html

**Goal:** Pill-shaped filter chip. Backed by existing `.pill` CSS in `app/web/static/css/marketplace.css:211-226` and `app/web/static/css/stack_card.css:292-312`. Adopters: catalog.html (line 119-120 — `.pill is-active` / `.pill` filter row), marketplace.html (line 150-153 — type filter), corporate_memory.html (similar).

**Files:**
- Modify: `app/web/templates/_components.html`
- Modify: `app/web/templates/catalog.html` (lines 119-120 — filter row; the simplest adopter)

**Step 1 — Write failing test:**
```python
def test_pill_chip_emits_pill_with_is_active():
    src = """
    {% import '_components.html' as ds %}
    {{ ds.pill_chip(label='All', data_filter='all', active=True) }}
    {{ ds.pill_chip(label='Required', data_filter='required') }}
    """
    out = _env().from_string(src).render()
    assert 'class="pill is-active"' in out
    assert 'data-filter="all"' in out
    assert 'class="pill"' in out
    assert 'data-filter="required"' in out
```

**Step 3 — Implement:**
```jinja
{# ─────────────────────────────────────────────────────────────────────
   Pill chip — filter-row pill button.

   Backed by `.pill` (marketplace.css:211-226, stack_card.css:292-312).
   Renders a <button> for keyboard accessibility (filter rows are JS-
   driven panel switchers, not navigation).

   Args:
     label         visible text (required)
     data_filter   data-filter attribute (filter hook for JS)
     data_type     data-type attribute (alt hook used by marketplace type pills)
     active        bool → adds is-active
     count         optional integer or string rendered inside <span class="count">

   TODO follow-up sweep adopters (not migrated by T13):
     - marketplace.html lines 150-153 (.pill type filter)
     - corporate_memory.html (verify via grep)
   ───────────────────────────────────────────────────────────────────── #}
{% macro pill_chip(label='', data_filter=None, data_type=None,
                   active=False, count=None) -%}
<button type="button"
        class="pill{% if active %} is-active{% endif %}"
        {%- if data_filter %} data-filter="{{ data_filter }}"{% endif -%}
        {%- if data_type %} data-type="{{ data_type }}"{% endif -%}>
  {{ label }}
  {%- if count is not none %}
  <span class="count">{{ count }}</span>
  {%- endif %}
</button>
{%- endmacro %}
```

**Migrate catalog.html lines 119-120.** Read the surrounding `.stack-filter-row` block first; replace the two literal `<button class="pill">` lines with two `ds.pill_chip(...)` calls.

**Update T08 constructed set:** add `pill`.

**Step 5 — Commit:**
```
git add app/web/templates/_components.html app/web/templates/catalog.html tests/test_web_components_macros.py tests/test_design_system_contract.py
git commit -m "feat(web): pill_chip macro + adopt in catalog.html"
```

---

## T14 — Macro `kpi_card` + adopter admin_sessions.html

**Goal:** Clickable KPI cards rendered as `<button>` for native keyboard nav. Backed by `.obs-kpi` (activity_center.css:79-87). Adopters: activity_center.html, admin_sessions.html, admin_usage.html.

**Files:**
- Modify: `app/web/templates/_components.html`
- Modify: `app/web/templates/admin_sessions.html` (lines 43-46 first card; do all 3 cards in the same block)

**Step 1 — Write failing test:**
```python
def test_kpi_card_emits_obs_kpi_button():
    src = """
    {% import '_components.html' as ds %}
    {{ ds.kpi_card(label='Events', value_id='kpi-events',
                   data_filter='', aria_label='All events') }}
    """
    out = _env().from_string(src).render()
    assert 'class="obs-kpi"' in out
    assert '<button' in out
    assert 'data-filter=""' in out
    assert 'aria-label="All events"' in out
    assert 'class="obs-kpi-label"' in out
    assert 'id="kpi-events"' in out
```

**Step 3 — Implement:**
```jinja
{# ─────────────────────────────────────────────────────────────────────
   KPI card — clickable headline metric.

   Backed by `.obs-kpi` + `.obs-kpi-label` + `.obs-kpi-value` +
   `.obs-kpi-sub` (activity_center.css:79-87). Renders a <button>
   so keyboard users can navigate via Tab + Enter — no role gymnastics.

   Args:
     label       small uppercase caption (required)
     value_id    DOM id on the value span; page JS writes the number here
     data_filter optional data-filter or data-quick attribute (JS hook)
     data_quick  alternative hook used by admin_sessions/admin_usage
     aria_label  long-form label for screen readers
     sub_id      optional id on a third .obs-kpi-sub line

   TODO follow-up sweep adopters (not migrated by T14):
     - activity_center.html (.obs-kpi at lines 52-…)
     - admin_usage.html (.obs-kpi)
   ───────────────────────────────────────────────────────────────────── #}
{% macro kpi_card(label='', value_id=None, data_filter=None, data_quick=None,
                  aria_label=None, sub_id=None) -%}
<button class="obs-kpi" type="button"
        {%- if data_filter is not none %} data-filter="{{ data_filter }}"{% endif -%}
        {%- if data_quick is not none %} data-quick="{{ data_quick }}"{% endif -%}
        {%- if aria_label %} aria-label="{{ aria_label }}"{% endif -%}>
  <span class="obs-kpi-label">{{ label }}</span>
  <span class="obs-kpi-value"{% if value_id %} id="{{ value_id }}"{% endif %}>—</span>
  {%- if sub_id %}<span class="obs-kpi-sub" id="{{ sub_id }}"></span>{% endif %}
</button>
{%- endmacro %}
```

**Migrate admin_sessions.html lines 42-…** Replace each `<button class="obs-kpi" data-quick="X">…</button>` with `ds.kpi_card(label=…, value_id=…, data_quick=…)`. Verify lines 43-56 inclusive.

**Update T08 constructed set:** add `obs-kpi`, `obs-kpi-label`, `obs-kpi-value`, `obs-kpi-sub`.

**Step 5 — Commit:**
```
git add app/web/templates/_components.html app/web/templates/admin_sessions.html tests/test_web_components_macros.py tests/test_design_system_contract.py
git commit -m "feat(web): kpi_card macro + adopt in admin_sessions.html"
```

---

## T15 — Macro `hero_search_btn` + adopter marketplace.html

**Goal:** Hero search-row button. Backed by `.search-btn` (marketplace.css:67-75) and `.stack-hero__search-btn` (stack_card.css:127-142). Marketplace + catalog + corporate_memory.

**Files:**
- Modify: `app/web/templates/_components.html`
- Modify: `app/web/templates/marketplace.html` (line 44: `<button class="search-btn" id="mp-search-btn" …>`)

**Step 1 — Write failing test:**
```python
def test_hero_search_btn_emits_search_btn():
    src = """
    {% import '_components.html' as ds %}
    {{ ds.hero_search_btn(id='mp-search-btn', label='Search') }}
    """
    out = _env().from_string(src).render()
    assert 'class="search-btn"' in out
    assert 'id="mp-search-btn"' in out
    assert 'type="button"' in out


def test_hero_search_btn_stack_variant():
    src = """
    {% import '_components.html' as ds %}
    {{ ds.hero_search_btn(id='memory-search-btn', label='Search',
                          variant='stack') }}
    """
    out = _env().from_string(src).render()
    assert 'class="stack-hero__search-btn"' in out
```

**Step 3 — Implement:**
```jinja
{# ─────────────────────────────────────────────────────────────────────
   Hero search-row button — sits inside a hero block, fires the row's
   search input. Two variants share the same shape:
     - variant='mp'    → .search-btn (marketplace.css:67-75)
     - variant='stack' → .stack-hero__search-btn (stack_card.css:127-142)

   Args:
     id        DOM id (required — page JS attaches the click handler)
     label     visible text (default 'Search')
     variant   'mp' (default) | 'stack'

   TODO follow-up sweep adopters (not migrated by T15):
     - catalog.html line 37 (.stack-hero__search-btn)
     - corporate_memory.html line 29 (.stack-hero__search-btn)
   ───────────────────────────────────────────────────────────────────── #}
{% macro hero_search_btn(id=None, label='Search', variant='mp') -%}
  {%- set cls = 'search-btn' if variant == 'mp' else 'stack-hero__search-btn' -%}
<button type="button" class="{{ cls }}"{% if id %} id="{{ id }}"{% endif %}>{{ label }}</button>
{%- endmacro %}
```

**Migrate marketplace.html line 44:**
```jinja
{{ ds.hero_search_btn(id='mp-search-btn') }}
```

**Update T08 constructed set:** add `search-btn`, `stack-hero__search-btn`.

**Step 5 — Commit:**
```
git add app/web/templates/_components.html app/web/templates/marketplace.html tests/test_web_components_macros.py tests/test_design_system_contract.py
git commit -m "feat(web): hero_search_btn macro + adopt in marketplace.html"
```

---

## T16 — Macro `info_panel_accent` + adopter marketplace.html

**Goal:** Custom-accent info panel (`.mp-curator-block`). Three accents exist: default (curated, no extra class), `.is-flea` (purple), `.is-mystack` (slate). Backed by CSS at `app/web/static/css/marketplace.css:175-…`.

**Files:**
- Modify: `app/web/templates/_components.html`
- Modify: `app/web/templates/marketplace.html` (lines 100-135 — three `.mp-curator-block` divs)

**Step 1 — Write failing test:**
```python
def test_info_panel_accent_default():
    src = """
    {% import '_components.html' as ds %}
    {% call ds.info_panel_accent(title='Trust', show_on='curated') %}
      <a class="link" href="/x">More →</a>
    {% endcall %}
    """
    out = _env().from_string(src).render()
    assert 'class="mp-curator-block"' in out
    assert 'data-show-on="curated"' in out
    assert '<div class="title">Trust</div>' in out
    assert '/x' in out


def test_info_panel_accent_flea_variant():
    src = """
    {% import '_components.html' as ds %}
    {% call ds.info_panel_accent(accent='flea', title='Open', show_on='flea') %}
      body
    {% endcall %}
    """
    out = _env().from_string(src).render()
    assert 'class="mp-curator-block is-flea"' in out
```

**Step 3 — Implement:**
```jinja
{# ─────────────────────────────────────────────────────────────────────
   Info panel (accent) — page-local info block with a coloured left
   border. Three accents:
     - accent=None (default)   → curated/blue (canonical)
     - accent='flea'           → violet  (.is-flea)
     - accent='mystack'        → slate   (.is-mystack)

   The accents intentionally fall OUTSIDE the canonical
   --ds-accent-{info,success,warn,danger} vocabulary because they
   signal Marketplace SHELVES, not status. CSS lives in
   `app/web/static/css/marketplace.css:175-…`.

   Args:
     title      <div class="title"> heading text (required)
     body       OR pass body via {% call %} block
     show_on    optional data-show-on attribute (JS visibility filter)
     accent     None | 'flea' | 'mystack'

   TODO follow-up sweep adopters (not migrated by T16):
     - catalog.html (mirrors mp-curator-block pattern; verify)
     - corporate_memory.html (mirrors mp-curator-block pattern; verify)
   ───────────────────────────────────────────────────────────────────── #}
{% macro info_panel_accent(title='', body=None, show_on=None, accent=None) -%}
  {%- set classes = ['mp-curator-block'] -%}
  {%- if accent %}{%- set _ = classes.append('is-' ~ accent) -%}{%- endif %}
<div class="{{ classes | join(' ') }}"
     {%- if show_on %} data-show-on="{{ show_on }}"{% endif -%}>
  <div class="text">
    {%- if title %}<div class="title">{{ title }}</div>{% endif %}
    {%- if body %}<div class="body">{{ body }}</div>{% endif %}
  </div>
  {%- if caller %}{{ caller() }}{% endif %}
</div>
{%- endmacro %}
```

**Migrate marketplace.html lines 100-108** (the `data-show-on="curated"` block) as the proof-of-adoption — the other two are TODO. Read the existing body text verbatim and pass into `body`:
```jinja
{% call ds.info_panel_accent(
    show_on='curated',
    title='Each plugin here has a named curator accountable for it.',
    body='Each plugin in this marketplace has a named curator and meets a baseline review bar (security, telemetry hygiene, documentation).') %}
  {% if curators_url %}
  <a class="link" href="{{ curators_url }}" target="_blank" rel="noopener">See all curators →</a>
  {% endif %}
{% endcall %}
```

**Update T08 constructed set:** add `mp-curator-block`, `is-flea`, `is-mystack`, `title`, `body`, `text`.

**Step 5 — Commit:**
```
git add app/web/templates/_components.html app/web/templates/marketplace.html tests/test_web_components_macros.py tests/test_design_system_contract.py
git commit -m "feat(web): info_panel_accent macro + adopt curated block in marketplace.html"
```

---

## T17 — Macro `code_chip` (no adopter; documented as page-local)

**Goal:** Macro added so the design-system vocabulary is complete; per #419 body and `system.md`, this pattern is intentionally page-local TODAY (admin_workspace_prompt.html, admin_welcome.html). Macro is shipped + documented; adoption is deferred.

**Files:**
- Modify: `app/web/templates/_components.html`

**Step 1 — Write failing test:**
```python
def test_code_chip_emits_btn_copy():
    src = """
    {% import '_components.html' as ds %}
    {{ ds.code_chip(target_id='placeholder-text') }}
    """
    out = _env().from_string(src).render()
    assert 'class="btn-copy"' in out
    assert 'data-copy-target="placeholder-text"' in out
    assert 'Copy' in out
```

**Step 3 — Implement:**
```jinja
{# ─────────────────────────────────────────────────────────────────────
   Code chip / copy button.

   INTENTIONALLY low-adoption today: the `.btn-copy` family lives
   page-local in admin_workspace_prompt.html + admin_welcome.html
   because the dark Catppuccin chrome (`#89b4fa`, `#a6e3a1`) is
   page-scoped, not a system token. Per `.interface-design/system.md`:
   if a future scope shift makes the dark surface canonical, sweep the
   page-local CSS into style-custom.css and turn this macro into the
   default render path.

   This macro is therefore the placeholder shape; calling pages stay
   on their own CSS until the canonicalisation lands.

   Args:
     target_id   id of the element whose innerText is copied (required)
     label       button text (default 'Copy')

   TODO future canonicalisation:
     - admin_workspace_prompt.html (lines 81-95, btn-copy CSS rules)
     - admin_welcome.html
   ───────────────────────────────────────────────────────────────────── #}
{% macro code_chip(target_id='', label='Copy') -%}
<button type="button" class="btn-copy" data-copy-target="{{ target_id }}">{{ label }}</button>
{%- endmacro %}
```

No adopter migration. Document the no-adoption explicitly in the macro docstring (done above).

**Step 5 — Commit:**
```
git add app/web/templates/_components.html tests/test_web_components_macros.py
git commit -m "feat(web): code_chip macro (page-local pattern; no adopter yet)"
```

---

## T18 — Manual smoke checklist (docs only)

**Files:**
- Modify: `docs/superpowers/plans/2026-05-27-design-system-419-followups.md` (append)

**Purpose:** A run-book the reviewer follows once locally before merging the PR. No code changes.

Append to the plan file:
```
## Manual smoke checklist

Run `make dev` (or the project's standard dev-server command). For each
URL below: load it, scan the browser devtools console for errors, and
spot-check the listed feature.

URL                              | Spot-check
---------------------------------|-----------------------------------------
/profile                         | Loads under base_ds.html. Esc closes any
                                   modal. Logout button works.
/marketplace?tab=curated         | mp-tabs strip renders with icons + counts.
                                   The mp-curator-block (default accent) shows
                                   trust copy + "See all curators" link.
                                   Search button (id=mp-search-btn) fires.
/marketplace?tab=flea            | Switching tabs via the strip works (no
                                   regression from tabs_rich macro).
/catalog                         | Filter row pills render (.pill / .pill is-active).
                                   `1` key switches to first tab (stack-tabs
                                   shortcut still wired via _app_scripts.html).
/me/activity                     | No raw hex visible in page CSS. Badges
                                   render in correct status colours
                                   (warn=amber, info=blue, success=green).
/setup                           | Step dots render at correct active colour
                                   (--ds-primary). Form inputs show border.
/home (not onboarded)            | mode-tabs strip renders (Permission level).
/admin/sessions                  | KPI cards (obs-kpi) keyboard-focusable;
                                   Enter triggers the quick-filter.
/admin/users                     | `g u` shortcut still works (admin nav from
                                   _app_scripts.html).
/admin/* (any)                   | Cmd-K opens command palette overlay.
/admin/marketplaces              | Trigger a soft-delete → undo toast appears
                                   in lower-right (showUndoToast still works).

If any of the above fails, do NOT merge. File a follow-up under #419 or
file a new bug — never mask a regression.
```

**Step 5 — Commit:**
```
git add docs/superpowers/plans/2026-05-27-design-system-419-followups.md
git commit -m "docs(web): manual smoke checklist for #419 follow-up PR"
```

---

## Self-review walk-through (issue body bullet → T-task)

| #419 open checkbox | T-task |
|---|---|
| Should-fix: test pin sweep (22 assertions) | T01 helper + T02 (6) + T03 (17) |
| Should-fix: base_ds.html adoption = 0 (extract JS + migrate profile) | T09 + T10 |
| Could-improve: hex sweep — profile (3) | T05 |
| Could-improve: hex sweep — setup (17) | T06 |
| Could-improve: hex sweep — me_activity (16) | T07 |
| Could-improve: var(--primary) → var(--ds-primary) (18 templates) | T04 |
| Could-improve: CI class-coverage contract test | T08 |
| Macro gap: tabs_rich (.mp-tabs/.stack-tabs/.os-tabs) | T11 |
| Macro gap: segmented_strip (.os-tabs dark / .mode-tabs) | T12 |
| Macro gap: pill_chip (.pill) | T13 |
| Macro gap: kpi_card (.obs-kpi) | T14 |
| Macro gap: hero_search_btn (.search-btn / .stack-hero__search-btn) | T15 |
| Macro gap: info_panel_accent (.mp-curator-block) | T16 |
| Macro gap: code_chip (.btn-copy — page-local placeholder) | T17 |
| (Cross-cut) Smoke checklist | T18 |

All 14 open checkboxes (and the cross-cut smoke) mapped → no orphans.

---

## Critical files for implementation

- `app/web/templates/_components.html`
- `app/web/templates/base.html`
- `app/web/templates/base_ds.html`
- `app/web/static/css/design-tokens.css`
- `tests/test_design_system_contract.py`
