"""Regression tests pinning the contract between the top nav and the
registered HTML routes.

Two assertions:

  1. Every literal ``href="/..."`` in ``_app_header.html`` must point to a
     registered route — catches dead nav links.

  2. Every registered HTML route must be reachable from the nav OR sit on
     a documented allowlist (detail pages reached from list views, auth /
     onboarding flows, intentionally-CLI-driven surfaces). Catches orphan
     pages that nobody links to.

The allowlist exists so that pages reached from elsewhere (e.g.
``/admin/users/{user_id}`` from the user list, ``/login`` from the
unauthenticated entry point) don't fail the second test. Routes added
without a nav link or an allowlist entry are treated as orphans —
either add them to the nav or document why they're reachable elsewhere.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.routing import APIRoute
from fastapi.responses import HTMLResponse

from app.main import app


# Routes intentionally not in the top nav: auth/onboarding, list-view
# detail pages, error/landing routes, and analyst pages that should be in
# nav but aren't yet (Phase 2 of the UI overhaul moves Catalog and
# Corporate Memory off this list and into the nav).
ALLOWLIST = {
    "/",
    "/setup",
    "/login",
    "/login/password",
    "/login/email",
    "/auth/password/reset",
    "/auth/password/setup",
    "/install",
    "/profile",
    "/tokens",
    # Detail pages reached from list views — never linked from the nav.
    "/admin/users/{user_id}",
    "/admin/groups/{group_id}",
    "/admin/grants",
    # Reached from /corporate-memory's "Manage knowledge" admin link.
    "/corporate-memory/admin",
}


def _registered_html_routes() -> set[str]:
    """GET routes whose ``response_class`` is ``HTMLResponse`` — i.e. real
    HTML pages humans navigate to. Filtering on response_class rather
    than URL prefix excludes CLI artifact routes, marketplace ZIP/git
    surfaces, and webhook endpoints, all of which return non-HTML even
    though they don't sit under ``/api/``."""
    paths: set[str] = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if "GET" not in route.methods:
            continue
        if route.response_class is not HTMLResponse:
            continue
        paths.add(route.path)
    return paths


def _nav_hrefs() -> set[str]:
    """Literal ``href="/..."`` values in ``_app_header.html``. Skips
    ``href="{{ url_for(...) }}"`` — those resolve via the URL shim and
    are validated implicitly when the template renders."""
    header = Path("app/web/templates/_app_header.html").read_text()
    return set(re.findall(r'href="(/[^"#?{]+)"', header))


def test_every_nav_link_resolves():
    """Every static href in the nav must point to a registered route."""
    nav = _nav_hrefs()
    routes = _registered_html_routes()
    missing = nav - routes
    assert not missing, (
        f"Nav links to non-registered routes: {sorted(missing)}. "
        "Either register the route or remove the dead nav link."
    )


def test_every_registered_page_is_reachable_or_allowlisted():
    """Every registered HTML route must be in the nav or on the allowlist.

    A route here that is neither in the nav nor on the allowlist is an
    orphan — nobody can reach it from the UI. Either add a nav link or
    add it to ALLOWLIST with a comment explaining how users reach it.
    """
    nav = _nav_hrefs()
    routes = _registered_html_routes()
    orphans = routes - nav - ALLOWLIST
    assert not orphans, (
        f"Registered HTML routes neither in nav nor on allowlist: "
        f"{sorted(orphans)}. Either add to nav, add to ALLOWLIST with "
        "rationale, or drop the route."
    )


def test_required_top_level_nav_links_present():
    """Pin the analyst-surface entry points so a refactor can't silently
    drop them from the header. ``/catalog`` and ``/corporate-memory`` are
    the analyst's two daily destinations; their absence in the nav is
    the kind of regression operators only notice when an analyst opens
    a support ticket asking 'where did the data catalog go?'."""
    nav = _nav_hrefs()
    required = {"/dashboard", "/catalog", "/corporate-memory", "/install"}
    missing = required - nav
    assert not missing, (
        f"Top nav missing required entries: {sorted(missing)}."
    )


def test_admin_submenu_links_present_in_header():
    """The admin sub-menu is rendered server-side inside the same header
    template (gated on ``session.user.role == 'admin'``) so its hrefs are
    in scope of ``_nav_hrefs`` regardless of which user role is rendering
    in tests. Pin the admin destinations so a refactor that drops e.g.
    'Registered tables' from the dropdown doesn't pass CI."""
    nav = _nav_hrefs()
    required_admin = {
        "/admin/users",
        "/admin/groups",
        "/admin/access",
        "/admin/tables",
        "/admin/sync",
        "/admin/marketplaces",
        "/admin/tokens",
    }
    missing = required_admin - nav
    assert not missing, (
        f"Admin sub-menu missing required entries: {sorted(missing)}."
    )
