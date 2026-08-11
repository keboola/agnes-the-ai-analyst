"""Static guard: every admin page linked from the topnav is reachable in the rail.

`data-ui-layout` picks exactly one chrome. `topnav` renders `_app_header.html`,
`rail` renders `_app_rail.html`, and neither falls back to the other — so an
admin entry that lives in only one of them is *unreachable* on instances using
the other layout, with no error and no empty state to hint at it.

That is not hypothetical: `/admin/semantic-layer` shipped in the topnav's
Sources column and never in the rail, so rail-layout instances had a live
Semantic layer page with no link anywhere in the product. It was found by an
operator who reached the page from a URL someone pasted them.

The guard is one-directional (topnav → rail) because the topnav is the older,
fuller menu; rail-only entries are a redesign choice, not drift.
"""

from __future__ import annotations

import re
from pathlib import Path

HEADER = Path("app/web/templates/_app_header.html")
RAIL = Path("app/web/templates/_app_rail.html")

# Topnav admin-menu entries that are deliberately absent from the rail. Each
# needs a reason; an entry without one is drift, not a decision.
KNOWN_TOPNAV_ONLY = {
    # The admin hub index. The rail's expandable Admin section IS the hub —
    # it lists every area inline, so a link to the index page is redundant.
    "/admin",
    # Needs exact-path matching to sit next to /admin/store/submissions, and
    # the rail's `_admin_hit` macro is startswith-only: an '/admin/store'
    # entry would light up on '/admin/store/submissions' too. Adding it means
    # touching the match model, which belongs in its own change.
    "/admin/store",
}


def _header_admin_links() -> set[str]:
    """Admin hrefs from the topnav's admin dropdown.

    Scoped to `role="menuitem"` so top-level nav links (e.g. the `can_studio`
    Studio entry, deliberately dropped in the rail redesign — see the note in
    `_app_rail.html`) are not mistaken for admin-menu items.
    """
    html = HEADER.read_text()
    return set(re.findall(r'role="menuitem"\s+href="(/admin[^"]*)"', html))


def _rail_links() -> set[str]:
    return set(re.findall(r"'href':\s*'([^']+)'", RAIL.read_text()))


def test_every_topnav_admin_page_is_reachable_from_the_rail():
    missing = _header_admin_links() - _rail_links() - KNOWN_TOPNAV_ONLY
    assert not missing, (
        f"Admin pages linked in the topnav but not in the rail: {sorted(missing)}. "
        "Rail-layout instances cannot reach them at all. Add them to the matching "
        "section of _app_rail.html, or to KNOWN_TOPNAV_ONLY with the reason."
    )


def test_semantic_layer_is_linked_in_both_chromes():
    """The specific regression: a live admin page with no link under `rail`."""
    assert "/admin/semantic-layer" in _header_admin_links()
    assert "/admin/semantic-layer" in _rail_links()


def test_known_exceptions_are_still_topnav_entries():
    """Keeps the exception list honest: an entry that no longer exists in the
    topnav is stale and must be deleted rather than silently excusing a page
    that isn't there any more."""
    stale = KNOWN_TOPNAV_ONLY - _header_admin_links()
    assert not stale, f"KNOWN_TOPNAV_ONLY lists entries the topnav no longer has: {sorted(stale)}"
