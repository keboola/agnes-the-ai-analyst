"""Guard tests for the grouped, collapsible `/admin` sidebar (`_admin_nav.html`).

(a) Inventory-driven coverage: every ``require_admin``-gated, template-
    rendering GET route registered in ``app/web/router.py`` must be
    reachable from an entry in ``app/web/admin_nav.py::ADMIN_NAV_SECTIONS``
    (or be the ``/admin`` hub itself, which the sidebar's "Admin" title
    links to instead of a section row). A future admin page shipped without
    a nav entry fails this test — that is the point.
(b) Active-state: visiting a page marks its own nav row active, and only
    that one.
(c) The sidebar renders for admins on admin pages only — never for a
    non-admin (403 before the template body even runs), and never on a
    non-admin page.
(d) Section disclosure: the section containing the current page renders
    expanded server-side (no ``hidden`` on its body, ``aria-expanded="true"``
    on its header); every other section renders collapsed. This is what
    keeps first paint honest — a client-side-only default would flash the
    full ~31-row list before JS could collapse it.
(e) Collapsed-sidebar markup: the whole-sidebar collapse toggle and one
    rail icon button per section are always present in the rendered HTML
    (CSS/JS decide which of the two representations is visible; the guard
    only needs to know both exist to be toggled between).
"""

from __future__ import annotations

from pathlib import Path
import re

from app.web.admin_nav import ADMIN_NAV_SECTIONS, resolve_active_href, resolve_active_section_key

ROUTER_SRC = Path("app/web/router.py").read_text(encoding="utf-8")

# Routes deliberately outside the sidebar's scope — see admin_nav.py's module
# docstring for why each is excluded.
_OUT_OF_SCOPE_PATHS = {
    "/admin",  # the hub itself — reached via the sidebar's "Admin" title link
    "/admin/studio",  # get_current_user, not require_admin — a different surface
    "/admin/studio/{domain}",  # ditto
    "/admin/usage",  # redirect -> /admin/telemetry
    "/admin/access",  # redirect -> /admin/groups
    "/admin/grants",  # redirect -> /admin/groups
    "/admin/scheduler-runs",  # redirect -> /admin/activity
    "/admin/agent-prompt",  # redirect -> /admin/prompts
    "/admin/workspace-prompt",  # redirect -> /admin/prompts
}

_ROUTE_RE = re.compile(r'@router\.get\("(/admin[^"]*)"')


def _admin_get_routes() -> list[tuple[str, str]]:
    """``(path, function_body)`` for every ``@router.get("/admin...")`` in
    the web router — the function body is the source from the decorator to
    the next ``@router.`` (or ``def `` at column 0), enough to sniff
    ``require_admin`` + whether it renders a template vs. a bare redirect."""
    lines = ROUTER_SRC.split("\n")
    routes: list[tuple[str, str]] = []
    for i, line in enumerate(lines):
        m = _ROUTE_RE.search(line)
        if not m:
            continue
        path = m.group(1)
        j = i + 1
        body_lines: list[str] = []
        while j < len(lines):
            nxt = lines[j]
            if nxt.startswith("@router.") or nxt.startswith("def ") or nxt.startswith("async def "):
                # Stop once we've reached the NEXT route (only if we've
                # already consumed this route's own `def` line).
                if body_lines and any("def " in bl for bl in body_lines):
                    break
            body_lines.append(nxt)
            j += 1
            if len(body_lines) > 120:
                break
        routes.append((path, "\n".join(body_lines)))
    return routes


def _is_admin_gated(body: str) -> bool:
    return "require_admin" in body


def _renders_template(body: str) -> bool:
    return "templates.TemplateResponse(" in body


def _in_scope_admin_page_routes() -> list[str]:
    """Every GET ``/admin/...`` route that is admin-gated, renders a
    template (not a bare redirect), and isn't explicitly out of scope."""
    out = []
    for path, body in _admin_get_routes():
        if path in _OUT_OF_SCOPE_PATHS:
            continue
        if _is_admin_gated(body) and _renders_template(body):
            out.append(path)
    return out


def _route_literal_prefix(path: str) -> str:
    """The literal (non-templated) portion of a route path, e.g.
    ``/admin/users/{user_id}`` -> ``/admin/users``."""
    return path.split("{")[0].rstrip("/")


def _all_nav_prefixes() -> list[str]:
    prefixes = []
    for section in ADMIN_NAV_SECTIONS:
        for item in section["items"]:
            prefixes.extend(item["match"])
    return prefixes


class TestAdminNavInventoryCoverage:
    def test_finds_a_realistic_number_of_admin_page_routes(self) -> None:
        """Sanity check on the parser itself — if this drops to near-zero
        the regex/route-walking above broke silently and the coverage test
        below would pass vacuously."""
        routes = _in_scope_admin_page_routes()
        assert len(routes) >= 25, routes

    def test_every_admin_page_route_has_a_nav_entry(self) -> None:
        prefixes = _all_nav_prefixes()
        uncovered = []
        for path in _in_scope_admin_page_routes():
            literal = _route_literal_prefix(path)
            hit = any(literal == p or literal.startswith(p + "/") or p == literal for p in prefixes)
            if not hit:
                uncovered.append(path)
        assert not uncovered, (
            f"admin page route(s) with no sidebar entry in app/web/admin_nav.py::ADMIN_NAV_SECTIONS: {uncovered}"
        )

    def test_every_nav_href_is_a_real_admin_route(self) -> None:
        """The reverse direction: a nav entry pointing at a URL the router
        doesn't serve would be a dead link in the sidebar."""
        routes = {p for p, _ in _admin_get_routes()}
        route_literals = {_route_literal_prefix(p) for p in routes}
        dead = []
        for section in ADMIN_NAV_SECTIONS:
            for item in section["items"]:
                href = item["href"]
                if href not in route_literals and href not in routes:
                    dead.append(href)
        assert not dead, f"nav entr(ies) point at a URL with no matching router route: {dead}"

    def test_exactly_the_seven_decided_sections_in_order(self) -> None:
        """The IA is a decision, not a projection — pin the seven section
        keys/labels and their order so a future edit that reshuffles them
        (or quietly drops one) fails loudly."""
        assert [(s["key"], s["label"]) for s in ADMIN_NAV_SECTIONS] == [
            ("people", "People & access"),
            ("data", "Data"),
            ("connections", "Connections"),
            ("moderation", "Moderation"),
            ("content", "Content"),
            ("instance", "Instance"),
            ("insights", "Insights"),
        ]

    def test_every_section_has_a_distinct_key_and_icon(self) -> None:
        keys = [s["key"] for s in ADMIN_NAV_SECTIONS]
        icons = [s["icon"] for s in ADMIN_NAV_SECTIONS]
        assert len(keys) == len(set(keys)), keys
        assert all(icons), "every section must carry an icon name for the collapsed rail"


class TestAdminNavActiveState:
    def test_active_href_resolves_own_section_item(self) -> None:
        assert resolve_active_href("/admin/users") == "/admin/users"
        assert resolve_active_href("/admin/groups") == "/admin/groups"
        assert resolve_active_href("/admin/tokens") == "/admin/tokens"

    def test_active_href_follows_detail_pages_to_the_parent_entry(self) -> None:
        assert resolve_active_href("/admin/users/abc123") == "/admin/users"
        assert resolve_active_href("/admin/groups/grp-1") == "/admin/groups"
        assert resolve_active_href("/admin/mcp-tools/tool1/grants") == "/admin/mcp-sources"

    def test_active_href_does_not_confuse_parent_and_child_sections(self) -> None:
        """/admin/store (the moderation hub) and /admin/store/submissions
        each carry their own row — a sub-page must not also leave its
        parent lit."""
        assert resolve_active_href("/admin/store") == "/admin/store"
        assert resolve_active_href("/admin/store/submissions") == "/admin/store/submissions"
        assert resolve_active_href("/admin/store/submissions/sub-1") == "/admin/store/submissions"
        assert resolve_active_href("/admin/store/lint") == "/admin/store/lint"

    def test_hub_page_has_no_active_section(self) -> None:
        assert resolve_active_href("/admin") is None

    def test_redirect_targets_land_on_the_groups_row(self) -> None:
        assert resolve_active_href("/admin/access") == "/admin/groups"
        assert resolve_active_href("/admin/grants") == "/admin/groups"

    def test_only_one_item_renders_is_active_for_each_page(self, seeded_app) -> None:
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        def _auth(t: str) -> dict:
            return {"Authorization": f"Bearer {t}"}

        for path, expected_label in (("/admin/users", "Users"), ("/admin/groups", "Groups")):
            resp = c.get(path, headers=_auth(token))
            assert resp.status_code == 200, resp.text
            actives = re.findall(r'admin-nav__link is-active"[^>]*>([^<]+)<', resp.text)
            assert actives == [expected_label], (path, actives)


class TestAdminNavActiveSection:
    """`resolve_active_section_key` picks the ONE section that should render
    expanded by default — the one containing `resolve_active_href`'s pick."""

    def test_active_section_matches_the_active_item_s_section(self) -> None:
        assert resolve_active_section_key("/admin/users") == "people"
        assert resolve_active_section_key("/admin/tables") == "data"
        assert resolve_active_section_key("/admin/mcp-sources") == "connections"
        assert resolve_active_section_key("/admin/store/lint") == "moderation"
        assert resolve_active_section_key("/admin/news") == "content"
        assert resolve_active_section_key("/admin/server-config") == "instance"
        assert resolve_active_section_key("/admin/activity") == "insights"

    def test_hub_page_has_no_active_section_key(self) -> None:
        assert resolve_active_section_key("/admin") is None

    def test_only_the_active_section_renders_expanded_server_side(self, seeded_app) -> None:
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/admin/users", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200, resp.text
        text = resp.text

        # The active section's header carries aria-expanded="true" and its
        # body carries no `hidden` attribute; every other section's header
        # carries aria-expanded="false" and its body IS hidden.
        groups = re.findall(
            r'data-admin-nav-group="([\w-]+)">\s*'
            r'<button type="button" class="admin-nav__group-hd" data-admin-nav-toggle\s*'
            r'aria-expanded="(true|false)"',
            text,
        )
        assert len(groups) == len(ADMIN_NAV_SECTIONS), groups
        by_key = dict(groups)
        assert by_key["people"] == "true"
        for section in ADMIN_NAV_SECTIONS:
            if section["key"] == "people":
                continue
            assert by_key[section["key"]] == "false", section["key"]

        # The active section's body has no `hidden`; the rest do.
        bodies = re.findall(
            r'<div class="admin-nav__group-body" id="admin-nav-body-([\w-]+)"( hidden)?>',
            text,
        )
        by_body_key = dict(bodies)
        assert by_body_key["people"] == "", "the active section's body must not be hidden"
        for section in ADMIN_NAV_SECTIONS:
            if section["key"] == "people":
                continue
            assert by_body_key[section["key"]] == " hidden", section["key"]


class TestAdminNavCollapsedMarkup:
    def test_collapse_toggle_and_seven_rail_icons_present(self, seeded_app) -> None:
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/admin/users", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200, resp.text
        text = resp.text
        assert "data-admin-nav-collapse-toggle" in text
        rail_keys = re.findall(r'data-admin-nav-rail-btn="([\w-]+)"', text)
        assert rail_keys == [s["key"] for s in ADMIN_NAV_SECTIONS]
        flyout_keys = re.findall(r'data-admin-nav-flyout="([\w-]+)"', text)
        assert flyout_keys == [s["key"] for s in ADMIN_NAV_SECTIONS]


class TestAdminNavGating:
    def test_sidebar_absent_for_non_admin(self, seeded_app) -> None:
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/admin/users", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403
        assert "admin-nav" not in resp.text

    def test_sidebar_absent_on_a_non_admin_page(self, seeded_app) -> None:
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/dashboard", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code in (200, 302)
        if resp.status_code == 200:
            assert "admin-nav__title" not in resp.text

    def test_sidebar_present_for_admin_on_admin_page(self, seeded_app) -> None:
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/admin/users", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert 'class="admin-nav"' in resp.text
        assert 'class="admin-nav__title"' in resp.text
