"""Guard tests for the grouped `/admin` sidebar (`_admin_nav.html`).

(a) Inventory-driven coverage: every ``require_admin``-gated, template-
    rendering GET route registered in ``app/web/router.py`` must be
    reachable from an entry in ``app/web/admin_nav.py`` — the seven
    ``ADMIN_NAV_SECTIONS`` or the ``ADMIN_NAV_HOME`` row. A future admin
    page shipped without a nav entry fails this test — that is the point.
(b) Active-state: visiting a page marks its own nav row active, and only
    that one. The hub row is active on ``/admin`` EXACTLY, never as a
    prefix, or the column would show two active rows on every admin page.
(c) The sidebar renders for admins on admin pages only — never for a
    non-admin (403 before the template body even runs), and never on a
    non-admin page.
(d) Section disclosure: the section containing the current page renders
    expanded server-side (no ``hidden`` on its body, ``aria-expanded="true"``
    on its header); every other section renders collapsed. This is what
    keeps first paint honest — a client-side-only default would flash the
    full ~31-row list before JS could collapse it.
(e) The column does NOT collapse. The whole-sidebar toggle, its 56px icon
    strip, the per-section hover flyouts and the stored preference behind
    them are retired — the rail beside it owns collapse/expand for the
    window, and two collapsible columns made "how do I get back?" a
    two-step puzzle. ``TestAdminNavDoesNotCollapse`` pins that, since the
    pieces reappear one accessor at a time if nothing watches.
(f) The rail's Admin row is one plain link to ``/admin`` on every page —
    the data-driven area flyout it used to open is gone, along with the
    second, drifted copy of the admin inventory that fed it.
"""

from __future__ import annotations

from pathlib import Path
import re

from app.web.admin_nav import (
    ADMIN_NAV_HOME,
    ADMIN_NAV_SECTIONS,
    resolve_active_href,
    resolve_active_section_key,
    resolve_home_active,
)

ROUTER_SRC = Path("app/web/router.py").read_text(encoding="utf-8")
# Admin pages whose routes are NOT declared in the web router. `/admin/chat` is
# a content-negotiated JSON/HTML route on a router with its own
# `prefix="/admin/chat"`, so neither `_ROUTE_RE` nor the path literal appears
# in ROUTER_SRC. It became a nav entry when /admin's card grid was replaced by
# the dashboard (the grid was the only place it was linked from), so the
# reverse guard below has to know it exists or it reads as a dead link.
# Add a module here only when it genuinely serves an admin PAGE.
_EXTERNAL_ADMIN_ROUTE_MODULES = ("app/api/admin_chat.py",)
RAIL_CSS = Path("app/web/static/css/rail.css").read_text(encoding="utf-8")
RAIL_HTML = Path("app/web/templates/_app_rail.html").read_text(encoding="utf-8")
ADMIN_NAV_JS = Path("app/web/static/js/admin/admin_nav.js").read_text(encoding="utf-8")
RAIL_TOGGLE_JS = Path("app/web/static/js/rail_toggle.js").read_text(encoding="utf-8")

# Routes deliberately outside the sidebar's scope — see admin_nav.py's module
# docstring for why each is excluded.
_OUT_OF_SCOPE_PATHS = {
    # NOTE: "/admin" is deliberately NOT here — the hub is the sidebar's first
    # row (ADMIN_NAV_HOME), so it is covered like every other page.
    "/admin/studio",  # get_current_user, not require_admin — a different surface
    "/admin/studio/{domain}",  # ditto
    "/admin/usage",  # redirect -> /admin/telemetry
    "/admin/grants",  # redirect -> /admin/access
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


_PREFIX_RE = re.compile(r'APIRouter\(\s*prefix="(/admin[^"]*)"')
_SUBPATH_RE = re.compile(r'@(?:\w+)\.get\("([^"]*)"')


def _external_admin_route_literals() -> set[str]:
    """Admin route paths declared outside `app/web/router.py`.

    Reads each module in `_EXTERNAL_ADMIN_ROUTE_MODULES`, joins its router
    prefix onto each GET sub-path, and returns the resulting literals. Only
    feeds the REVERSE guard (is this nav href real?) — the forward guard
    stays scoped to the web router, whose job is catching a new PAGE shipped
    without a nav entry.
    """
    out: set[str] = set()
    for mod in _EXTERNAL_ADMIN_ROUTE_MODULES:
        src = Path(mod).read_text(encoding="utf-8")
        prefix_m = _PREFIX_RE.search(src)
        if not prefix_m:
            continue
        prefix = prefix_m.group(1)
        out.add(prefix)
        for sub in _SUBPATH_RE.findall(src):
            out.add((prefix + sub).rstrip("/") or prefix)
    return out


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
    # The hub row first — it is a nav entry like any other now, just one that
    # lives above the sections rather than inside one.
    prefixes = [ADMIN_NAV_HOME["href"]]
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
        route_literals |= _external_admin_route_literals()
        dead = []
        for section in ADMIN_NAV_SECTIONS:
            for item in section["items"]:
                href = item["href"]
                if href not in route_literals and href not in routes:
                    dead.append(href)
        assert not dead, f"nav entr(ies) point at a URL with no matching router route: {dead}"

    def test_exactly_the_decided_sections_in_order(self) -> None:
        """The IA is a decision, not a projection — pin the section
        keys/labels and their order so a future edit that reshuffles them
        (or quietly drops one) fails loudly.

        The shape is the admin redesign's: three INTENT sections first
        (People, Data, Access — get people in, get data in, get the data to
        the people), then the maintenance half behind the divider (Library,
        Instance, Activity)."""
        assert [(s["key"], s["label"]) for s in ADMIN_NAV_SECTIONS] == [
            ("people", "People"),
            ("data", "Data"),
            ("access", "Access"),
            ("library", "Library"),
            ("instance", "Instance"),
            ("activity", "Activity"),
        ]

    def test_the_maintain_divider_opens_the_library_section(self) -> None:
        """The intent/maintain split is part of the pinned IA: exactly one
        section carries `divider_before`, and it is the first maintenance
        one."""
        flagged = [s["key"] for s in ADMIN_NAV_SECTIONS if s.get("divider_before")]
        assert flagged == ["library"]

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

    def test_access_and_its_legacy_url_light_the_access_row(self) -> None:
        """`/admin/access` is a real page again (the Access section's row) and
        `/admin/grants` 308s onto it, so both resolve to that row rather than
        to Groups, where they pointed while the matrix lived on the group's
        own detail tab."""
        assert resolve_active_href("/admin/access") == "/admin/access"
        assert resolve_active_href("/admin/grants") == "/admin/access"

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
        assert resolve_active_section_key("/admin/access") == "access"
        assert resolve_active_section_key("/admin/tables") == "data"
        assert resolve_active_section_key("/admin/mcp-sources") == "instance"
        assert resolve_active_section_key("/admin/store/lint") == "library"
        assert resolve_active_section_key("/admin/news") == "library"
        assert resolve_active_section_key("/admin/server-config") == "instance"
        assert resolve_active_section_key("/admin/activity") == "activity"

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


class TestAdminNavDoesNotCollapse:
    """The column is the primary navigation of every page that renders it, and
    the rail beside it already owns collapse/expand for the window — two
    collapsible columns made "how do I get back?" a two-step puzzle. What was
    here (a toggle, a 56px icon strip, seven hover flyouts, a stored
    preference) is retired; these pin that it stays retired, since the pieces
    reappear one accessor at a time if nothing watches."""

    def test_no_collapse_toggle_icon_strip_or_flyouts(self, seeded_app) -> None:
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/admin/users", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200, resp.text
        text = resp.text
        for gone in (
            "data-admin-nav-collapse-toggle",
            "data-admin-nav-rail-btn",
            "data-admin-nav-flyout",
            "admin-nav__rail",
            "admin-nav__flyout",
            "admin-nav__collapse",
        ):
            assert gone not in text, gone
        # ...nor the stored preference, nor the inline first-paint script that
        # applied it before the aside painted.
        assert "agnes.adminNav.collapsed" not in text
        # The JS may still NAME the retired key in its header comment (that is
        # the record of what went and why); what it must not do is read or
        # write it, or toggle the class it drove.
        assert 'getItem("agnes.adminNav.collapsed")' not in ADMIN_NAV_JS
        assert 'setItem("agnes.adminNav.collapsed"' not in ADMIN_NAV_JS
        assert "is-collapsed" not in ADMIN_NAV_JS

    def test_css_carries_no_collapsed_state(self) -> None:
        css = Path("app/web/static/css/admin-nav.css").read_text(encoding="utf-8")
        assert ".admin-nav.is-collapsed" not in css
        assert ".admin-nav__rail" not in css
        assert ".admin-nav__flyout" not in css

    def test_per_section_disclosure_survives(self, seeded_app) -> None:
        """Only the WHOLE-COLUMN collapse went. A section still opens and
        closes — that is what makes the nesting legible."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/admin/users", headers={"Authorization": f"Bearer {token}"})
        assert "data-admin-nav-toggle" in resp.text
        assert "agnes.adminNav.sections" in ADMIN_NAV_JS


class TestAdminNavHomeRow:
    """The hub is the sidebar's first row. It used to be reachable only by
    clicking the column's uppercase "Admin" TITLE, which nobody reads as a
    link."""

    def test_home_row_renders_first_and_is_not_inside_a_section(self, seeded_app) -> None:
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/admin/users", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200, resp.text
        nav = resp.text.split('<aside class="admin-nav"', 1)[1].split("</aside>", 1)[0]
        assert "admin-nav__link--home" in nav
        # First link in the column, ahead of every section body.
        assert nav.index("admin-nav__link--home") < nav.index("admin-nav__group-body")
        # And it is not a member of the seven sections.
        assert all(
            ADMIN_NAV_HOME["href"] != item["href"] for section in ADMIN_NAV_SECTIONS for item in section["items"]
        )

    def test_title_is_a_label_not_a_link(self, seeded_app) -> None:
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/admin/users", headers={"Authorization": f"Bearer {token}"})
        assert '<span class="admin-nav__title">' in resp.text
        assert '<a class="admin-nav__title"' not in resp.text

    def test_home_is_active_on_the_hub_only(self) -> None:
        assert resolve_home_active("/admin") is True
        assert resolve_home_active("/admin/") is True
        # A prefix rule here would light the hub row on every admin page and
        # give the column two active rows at once.
        assert resolve_home_active("/admin/users") is False
        assert resolve_home_active("/admin/store/lint") is False

    def test_hub_page_lights_the_home_row_and_no_section_row(self, seeded_app) -> None:
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/admin", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200, resp.text
        actives = re.findall(r'class="admin-nav__link[^"]*is-active"[^>]*>([^<]+)<', resp.text)
        assert actives == [ADMIN_NAV_HOME["label"]], actives


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


class TestRailCollapsePreference:
    """The rail's collapsed/expanded width is a PERSISTED USER PREFERENCE
    (localStorage `agnes.rail.collapsed`), honored on EVERY rail page —
    admin included — not derived from the request path the way it used to
    be (`_admin_page` in _app_rail.html now only supplies the default a
    caller who has never toggled starts from). Markup-level assertions live
    here; the CSS contract that makes the collapse/peek/overlay behaviour
    actually work lives in TestRailCollapseCss below."""

    def _auth(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def _enable_chat(self, seeded_app) -> None:
        """New chat / the conversation region / the onboarding row are all
        `can_chat`-gated (`_compute_can_chat`, app/web/router.py), which
        needs BOTH an explicit resource grant on a group the caller actually
        belongs to AND `app.state.chat_config.enabled` — admin god-mode
        covers neither (`has_explicit_grant` deliberately does not
        short-circuit for Admin), and with chat disabled (the default with
        no instance.yaml) every test below asserting on those blocks,
        present or absent, would pass vacuously regardless of the
        branch it means to exercise. Granted to the Admin group rather than
        Everyone: new users are no longer auto-added to Everyone
        (src/repositories/users.py), so the seeded admin (a member of Admin
        only) would not pick up a grant made there. Sets `chat_config`
        directly on the already-built app (same pattern as
        test_admin_chat.py) rather than `AGNES_CHAT_ENABLED`, which would
        re-run the full startup provider/key validation cascade in
        app/main.py for a check that only reads `.enabled`."""
        from app.chat.config import ChatConfig
        from src.db import SYSTEM_ADMIN_GROUP, get_system_db
        from src.repositories import resource_grants_repo, user_groups_repo

        conn = get_system_db()
        admin_group = user_groups_repo().get_by_name(SYSTEM_ADMIN_GROUP)
        resource_grants_repo().create(admin_group["id"], "chat", "chat")
        conn.close()
        seeded_app["client"].app.state.chat_config = ChatConfig(enabled=True)

    def test_default_is_collapsed_on_admin_expanded_elsewhere(self, seeded_app, monkeypatch) -> None:
        """With no stored preference, the server-rendered class is only a
        STARTING value — collapsed on /admin, expanded everywhere else."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        c = seeded_app["client"]
        admin_text = c.get("/admin/users", headers=self._auth(seeded_app["admin_token"])).text
        assert 'class="rail rail-icon-mode"' in admin_text
        assert 'class="admin-nav"' in admin_text

        stack_text = c.get("/stack", headers=self._auth(seeded_app["admin_token"])).text
        assert 'class="rail-icon-mode"' not in stack_text
        assert 'class="rail"' in stack_text

    def test_preference_bootstrap_script_renders_on_every_rail_page(self, seeded_app, monkeypatch) -> None:
        """The before-first-paint bootstrap (reads localStorage, applies a
        stored preference to the `<nav>` class before the browser paints —
        same no-flash technique as _theme_resolve.html) must render on EVERY
        rail page, not just /admin — the preference applies everywhere."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        c = seeded_app["client"]
        for path in ("/admin/users", "/stack"):
            text = c.get(path, headers=self._auth(seeded_app["admin_token"])).text
            assert "agnes.rail.collapsed" in text
            assert "document.currentScript.parentNode" in text
            # The bootstrap script is the FIRST thing inside <nav> — before
            # any visible chrome — so it runs before that subtree paints.
            nav_open = text.index("<nav class=")
            script_pos = text.index("<script>", nav_open)
            logo_pos = text.index('class="rail-logo-row"', nav_open)
            assert nav_open < script_pos < logo_pos

    def test_one_consolidated_toggle_with_correct_aria(self, seeded_app, monkeypatch) -> None:
        """ONE `#rail-toggle` in the logo row, not two separate controls —
        the old admin-only `#rail-icon-toggle` tap affordance is gone
        entirely, replaced by this single control everywhere."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        c = seeded_app["client"]
        admin_text = c.get("/admin/users", headers=self._auth(seeded_app["admin_token"])).text
        assert 'id="rail-icon-toggle"' not in admin_text
        assert "js/rail_icon_mode.js" not in admin_text
        assert admin_text.count('id="rail-toggle"') == 1
        assert 'aria-expanded="false"' in admin_text.split('id="rail-toggle"', 1)[1][:200]
        assert "Expand navigation" in admin_text.split('id="rail-toggle"', 1)[1][:200]

        stack_text = c.get("/stack", headers=self._auth(seeded_app["admin_token"])).text
        assert 'id="rail-icon-toggle"' not in stack_text
        assert "js/rail_icon_mode.js" not in stack_text
        assert stack_text.count('id="rail-toggle"') == 1
        assert 'aria-expanded="true"' in stack_text.split('id="rail-toggle"', 1)[1][:200]
        assert "Collapse navigation" in stack_text.split('id="rail-toggle"', 1)[1][:200]

    def test_toggle_js_loaded_on_every_rail_page(self, seeded_app, monkeypatch) -> None:
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        c = seeded_app["client"]
        for path in ("/admin/users", "/stack"):
            text = c.get(path, headers=self._auth(seeded_app["admin_token"])).text
            assert "js/rail_toggle.js" in text

    def test_rail_admin_entry_is_a_plain_active_link_not_a_flyout(self, seeded_app, monkeypatch) -> None:
        """The admin sidebar already carries the seven-area IA — the rail's
        own Admin flyout would just be the same tree twice, so on an admin
        page it collapses to a plain link back to the hub."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        c = seeded_app["client"]
        resp = c.get("/admin/users", headers=self._auth(seeded_app["admin_token"]))
        text = resp.text
        nav = text.split('<nav class="rail rail-icon-mode"', 1)[1].split("</nav>", 1)[0]
        assert "rail-admin-summary" not in nav
        assert "rail-admin-flyout" not in nav
        assert 'rail-i on" href="/admin"' in nav

    def test_rail_keeps_destinations_and_profile_when_collapsed(self, seeded_app, monkeypatch) -> None:
        """The collapsed rail still SHOWS the brand mark, the destination rows
        (including New chat, once chat is granted) and the profile avatar."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        self._enable_chat(seeded_app)
        c = seeded_app["client"]
        resp = c.get("/admin/users", headers=self._auth(seeded_app["admin_token"]))
        text = resp.text
        for anchor in ('class="rail-orb"', 'id="new-chat"', 'href="/library"', 'id="userMenuTrigger"'):
            assert anchor in text, f"collapsed rail is missing {anchor}"

    def test_conversation_region_renders_on_admin_pages_and_is_css_gated(self, seeded_app, monkeypatch) -> None:
        """The region is RENDERED on an admin page and hidden by CSS while the
        rail is collapsed, so hover-peeking it (or persisting expanded) gives
        you the standard rail — conversations included, identical to any
        other rail page.

        Chat is explicitly enabled+granted (`_enable_chat`) so this is a real
        assertion and not a vacuous pass from `can_chat` being False anyway."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        self._enable_chat(seeded_app)
        c = seeded_app["client"]
        admin_text = c.get("/admin/users", headers=self._auth(seeded_app["admin_token"])).text
        assert 'id="rail-history"' in admin_text
        # The rows are the standard rail's rows, so they get the standard
        # rail's per-row menu (Pin/Rename/Delete) rather than a degraded copy.
        assert "js/components/chat_row_menu.js" in admin_text

        # Same caller, same grant, a non-admin rail page: identical.
        stack_text = c.get("/stack", headers=self._auth(seeded_app["admin_token"])).text
        assert 'id="rail-history"' in stack_text

    def test_onboarding_row_stays_when_collapsed_same_anatomy_as_other_rows(self, seeded_app, monkeypatch) -> None:
        """Unlike the conversation region, the onboarding row is NOT removed
        when collapsed — hiding then materializing it on peek would shift the
        profile row exactly as the conversation region would. It doesn't
        need special-casing to avoid that any more, though: its icon IS the
        progress ring (a fixed size in both states, same as any other row's
        icon), so only its label — same mechanism as every other row's —
        appears/disappears, never the row itself."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        self._enable_chat(seeded_app)
        c = seeded_app["client"]
        text = c.get("/admin/users", headers=self._auth(seeded_app["admin_token"])).text
        assert 'id="railGetStarted"' in text
        assert 'id="rail-getstarted-toggle"' in text
        assert 'id="rail-getstarted-ring-fill"' in text
        assert 'id="rail-getstarted-title"' in text
        assert 'id="rail-getstarted-count"' in text
        # Retired anatomy stays retired here too — no bar, no chevron.
        assert "rail-getstarted-bar" not in text
        assert "rail-getstarted-chev" not in text
        # The JS that writes its progress (title/count/ring) is loaded on an
        # admin page, same as everywhere else.
        assert "js/rail_history.js" in text
        assert "js/chat_onboarding.js" in text

    def test_non_admin_rail_page_keeps_the_full_rail_and_a_plain_admin_link(self, seeded_app, monkeypatch) -> None:
        """A rail page outside /admin/* keeps the full-width rail and the
        conversation region — and, since the flyout was retired, the SAME
        plain Admin link the admin pages get. The two branches converged: one
        Admin row, one destination, on every page."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        self._enable_chat(seeded_app)
        c = seeded_app["client"]
        resp = c.get("/stack", headers=self._auth(seeded_app["admin_token"]))
        assert resp.status_code == 200, resp.text
        text = resp.text
        assert 'class="rail-icon-mode"' not in text
        assert 'class="rail"' in text
        nav = text.split('<nav class="rail"', 1)[1].split("</nav>", 1)[0]
        assert 'id="rail-history"' in nav
        # No flyout machinery anywhere, on any page.
        for gone in (
            "rail-admin-summary",
            "rail-admin-flyout",
            "rail-admin-sub",
            "rail-admin-groups",
            "rail-admin-caret",
        ):
            assert gone not in nav, gone
        # ...and Admin is a plain link to the hub.
        assert '<a class="rail-i " href="/admin">' in nav or '<a class="rail-i" href="/admin">' in nav

    def test_topnav_default_is_unaffected_on_admin_and_non_admin_pages(self, seeded_app, monkeypatch) -> None:
        monkeypatch.delenv("AGNES_UI_LAYOUT", raising=False)
        c = seeded_app["client"]
        for path in ("/admin/users", "/stack"):
            resp = c.get(path, headers=self._auth(seeded_app["admin_token"]))
            assert resp.status_code == 200, (path, resp.text)
            assert "rail-icon-mode" not in resp.text
            assert 'class="rail"' not in resp.text
            assert "js/rail_toggle.js" not in resp.text
            assert 'id="rail-toggle"' not in resp.text


class TestRailCollapseCss:
    """CSS contract for the collapsed rail — everything the markup-level
    tests above can't see (hover/focus peek expansion, the overlay-not-reflow
    body clearance, the ≤1024px reflow staying untouched, reduced motion,
    the hover-delay on open, and the hover-capable/touch split on the
    logo-row toggle)."""

    def _desktop_block(self) -> str:
        """The `@media (min-width: 1025px)` block's body — every collapse
        rule must live inside this (or a sibling desktop-only query), never
        inside the `@media (max-width: 1024px)` block below it."""
        start = RAIL_CSS.index("@media (min-width: 1025px) {")
        end = RAIL_CSS.index("@media (max-width: 1024px)")
        return RAIL_CSS[start:end]

    def test_icon_mode_rules_are_gated_to_desktop_only(self) -> None:
        """Guards against the collapse mechanism fighting the narrow-screen
        rail reflow (CLAUDE.md's design-system contract): every occurrence of
        `.rail-icon-mode` in a sizing/behavioural rule sits above the
        `max-width: 1024px` query, never inside it."""
        narrow = RAIL_CSS[RAIL_CSS.index("@media (max-width: 1024px)") :]
        assert "rail-icon-mode" not in narrow, (
            "the collapse mechanism leaked into the ≤1024px reflow — that query must win outright at narrow widths"
        )
        # The width-preference toggle itself has no job below 1025px either
        # — it is switched off there rather than left to render inertly.
        assert 'html[data-ui-layout="rail"] .rail-toggle {\n        display: none;' in narrow

    def test_collapsed_width_and_peek_triggers(self) -> None:
        block = self._desktop_block()
        assert 'html[data-ui-layout="rail"] .rail.rail-icon-mode {' in block
        assert "width: 56px;" in block
        # Hover and keyboard :focus-within both peek it open — no separate
        # "pinned open" class any more (clicking the toggle persists the
        # preference instead, which removes .rail-icon-mode outright).
        assert ":is(:hover, :focus-within) {" in block
        assert "rail-pinned-open" not in RAIL_CSS
        assert "width: 240px;" in block

    def test_hover_open_delay_close_is_instant(self) -> None:
        """~120ms delay on the way OPEN (an accidental pointer pass-through
        shouldn't peek it); the collapsed rule carries no delay, so closing
        on mouse-leave is instant."""
        block = self._desktop_block()
        collapsed = block.split('html[data-ui-layout="rail"] .rail.rail-icon-mode {', 1)[1].split("}", 1)[0]
        assert "ease) 0s" in collapsed
        # Matched by shape rather than by the literal selector: the peek rules
        # also carry a `:not(.rail-no-peek)` gate (see the peek-suppression test
        # below), and pinning the exact string made this fail for a change that
        # left its actual invariant — delayed open, instant close — intact.
        peek = re.search(
            r'html\[data-ui-layout="rail"\] \.rail\.rail-icon-mode[^ ]*:is\(:hover, :focus-within\) \{([^}]*)\}',
            block,
        )
        assert peek, "no rule peeks the collapsed rail open"
        assert "width: 240px" in peek.group(1)
        assert "ease) .12s" in peek.group(1)

    def test_both_widths_animate_and_the_page_moves_with_them(self) -> None:
        """The toggle animates in BOTH directions, and the page's clearance
        travels with the rail rather than snapping ahead of it.

        Two bugs in one guard, because they share a cause — a transition that
        only existed on ONE of the two states. The width transition used to be
        declared solely on `.rail.rail-icon-mode`, so clicking to EXPAND (which
        removes that class) dropped the rule in the same frame the width
        changed and the rail jumped 56 -> 240px with no animation at all; and
        `body`'s padding-left carried no transition either, so content landed
        184px away a full 160ms before the rail caught up."""
        block = self._desktop_block()
        base = block.split('html[data-ui-layout="rail"] .rail {', 1)[1].split("}", 1)[0]
        assert "transition: width" in base, "the expanded state needs its own width transition or it cannot animate"
        clearance = block.split('html[data-ui-layout="rail"] body:has(.rail) {', 1)[1].split("}", 1)[0]
        assert "transition: padding-left" in clearance
        # Same duration token on both, so the two edges can't drift apart.
        assert "--ds-motion-fast" in base and "--ds-motion-fast" in clearance

    def test_peek_is_suppressed_for_the_collapse_that_just_happened(self) -> None:
        """`#rail-toggle` lives inside the rail and Cmd+\\ can fire while the
        pointer rests on it, so `:hover` / `:focus-within` kept the peek open
        through a collapse: `body` reflowed to the 56px clearance while the rail
        stayed 240px wide on top of it, and nothing looked collapsed until the
        caller wandered off. Every peek rule is gated on `:not(.rail-no-peek)`,
        which rail_toggle.js parks on the rail for exactly that window."""
        peeks = re.findall(r"\.rail\.rail-icon-mode(:not\(\.rail-no-peek\))?:is\(:hover, :focus-within\)", RAIL_CSS)
        assert peeks, "no peek rules found"
        assert all(peeks), "a peek rule is missing its :not(.rail-no-peek) gate — a collapse can stall open on it"
        # The class is inert on its own: it must never carry declarations of
        # its own, or it becomes a third rail state instead of a suppression.
        assert not re.search(r"\.rail-no-peek\s*\{", RAIL_CSS)
        # …and it has to be released, or the rail is left permanently un-peekable.
        assert "rail-no-peek" in RAIL_TOGGLE_JS
        assert "function releasePeek" in RAIL_TOGGLE_JS
        assert "pointermove" in RAIL_TOGGLE_JS, "release must not rely on mouseleave alone (the rail shrinks away)"

    def test_the_column_opens_before_anything_inside_it_appears(self) -> None:
        """The sequence the whole peek depends on: width first, content second,
        and the reverse on the way out.

        The width carries a 120ms opening delay (anti pass-through); every reveal
        trails it on one shared token, so the column is already moving before a
        label, the conversation list or the collapse toggle starts to appear —
        and none of them can out-run it. The close token is shorter and undelayed,
        so content releases while the column is still wide."""
        assert "--rail-peek-text-delay:" in RAIL_CSS
        assert "--rail-peek-text-out:" in RAIL_CSS
        reveal = float(RAIL_CSS.split("--rail-peek-text-delay:", 1)[1].split(";", 1)[0].strip().rstrip("s"))
        out = float(RAIL_CSS.split("--rail-peek-text-out:", 1)[1].split(";", 1)[0].strip().rstrip("s"))
        assert reveal > 0.12, "the reveal must trail the width's 120ms opening delay, not race it"
        assert out <= reveal, "closing must not linger longer than opening — content leads on the way out"

        # Every rule that reveals something uses the token, none a hardcoded
        # delay: the labels, the conversation region AND the collapse toggle,
        # which is content too and used to appear at t=0 on its own trigger.
        block = self._desktop_block()
        reveals = [
            rule
            for rule in re.findall(r"\.rail-icon-mode[^{]*(?::hover|:focus-within)[^{]*\{[^}]*\}", block)
            if "opacity: 1;" in rule
        ]
        assert len(reveals) >= 3, reveals
        for rule in reveals:
            # The orb's restore is the one opacity flip with nothing to reveal —
            # it is already on screen; the peek only undoes the touch swap.
            if ".rail-logo-row .rail-logo" in rule:
                continue
            assert "var(--rail-peek-text-delay)" in rule, rule

    def test_body_clearance_is_the_icon_width_and_constant(self) -> None:
        """The 56px reservation must NOT change on hover/focus — the peeked
        rail is an overlay, so the page's own layout never moves."""
        block = self._desktop_block()
        assert "body:has(.rail.rail-icon-mode) {" in block
        assert "padding-left: 56px;" in block
        # No hover/focus-conditioned body rule anywhere in the file.
        assert re.search(r"body:has\(\.rail\.rail-icon-mode[^)]*\)\s*:is\(", RAIL_CSS) is None
        assert "hover, .rail-icon-mode" not in RAIL_CSS  # no accidental body-level hover selector

    def test_css_gates_the_conversation_region_on_both_states(self) -> None:
        """The conversation region is the one entry in the collapse hide-list
        that is not merely a label: its rows are text end to end and have no
        icon form. So it is hidden while COLLAPSED and restored when
        PEEKED/EXPANDED, and both halves must exist — a hide with no matching
        restore would leave the peeked rail without the conversations that
        are the whole reason to peek it."""
        icon_mode_section = RAIL_CSS.split("── Collapsed rail")[1].split("Small + tablet screens")[0]
        assert ".rail-icon-mode .rail-history" in icon_mode_section
        expanded = ":is(:hover, :focus-within) .rail-history"
        assert expanded in icon_mode_section
        # The restore must come AFTER the hide, or the cascade drops it.
        assert icon_mode_section.index(".rail-icon-mode .rail-history") < icon_mode_section.index(expanded)

    def test_onboarding_row_never_needs_a_height_override_in_icon_mode(self) -> None:
        """The onboarding row's icon IS the progress ring, a fixed size in
        both states — unlike the conversation region it needed no bespoke
        height/reservation rule to keep it from shifting other rows, and
        none should reappear. Its label instead rides the SAME shared
        show/hide list every other row's label does (see the next test)."""
        assert not re.search(r"\.rail-icon-mode \.rail-getstarted\s*\{[^}]*height:", RAIL_CSS)
        assert not re.search(r":is\([^)]*\)\s*\.rail-getstarted\s*\{[^}]*height:", RAIL_CSS)

    def test_onboarding_row_label_hides_via_the_shared_label_mechanism(self) -> None:
        """`.rail-getstarted-body` (the row's two-line label) must sit in the
        SAME collapse/expand selector lists as `.rail-i-label` — same
        mechanism as every other row, not a bespoke rule of its own."""
        block = self._desktop_block()
        # Matched on the SELECTOR LIST, never on the property doing the hiding.
        # This test has now been rewritten twice for changes that kept its actual
        # invariant — one shared mechanism — perfectly intact: once when the rules
        # gained an opacity pair, and again when the mechanism moved off `display`
        # onto `visibility` (see test_hidden_content_stays_in_flow). The selector
        # list is the invariant; the declaration is an implementation detail.
        collapsed = re.search(r"\.rail-icon-mode \.rail-logo-txt,(.*?)\{[^}]*visibility: hidden;[^}]*\}", block, re.S)
        assert collapsed and "rail-getstarted-body" in collapsed.group(1)
        revealed = re.search(
            r":is\(:hover, :focus-within\) \.rail-logo-txt,(.*?)\{[^}]*visibility: visible;[^}]*\}",
            block,
            re.S,
        )
        assert revealed and "rail-getstarted-body" in revealed.group(1)

    def test_hidden_content_stays_in_flow(self) -> None:
        """The collapsed rail hides its labels and conversation list with
        `visibility`, NEVER `display`.

        `display: none` took them out of flow, so peeking had to put them back —
        an un-animatable relayout of the whole column on the frame the pointer
        arrived, with text laid out in a 40px box before the panel had gained a
        pixel. That is what made the peek look broken: content moving before the
        container did. `visibility: hidden` keeps the same boxes in flow, so hover
        changes nothing but the rail's width, and it keeps the strip inert (a
        hidden row takes no clicks, where `opacity: 0` alone would leave the
        conversation list clickable under the pointer)."""
        # Comments stripped first — this rule's own note recounts the bug by
        # name, and the first version of this test matched that.
        css = re.sub(r"/\*.*?\*/", "", self._desktop_block(), flags=re.S)
        hide = css.split(".rail-icon-mode .rail-logo-txt,", 1)[1].split("}", 1)[0]
        assert "visibility: hidden;" in hide
        assert "opacity: 0;" in hide, "opacity is what animates; visibility only steps behind it"
        assert "display:" not in hide, "display is back in the collapse hide rule — that is the relayout-on-hover bug"
        # `visibility` is discrete: it MUST be stepped behind the opacity it
        # guards (0s duration, delayed), or it flips at the wrong end and the
        # fade it is protecting never renders.
        assert re.search(r"visibility 0s[^;]*var\(--rail-peek-text-out\)", hide)

    def test_nothing_discrete_flips_on_peek(self) -> None:
        """The other half of the same bug. Every property the peek changes has
        to be interpolatable, or it lands in full on the first frame — ahead of
        the width, which is the one thing that should lead.

        Two were: `justify-content` (centre -> flex-start), which snapped every
        icon in the column sideways, and the toggle's `position` (absolute ->
        static), which teleported it from the middle of the 56px strip to the
        right end of a column that had not widened yet. Icons now travel by
        padding and the toggle rides a `right` anchor."""
        block = self._desktop_block()
        peek_rules = re.findall(r":is\(:hover, :focus-within\)[^{]*\{([^}]*)\}", block)
        assert peek_rules
        for body in peek_rules:
            for prop in ("justify-content:", "position:", "display:", "inset:"):
                assert prop not in body, f"discrete property {prop} flips on peek: {body!r}"

    def test_onboarding_row_icon_is_a_constant_size_ring_in_both_states(self) -> None:
        """The ring's own box (`.rail-getstarted-ring`) must be an unconditional
        rule, not one that changes width/height between collapsed and
        peeked-open — that constancy is what keeps the row's own height from
        ever differing between the two."""
        assert not re.search(r":is\([^)]*\)\s*\.rail-getstarted-ring\s*\{[^}]*(width|height):", RAIL_CSS)
        ring = RAIL_CSS.split('html[data-ui-layout="rail"] .rail-getstarted-ring {', 1)[1].split("}", 1)[0]
        assert "width: 28px" in ring
        assert "height: 28px" in ring

    def test_completed_onboarding_is_not_retired_from_the_rail(self) -> None:
        """`.is-complete` never sets `display: none` — the row stays on every
        rail page at 5/5 too, where the arc simply reads a full lap."""
        assert ".rail-getstarted.is-complete {" not in RAIL_CSS

    def test_the_ring_is_a_bare_progress_meter(self) -> None:
        """Track + arc and NOTHING inside.

        A glyph centred in the ring was tried and rejected: at 28px an icon
        and an arc compete for the same pixels, the icon wins, and the row
        reads as "a checklist icon" instead of "you are N of M done". This
        guards against it creeping back — the meter's legibility is the whole
        reason the element exists.
        """
        assert "rail-getstarted-ring-glyph" not in RAIL_CSS
        assert "rail-getstarted-ring-glyph" not in RAIL_HTML

        # Both strokes present, equal, and heavy enough to read as a meter
        # rather than as a decorative circle at this diameter.
        track = RAIL_CSS.split(".rail-getstarted-ring-track {", 1)[1].split("}", 1)[0]
        fill = RAIL_CSS.split(".rail-getstarted-ring-fill {", 1)[1].split("}", 1)[0]
        assert "stroke-width: 4" in track
        assert "stroke-width: 4" in fill
        assert "stroke-linecap: round" in fill
        # The arc is the brand action colour, never Kai's identity accent.
        assert "stroke: var(--ds-primary)" in fill
        assert "--ds-kai" not in fill

    def test_onboarding_row_height_is_pinned_in_icon_mode(self) -> None:
        """The row's label is two lines and therefore taller than its 28px
        ring, so letting the row size to its content made it 40px collapsed
        and 46px peeked-open — and because the bottom zone is pinned to the
        foot, those 6px slid Library/Agents/Admin upward on every hover. One
        pinned height in both states is what stops that.
        """
        btn = RAIL_CSS.split('html[data-ui-layout="rail"] .rail.rail-icon-mode .rail-getstarted-btn {', 1)[1].split(
            "}", 1
        )[0]
        assert "min-height" in btn

    def test_reduced_motion_disables_the_width_and_toggle_transitions(self) -> None:
        assert "@media (min-width: 1025px) and (prefers-reduced-motion: reduce)" in RAIL_CSS
        reduced = RAIL_CSS[RAIL_CSS.index("@media (min-width: 1025px) and (prefers-reduced-motion: reduce)") :]
        reduced = reduced[: reduced.index("\n}") + 2]
        assert "rail-icon-mode" in reduced
        assert "rail-toggle" in reduced
        assert "transition: none;" in reduced

    def test_toggle_hidden_on_hover_reveals_on_touch(self) -> None:
        """A mouse already peeks the rail open on hover, and a keyboard
        caller already gets `:focus-within` — the toggle stays invisible at
        rest there, appearing only on hover/focus of the logo row. Touch has
        neither, so the `(hover: none)` override shows it unconditionally
        instead — this is the ONE control, not a second bolted-on one."""
        block = self._desktop_block()
        # At rest, collapsed: invisible. Two rules share the
        # `.rail.rail-icon-mode .rail-toggle` selector (one just declares the
        # opacity transition, alongside `.rail-logo`) — match the specific
        # rule that sets `opacity: 0`, not the first one textually.
        rest = re.search(
            r'html\[data-ui-layout="rail"\] \.rail\.rail-icon-mode \.rail-toggle \{([^}]*opacity: 0[^}]*)\}',
            block,
        )
        assert rest, "no rule sets the collapsed toggle to invisible at rest"
        # Hover/focus on the logo row reveals it.
        reveal = re.search(
            r"\.rail-logo-row:hover \.rail-toggle,\s*"
            r'html\[data-ui-layout="rail"\] \.rail\.rail-icon-mode \.rail-logo-row:focus-within \.rail-toggle\s*'
            r"\{([^}]*)\}",
            block,
        )
        assert reveal and "opacity: 1" in reveal.group(1)
        # Touch: no hover event at all, so it defaults to visible instead.
        assert "@media (min-width: 1025px) and (hover: none)" in RAIL_CSS
        touch = RAIL_CSS[RAIL_CSS.index("@media (min-width: 1025px) and (hover: none)") :]
        touch = touch[: touch.index("\n}\n") + 2]
        assert ".rail-toggle" in touch
        assert "opacity: 1" in touch

    def test_no_second_toggle_control_left_behind(self) -> None:
        """The old admin-only `#rail-icon-toggle` tap affordance is gone in
        CSS too, as a selector — the name may still appear in a comment
        recording why it was retired (history, not a live rule)."""
        for selector in (".rail-icon-toggle {", ".rail-icon-toggle:", ".rail-icon-toggle "):
            assert selector not in RAIL_CSS, selector
