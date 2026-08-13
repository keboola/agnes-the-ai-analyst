"""Static guard: every admin page linked from the topnav is reachable under `rail`.

`data-ui-layout` picks exactly one chrome. `topnav` renders `_app_header.html`,
`rail` renders `_app_rail.html`, and neither falls back to the other — so an
admin entry that lives in only one of them is *unreachable* on instances using
the other layout, with no error and no empty state to hint at it.

That is not hypothetical: `/admin/semantic-layer` shipped in the topnav's
Sources column and never in the rail, so rail-layout instances had a live
Semantic layer page with no link anywhere in the product. It was found by an
operator who reached the page from a URL someone pasted them.

The reachability contract changed when the rail's hand-written Admin flyout was
retired (it was a second, drifting copy of the admin inventory): the rail now
carries ONE link — `/admin` — and every `/admin/*` page renders the admin
column (`_admin_nav.html`), whose inventory is `app/web/admin_nav.py`. So the
guarantee this guard enforces is:

    topnav admin menu  ⊆  admin_nav.py inventory   (reachable via rail → /admin)

plus the rail actually linking `/admin`. `tests/test_web_admin_nav.py` guards
the other half (every `require_admin` route has an inventory entry), so
together the two make "linked somewhere" mean "linked everywhere".
"""

from __future__ import annotations

import re
from pathlib import Path

from app.web.admin_nav import (
    ADMIN_NAV_HOME,
    ADMIN_NAV_OFFNAV,
    ADMIN_NAV_SECTIONS,
    _section_entries,
)

HEADER = Path("app/web/templates/_app_header.html")
RAIL = Path("app/web/templates/_app_rail.html")


def _header_admin_links() -> set[str]:
    """Admin hrefs from the topnav's admin dropdown.

    Scoped to `role="menuitem"` so top-level nav links (e.g. the `can_studio`
    Studio entry, deliberately dropped in the rail redesign — see the note in
    `_app_rail.html`) are not mistaken for admin-menu items.
    """
    html = HEADER.read_text()
    return set(re.findall(r'role="menuitem"\s+href="(/admin[^"]*)"', html))


def _inventory_hrefs() -> set[str]:
    """Every URL the admin column can reach, across BOTH tiers.

    A section carries its children under `tabs` (a destination with a tab
    strip) or `items` (a legacy disclosure group) — never both — and a
    destination's own `href` is a link in its own right. Reading `["items"]`
    directly raised KeyError the moment the first section became tabbed, so
    this walks the same accessor the resolvers do. Query strings are dropped:
    a lens tab (`/admin/access?lens=simulate`) reaches the same PAGE as its
    sibling, which is the granularity the topnav is compared at.

    `ADMIN_NAV_OFFNAV` counts as reachable. It is the explicit list of admin
    pages that deliberately carry no nav row because they are reached from
    somewhere specific instead — `/admin/sync` from a source card's SYNC cell,
    since a sync run is per-source and a nav row would imply a cross-source
    page that does not exist. Each entry there already records the door it IS
    reached from, which is the property this test is really asserting; treating
    them as missing would push a row back into the column that was removed on
    purpose."""
    hrefs = {ADMIN_NAV_HOME["href"]}
    for section in ADMIN_NAV_SECTIONS:
        if section.get("href"):
            hrefs.add(section["href"])
        hrefs.update(entry["href"].split("?", 1)[0] for entry in _section_entries(section))
    hrefs.update(entry["href"].split("?", 1)[0] for entry in ADMIN_NAV_OFFNAV)
    return hrefs


def test_every_topnav_admin_page_is_in_the_admin_nav_inventory():
    missing = _header_admin_links() - _inventory_hrefs()
    assert not missing, (
        f"Admin pages linked in the topnav but absent from app/web/admin_nav.py: "
        f"{sorted(missing)}. Rail-layout instances reach admin pages only through "
        "the admin column, whose inventory that module is — add the entry there."
    )


def test_rail_links_the_admin_hub():
    """The rail's single Admin entry — the door to the whole inventory."""
    assert re.search(r'href="/admin"', RAIL.read_text()), (
        "_app_rail.html no longer links /admin; rail-layout instances would have no path into the admin area at all."
    )


def test_semantic_layer_is_reachable_from_both_chromes():
    """The specific regression: a live admin page with no link under `rail`."""
    assert "/admin/semantic-layer" in _header_admin_links()
    assert "/admin/semantic-layer" in _inventory_hrefs()
