"""The Catalog's card wording follows the data, not the key name (#1206).

After the auto-membership reshape, `in_stack` on a catalog entry
means "a local copy exists", not "in the caller's stack". The key name still
says the old thing, so the card macro read it the old way and offered "Add to
stack" on a package listed under My Stack. The projection now states the
semantics (`in_stack_is_local`) and the macro reads the flag.

A wording defect with no server behaviour to assert, so these are template-
level checks: the macro rendered against each entry shape, plus a count that
ties the flag to the number of projections re-pointing the key.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app" / "web" / "templates"
STATIC = ROOT / "app" / "web" / "static"


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
