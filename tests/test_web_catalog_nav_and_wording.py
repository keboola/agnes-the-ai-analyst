"""Two accidents of a shared default, on two different pages.

**#1207** — `style-custom.css` carried a bare `nav { display: flex }`. Written
for the header, a bare element selector reaches every `<nav>`, so any nav that
did not declare its own `display` silently got a horizontal row. The semantic
layer's category sidebar was the visible casualty (buttons laid out sideways
and clipped by an `overflow: hidden` parent); `/admin/server-config`'s section
nav was a quieter one — centred, shrink-wrapped links instead of full-width
rows. Both are fixed by deleting the rule, which an audit showed gives the
header nothing it does not already declare.

**#1206** — after the auto-membership reshape, `in_stack` on a catalog entry
means "a local copy exists", not "in the caller's stack". The key name still
says the old thing, so the card macro read it the old way and offered "Add to
stack" on a package listed under My Stack. The projection now states the
semantics (`in_stack_is_local`) and the macro reads the flag.

Both are wording/layout defects with no server behaviour to assert, so these
are template- and asset-level checks. The geometric half of #1207 was verified
in a real browser (categories stacked, `/admin/server-config` links full-width)
— what a test can hold is that the rule stays gone.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app" / "web" / "templates"
STATIC = ROOT / "app" / "web" / "static"


# ── #1207: the global rule stays gone ───────────────────────────────────────


def test_no_bare_nav_element_selector():
    """A bare `nav { … }` is a default every future nav inherits without asking.

    Matching the selector at the start of a rule (not `.x nav`, not `nav.x`),
    which is the shape that reaches everything.
    """
    css = (STATIC / "style-custom.css").read_text(encoding="utf-8")
    # Strip comments so the explanatory block above the deletion cannot match.
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    offenders = re.findall(r"(?m)^\s*nav\s*\{", css)
    assert not offenders, (
        "bare `nav { … }` is back in style-custom.css — it applies to every <nav> "
        "in the app and silently re-lays-out any that declares no display (#1207)"
    )


def test_category_sidebar_declares_its_own_layout():
    """`.sl-cat-nav` states column flow rather than inheriting whatever a
    global happens to say — the condition that made it breakable."""
    text = (TEMPLATES / "catalog_semantics.html").read_text(encoding="utf-8")
    rule = re.search(r"\.sl-cat-nav\s*\{([^}]*)\}", text)
    assert rule, ".sl-cat-nav rule disappeared"
    body = rule.group(1)
    assert "display: flex" in body
    assert "flex-direction: column" in body


@pytest.mark.parametrize(
    ("template", "selector"),
    [
        ("catalog_semantics.html", ".sl-cat-nav"),
        ("admin_server_config.html", ".cfg-sidenav"),
    ],
)
def test_navs_that_relied_on_the_global_now_declare_display(template, selector):
    """The two navs the audit found declaring no `display` of their own. With
    the global gone they must say what they are, or they fall back to block
    flow by accident rather than by decision."""
    text = (TEMPLATES / template).read_text(encoding="utf-8")
    rules = re.findall(rf"{re.escape(selector)}\s*\{{([^}}]*)\}}", text)
    assert rules, f"{selector} rule not found in {template}"
    assert any("display" in r for r in rules), f"{selector} still declares no display"


# ── #1206: the wording follows the flag, not the key name ───────────────────


def _render_card(**entry):
    """Render the legacy `card` macro against one entry dict.

    The macro is loaded from disk with its real filename, so this exercises the
    shipped template rather than a copy.
    """
    # FileSystemLoader rooted at the real templates dir — the macro imports
    # siblings (`macros/_trustmark.html`), so a standalone loader cannot render
    # it, and stubbing those imports would be testing a copy.
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)
    tpl = env.from_string('{% from "macros/_stack_card.html" import card %}{{ card(entry) }}')
    base = {"id": "pkg1", "name": "Sales", "description": "d", "drilldown_url": "/x"}
    base.update(entry)
    return tpl.render(entry=base)


def test_downloaded_package_says_downloaded():
    """The reported contradiction: a package under My Stack whose own button
    invited you to add it to your stack."""
    html = _render_card(in_stack=True, in_stack_is_local=True)
    assert "Downloaded" in html
    assert "Remove local copy" in html
    assert "In stack" not in html
    assert "Add to stack" not in html


def test_not_downloaded_package_offers_the_download():
    html = _render_card(in_stack=False, in_stack_is_local=True)
    assert "Download locally" in html
    assert "Add to stack" not in html


def test_required_package_says_downloaded_required():
    html = _render_card(in_stack=True, in_stack_is_local=True, requirement="required")
    assert "Downloaded (required)" in html


def test_projections_without_the_flag_keep_the_old_wording():
    """The flag is opt-in so a consumer that never re-pointed `in_stack` — it
    still means stack membership there — is untouched by this change."""
    html = _render_card(in_stack=True)
    assert "In stack" in html
    assert "Downloaded" not in html

    html = _render_card(in_stack=False)
    assert "Add to stack" in html
    assert "Download locally" not in html


def test_both_projections_declare_the_local_semantics():
    """`_data_package_entry_dict` and `_memory_domain_entry_dict` both re-point
    `in_stack` at `entry.materialized`; each must therefore ship the flag, or
    its cards read the old meaning."""
    router = (ROOT / "app" / "web" / "router.py").read_text(encoding="utf-8")
    repoints = router.count('"in_stack": getattr(entry, "materialized", False),')
    flags = router.count('"in_stack_is_local": True,')
    assert repoints, "the re-point disappeared — revisit this guard"
    assert flags == repoints, (
        f"{repoints} projections re-point `in_stack` at the local-copy state but only "
        f"{flags} say so — the cards on the unflagged one read the old meaning (#1206)"
    )


def test_the_js_twin_matches_the_macro():
    """`catalog.html` re-labels cards client-side after an add/remove. Left on
    the old strings it would undo the fix on the first click."""
    text = (TEMPLATES / "catalog.html").read_text(encoding="utf-8")
    assert "'Downloaded'" in text
    assert "'Remove local copy' : 'Download locally'" in text
    assert 'data-filter="in_stack">Downloaded<' in text, "the filter chip still reads 'In stack'"
