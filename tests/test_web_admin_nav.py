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
RAIL_CSS = Path("app/web/static/css/rail.css").read_text(encoding="utf-8")
RAIL_HTML = Path("app/web/templates/_app_rail.html").read_text(encoding="utf-8")

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


class TestRailIconModeOnAdminPages:
    """On rail-layout instances, /admin/* pages now carry TWO nav columns —
    the admin sidebar above plus the global rail (`_app_rail.html`). The
    rail collapses to a ~56px icon strip there (`_admin_page` in
    _app_rail.html) so the admin sidebar can be the primary nav instead of
    two permanent full-width columns; see the CHANGELOG's admin-sidebar
    bullet. Markup-level assertions live here; the CSS contract that makes
    the collapse/expand/overlay behaviour actually work lives in
    TestRailIconModeCss below."""

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
        `_admin_page` branch it means to exercise. Granted to the Admin
        group rather than Everyone: new users are no longer auto-added to
        Everyone (src/repositories/users.py), so the seeded admin (a member
        of Admin only) would not pick up a grant made there. Sets
        `chat_config` directly on the already-built app (same pattern as
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

    def test_rail_renders_icon_mode_alongside_the_admin_sidebar(self, seeded_app, monkeypatch) -> None:
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        c = seeded_app["client"]
        resp = c.get("/admin/users", headers=self._auth(seeded_app["admin_token"]))
        assert resp.status_code == 200, resp.text
        text = resp.text
        assert 'class="rail rail-icon-mode"' in text
        assert 'class="admin-nav"' in text

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

    def test_rail_keeps_destinations_and_profile_in_icon_mode(self, seeded_app, monkeypatch) -> None:
        """Icon mode still SHOWS the brand mark, the destination rows
        (including New chat, once chat is granted) and the profile avatar."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        self._enable_chat(seeded_app)
        c = seeded_app["client"]
        resp = c.get("/admin/users", headers=self._auth(seeded_app["admin_token"]))
        text = resp.text
        for anchor in ('class="rail-orb"', 'id="new-chat"', 'href="/library"', 'id="userMenuTrigger"'):
            assert anchor in text, f"icon-mode rail is missing {anchor}"

    def test_conversation_region_is_never_rendered_in_icon_mode(self, seeded_app, monkeypatch) -> None:
        """Regression guard for the reflow bug: a block that materializes
        only on hover shifts every row below it (`.rail-nav-bottom`'s
        `margin-top: auto` re-anchors the bottom zone to whatever height the
        column above it has), so the rail's row set must be identical
        collapsed and hover-expanded. The conversation region's rows are
        text and cannot shrink to 56px, so — unlike the onboarding card,
        which gets a fixed-height ring instead (see the next test) — it is
        absent from the response ENTIRELY on an admin page: there's no hover
        state for a static HTML fetch to toggle, so total absence is what a
        `:hover` CSS rule can never accidentally undo. Chat is explicitly
        enabled+granted (`_enable_chat`) so this is a real assertion about
        the `_admin_page` branch and not a vacuous pass from `can_chat`
        being False anyway — the non-admin comparison page below proves the
        same grant DOES render it when `_admin_page` isn't in play."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        self._enable_chat(seeded_app)
        c = seeded_app["client"]
        admin_text = c.get("/admin/users", headers=self._auth(seeded_app["admin_token"])).text
        assert 'id="rail-history"' not in admin_text
        # The JS that fills the conversation list has nothing to bind to on
        # an admin page's chat rows, but it also wires the onboarding card's
        # popover (which DOES render there — see the next test), so it stays
        # loaded; only the row-menu script (Pin/Rename/Delete, conversation
        # rows only) is skipped.
        assert "js/components/chat_row_menu.js" not in admin_text

        # Same caller, same grant, a non-admin rail page: the region DOES
        # render — proving the assertion above tests `_admin_page`, not an
        # unrelated `can_chat` gate.
        stack_text = c.get("/stack", headers=self._auth(seeded_app["admin_token"])).text
        assert 'id="rail-history"' in stack_text

    def test_onboarding_row_stays_in_icon_mode_same_anatomy_as_other_rows(self, seeded_app, monkeypatch) -> None:
        """Unlike the conversation region, the onboarding row is NOT removed
        in icon mode — hiding then materializing it on hover would shift the
        profile row exactly as the conversation region would. It doesn't
        need special-casing to avoid that any more, though: its icon IS the
        progress ring (a fixed size in both icon-mode states, same as any
        other row's icon), so only its label — same mechanism as every other
        row's — appears/disappears, never the row itself."""
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

    def test_tap_expand_affordance_exists_in_icon_mode(self, seeded_app, monkeypatch) -> None:
        """The click/tap pin toggle must exist in the markup for a touch
        caller to reach — CSS (not this test) decides that a hover-capable
        mouse never sees it. See TestRailIconModeCss for that half."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        c = seeded_app["client"]
        resp = c.get("/admin/users", headers=self._auth(seeded_app["admin_token"]))
        text = resp.text
        assert 'id="rail-icon-toggle"' in text
        assert "js/rail_icon_mode.js" in text

    def test_tap_expand_affordance_absent_outside_icon_mode(self, seeded_app, monkeypatch) -> None:
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        c = seeded_app["client"]
        resp = c.get("/stack", headers=self._auth(seeded_app["admin_token"]))
        assert resp.status_code == 200, resp.text
        text = resp.text
        assert 'id="rail-icon-toggle"' not in text
        assert "js/rail_icon_mode.js" not in text

    def test_non_admin_rail_page_keeps_the_full_rail_and_its_admin_flyout(self, seeded_app, monkeypatch) -> None:
        """A rail page outside /admin/* must render byte-for-byte as before
        this follow-up: full-width rail, conversation region, and the
        data-driven Admin flyout (not the plain link)."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        self._enable_chat(seeded_app)
        c = seeded_app["client"]
        resp = c.get("/stack", headers=self._auth(seeded_app["admin_token"]))
        assert resp.status_code == 200, resp.text
        text = resp.text
        assert 'class="rail-icon-mode"' not in text
        assert 'class="rail"' in text
        nav = text.split('<nav class="rail"', 1)[1].split("</nav>", 1)[0]
        assert "rail-admin-summary" in nav
        assert "rail-admin-flyout" in nav
        assert 'id="rail-history"' in nav

    def test_topnav_default_is_unaffected_on_admin_and_non_admin_pages(self, seeded_app, monkeypatch) -> None:
        monkeypatch.delenv("AGNES_UI_LAYOUT", raising=False)
        c = seeded_app["client"]
        for path in ("/admin/users", "/stack"):
            resp = c.get(path, headers=self._auth(seeded_app["admin_token"]))
            assert resp.status_code == 200, (path, resp.text)
            assert "rail-icon-mode" not in resp.text
            assert 'class="rail"' not in resp.text
            assert "js/rail_icon_mode.js" not in resp.text


class TestRailIconModeCss:
    """CSS contract for the icon-mode rail — everything the markup-level
    tests above can't see (hover/focus/pin expansion, the overlay-not-reflow
    body clearance, the ≤1024px reflow staying untouched, reduced motion,
    and the hover-capable/touch split on the tap toggle)."""

    def _desktop_block(self) -> str:
        """The `@media (min-width: 1025px)` block's body — every icon-mode
        rule must live inside this (or a sibling desktop-only query), never
        inside the `@media (max-width: 1024px)` block below it."""
        start = RAIL_CSS.index("@media (min-width: 1025px) {")
        end = RAIL_CSS.index("@media (max-width: 1024px)")
        return RAIL_CSS[start:end]

    def test_icon_mode_rules_are_gated_to_desktop_only(self) -> None:
        """Guards against icon mode fighting the narrow-screen rail reflow
        (CLAUDE.md's design-system contract): every occurrence of
        `.rail-icon-mode` in a sizing/behavioural rule sits above the
        `max-width: 1024px` query, never inside it."""
        narrow = RAIL_CSS[RAIL_CSS.index("@media (max-width: 1024px)") :]
        assert "rail-icon-mode" not in narrow, (
            "icon-mode rules leaked into the ≤1024px reflow — that query must win outright at narrow widths"
        )

    def test_collapsed_width_and_expand_triggers(self) -> None:
        block = self._desktop_block()
        assert 'html[data-ui-layout="rail"] .rail.rail-icon-mode {' in block
        assert "width: 56px;" in block
        # Hover, keyboard :focus-within, and the click/tap pin all expand it.
        assert ":is(:hover, :focus-within, .rail-pinned-open) {" in block
        assert "width: 240px;" in block

    def test_body_clearance_is_the_icon_width_and_constant(self) -> None:
        """The 56px reservation must NOT change on hover/focus/pin — the
        expanded rail is an overlay, so the page's own layout never moves."""
        block = self._desktop_block()
        assert "body:has(.rail.rail-icon-mode) {" in block
        assert "padding-left: 56px;" in block
        # No hover/focus/pin-conditioned body rule anywhere in the file.
        assert re.search(r"body:has\(\.rail\.rail-icon-mode[^)]*\)\s*:is\(", RAIL_CSS) is None
        assert "hover, .rail-icon-mode" not in RAIL_CSS  # no accidental body-level hover selector

    def test_css_never_toggles_the_conversation_region(self) -> None:
        """Regression guard for the reflow bug: the conversation region must
        not be addressed by ANY icon-mode CSS rule (hide-on-collapse,
        restore-on-expand, or otherwise) — `_app_rail.html` simply never
        renders it on an admin page, in either state, which is the only way
        to guarantee the row set can't differ between hover and rest. A CSS
        rule that hides it on `.rail-icon-mode` and shows it again on
        `:hover`/`:focus-within`/`.rail-pinned-open` is exactly the pattern
        that shifted every row below it and must never come back."""
        icon_mode_section = RAIL_CSS.split("── Icon-only rail")[1].split("Small + tablet screens")[0]
        assert "rail-history" not in icon_mode_section

    def test_onboarding_row_never_needs_a_height_override_in_icon_mode(self) -> None:
        """The onboarding row's icon IS the progress ring, a fixed size in
        both icon-mode states — unlike the conversation region it needed no
        bespoke height/reservation rule to keep it from shifting other rows,
        and none should reappear. Its label instead rides the SAME shared
        show/hide list every other row's label does (see the next test)."""
        assert not re.search(r"\.rail-icon-mode \.rail-getstarted\s*\{[^}]*height:", RAIL_CSS)
        assert not re.search(r":is\([^)]*\)\s*\.rail-getstarted\s*\{[^}]*height:", RAIL_CSS)

    def test_onboarding_row_label_hides_via_the_shared_label_mechanism(self) -> None:
        """`.rail-getstarted-body` (the row's two-line label) must sit in the
        SAME collapse/expand selector lists as `.rail-i-label` — same
        mechanism as every other row, not a bespoke rule of its own."""
        block = self._desktop_block()
        collapsed = re.search(r"\.rail-icon-mode \.rail-logo-txt,.*?\{\s*display: none;\s*\}", block, re.S)
        assert collapsed and "rail-getstarted-body" in collapsed.group(0)
        expanded = re.search(
            r":is\(:hover, :focus-within, \.rail-pinned-open\) \.rail-getstarted-body,.*?\{\s*display: flex;\s*\}",
            block,
            re.S,
        )
        assert expanded is not None

    def test_onboarding_row_icon_is_a_constant_size_ring_in_both_states(self) -> None:
        """The ring's own box (`.rail-getstarted-ring`) must be an unconditional
        rule, not one that changes width/height between collapsed and
        hover-expanded — that constancy is what keeps the row's own height
        from ever differing between the two."""
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
        and 46px hover-expanded — and because the bottom zone is pinned to
        the foot, those 6px slid Library/Agents/Admin upward on every hover.
        One pinned height in both states is what stops that.
        """
        btn = RAIL_CSS.split('html[data-ui-layout="rail"] .rail.rail-icon-mode .rail-getstarted-btn {', 1)[1].split(
            "}", 1
        )[0]
        assert "min-height" in btn

    def test_reduced_motion_disables_the_width_transition(self) -> None:
        assert "@media (min-width: 1025px) and (prefers-reduced-motion: reduce)" in RAIL_CSS
        reduced = RAIL_CSS[RAIL_CSS.index("@media (min-width: 1025px) and (prefers-reduced-motion: reduce)") :]
        reduced = reduced[: reduced.index("\n}") + 2]
        assert "rail-icon-mode" in reduced
        assert "transition: none;" in reduced

    def test_tap_toggle_hidden_for_hover_capable_fine_pointer_devices(self) -> None:
        """A mouse already expands the rail on hover, and a keyboard caller
        already gets `:focus-within` — the visible tap button is withdrawn
        there (not simply left unrendered), and touch (no hover, no fine
        pointer) never matches this query, so it keeps seeing the base
        `display: flex` rule instead."""
        assert "@media (min-width: 1025px) and (hover: hover) and (pointer: fine)" in RAIL_CSS
        hover_capable = RAIL_CSS[RAIL_CSS.index("@media (min-width: 1025px) and (hover: hover) and (pointer: fine)") :]
        hover_capable = hover_capable[: hover_capable.index("\n}") + 2]
        assert ".rail-icon-toggle" in hover_capable
        assert "display: none;" in hover_capable

    def test_tap_toggle_base_rule_shows_it_by_default_in_icon_mode(self) -> None:
        """The un-narrowed rule (no hover/pointer condition) is what a touch
        caller actually sees — it must default to visible so withdrawing it
        for mouse/keyboard is an override, not the only rule."""
        block = self._desktop_block()
        assert re.search(r"\.rail-icon-mode \.rail-icon-toggle\s*\{\s*display: flex;", block)
