"""Static guard: every USER-FACING page linked from the topnav is reachable
under the rail chrome too.

Sibling of ``test_admin_nav_parity.py``, which guards the same invariant for
the admin menu. ``data-ui-layout`` picks exactly one chrome — ``topnav``
renders ``_app_header.html``, ``rail`` renders ``_app_rail.html``, and neither
falls back to the other — so a destination that lives only in the topnav is
*unreachable* on rail-layout instances: no error, no empty state, no global
search box to stumble into it with.

Not hypothetical: the rail redesign shipped without /me/connections,
/corporate-memory, /apps, /marketplace and /catalog. The first three had
simply been dropped (the first rail carried Memory; the topnav carries all
of them); the last two were retired deliberately *onto* in-page replacement
paths — "the Library header's + Add menu, search" — that did not exist.
All five were found by audit, not by a user with a working link.

Reachability under rail = a literal href in ``_app_rail.html`` OR in
``library.html`` (the Library is a rail destination, and its "+ Add" menu is
the rail's designated home for browse/install paths). Anything else needs a
``KNOWN_TOPNAV_ONLY`` entry with the reason. One-directional (topnav → rail)
like the admin guard: rail-only entries are a redesign choice, not drift.
"""

from __future__ import annotations

import re
from pathlib import Path

HEADER = Path("app/web/templates/_app_header.html")
RAIL = Path("app/web/templates/_app_rail.html")
LIBRARY = Path("app/web/templates/library.html")

# Topnav user-facing entries deliberately absent from the rail chrome. Each
# needs a reason; an entry without one is drift, not a decision.
KNOWN_TOPNAV_ONLY = {
    # Consolidated by the rail redesign into /how-it-works#connect; the rail
    # account menu carries "How {brand} works" instead. The chrome-dependent
    # switch is guarded by tests/test_web_nav_cowork.py.
    "/me/ai-connector",
    # Studio was retired from the rail wholesale (see the IA note in
    # _app_rail.html): the builders it fronted live in the Library "+ Add"
    # menu (/skills?type=...), and the admin-side Studio pages are a topnav
    # surface. Restoring a rail path is an IA decision, not a parity fix.
    "/admin/studio",
}


def _topnav_user_links() -> set[str]:
    """Literal hrefs a user can click in the topnav chrome: the primary nav
    row plus the user dropdown. Dynamic hrefs (``{{ _home }}``, logout's
    ``url_for``) can't be collected statically and are skipped."""
    html = HEADER.read_text()
    primary = re.findall(r'class="app-nav-link[^"]*"\s+href="(/[^"?#]+)"', html)
    dropdown = re.findall(r'class="app-user-menu-item[^"]*"\s+role="menuitem"\s+href="(/[^"?#]+)"', html)
    return set(primary) | set(dropdown)


def _rail_reachable_links() -> set[str]:
    """Hrefs reachable under the rail chrome: the rail itself (literal hrefs
    plus the data-driven admin flyout entries) and the Library page."""
    rail = RAIL.read_text()
    library = LIBRARY.read_text()
    links = set(re.findall(r'href="(/[^"?#]+)"', rail))
    links |= set(re.findall(r"'href':\s*'(/[^'?#]+)'", rail))
    links |= set(re.findall(r'href="(/[^"?#]+)"', library))
    return links


def test_every_topnav_user_page_is_reachable_under_rail():
    missing = _topnav_user_links() - _rail_reachable_links() - KNOWN_TOPNAV_ONLY
    assert not missing, (
        f"User-facing pages linked in the topnav but unreachable under the rail "
        f"chrome: {sorted(missing)}. Rail-layout instances cannot reach them at "
        "all. Add a link to _app_rail.html (or an entry to the Library '+ Add' "
        "menu in library.html), or add the page to KNOWN_TOPNAV_ONLY with the "
        "reason it is deliberately topnav-only."
    )


def test_rail_replacement_paths_for_retired_entries_exist():
    """The rail retired its Marketplace and Data Packages rows *onto* the
    Library "+ Add" menu. That substitution only holds while the menu really
    carries both links — this pins them so a Library toolbar refactor can't
    silently strand two live surfaces again."""
    library = LIBRARY.read_text()
    for href in ("/marketplace", "/catalog"):
        assert f'href="{href}"' in library, (
            f"library.html no longer links {href}. Under the rail chrome the "
            "Library '+ Add' menu is that page's only entry point (see the IA "
            "note in _app_rail.html) — restore the menu item or give the rail "
            "a nav entry."
        )
