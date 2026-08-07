"""Chrome-layout switch (topnav/rail) + paper theme contract.

Three guarantees:

1. **Default-chrome regression guard** — with no theme/layout config,
   pages render the horizontal ``_app_header.html`` chrome and the
   ``blue`` palette exactly as before the paper redesign. Existing
   instances must see zero change without opting in.
2. **Opt-in rail layout** — ``AGNES_UI_LAYOUT=rail`` swaps the chrome
   for ``_app_rail.html`` (and only then).
3. **Paper theme registration** — ``AGNES_INSTANCE_THEME=paper`` stamps
   ``data-theme="paper"`` and the token sheet actually defines the
   palette block, so the value can't silently no-op.
"""

import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.instance_config import get_instance_theme, get_ui_layout


@pytest.fixture
def web_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    (tmp_path / "state").mkdir()
    (tmp_path / "analytics").mkdir()
    (tmp_path / "extracts").mkdir()
    from src.db import close_system_db

    close_system_db()
    from app.main import create_app

    app = create_app()
    yield TestClient(app)
    close_system_db()


@pytest.fixture
def admin_cookie(web_client):
    from argon2 import PasswordHasher

    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from tests.helpers.auth import grant_admin

    password = "AdminPass1!"
    conn = get_system_db()
    UserRepository(conn).create(
        id="admin1",
        email="admin@test.com",
        name="Admin",
        password_hash=PasswordHasher().hash(password),
    )
    grant_admin(conn, "admin1")
    conn.close()
    resp = web_client.post("/auth/token", json={"email": "admin@test.com", "password": password})
    assert resp.status_code == 200, f"Bootstrap failed: {resp.text}"
    return {"access_token": resp.json()["access_token"]}


class TestResolvers:
    def test_ui_layout_defaults_to_topnav(self, monkeypatch):
        monkeypatch.delenv("AGNES_UI_LAYOUT", raising=False)
        assert get_ui_layout() == "topnav"

    def test_ui_layout_env_rail(self, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        assert get_ui_layout() == "rail"

    def test_ui_layout_typo_falls_back(self, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "sidebar")
        assert get_ui_layout() == "topnav"

    def test_theme_accepts_paper(self, monkeypatch):
        monkeypatch.setenv("AGNES_INSTANCE_THEME", "paper")
        assert get_instance_theme() == "paper"

    def test_theme_typo_falls_back_to_blue(self, monkeypatch):
        monkeypatch.setenv("AGNES_INSTANCE_THEME", "papier")
        assert get_instance_theme() == "blue"


class TestDefaultChromeUnchanged:
    """Existing instances (no opt-in config) must keep today's chrome."""

    def test_default_renders_topnav_not_rail(self, web_client, admin_cookie, monkeypatch):
        monkeypatch.delenv("AGNES_UI_LAYOUT", raising=False)
        monkeypatch.delenv("AGNES_INSTANCE_THEME", raising=False)
        resp = web_client.get("/dashboard", cookies=admin_cookie)
        assert resp.status_code == 200
        assert 'class="app-header"' in resp.text
        assert 'class="rail"' not in resp.text
        assert 'data-ui-layout="topnav"' in resp.text
        assert 'data-theme="blue"' in resp.text

    def test_default_footer_is_config_copyright_not_keboola(self, web_client, admin_cookie, monkeypatch):
        """Default (blue/topnav) instances keep the config-driven copyright
        footer; the Keboola-branded credit ships only under the opt-in
        redesign (paper/rail). Regression guard for the #896 footer leak."""
        monkeypatch.delenv("AGNES_UI_LAYOUT", raising=False)
        monkeypatch.delenv("AGNES_INSTANCE_THEME", raising=False)
        resp = web_client.get("/dashboard", cookies=admin_cookie)
        assert resp.status_code == 200
        assert "AI Harness" in resp.text
        assert "<b>Keboola</b>" not in resp.text

    def test_default_favicon_is_svg_not_orb(self, web_client, admin_cookie, monkeypatch):
        """Default instances keep the original SVG favicon; the orb PNG is
        redesign-only."""
        monkeypatch.delenv("AGNES_UI_LAYOUT", raising=False)
        monkeypatch.delenv("AGNES_INSTANCE_THEME", raising=False)
        resp = web_client.get("/dashboard", cookies=admin_cookie)
        assert "favicon.svg" in resp.text
        assert "img/agnes-orb.png" not in resp.text


class TestRailBodyClearance:
    """The 240px body padding must be tied to the rail actually rendering.

    `data-ui-layout="rail"` is stamped on <html> from instance config, but the
    rail nav renders only for a signed-in user — so an unconditional padding
    survived onto pre-auth pages that have no rail, and `/login/password`
    centred its card inside a box shifted 240px right of the viewport (#1170).
    """

    def test_pre_auth_page_carries_no_rail(self, web_client, monkeypatch):
        """The premise of the fix: on a logged-out page the rail is absent
        while the layout attribute is still stamped."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        resp = web_client.get("/login/password")
        assert resp.status_code == 200
        assert 'data-ui-layout="rail"' in resp.text, "layout attribute should still be stamped"
        assert 'class="rail"' not in resp.text, "the rail nav must not render pre-auth"

    def test_body_clearance_is_conditional_on_the_rail(self, web_client):
        css = web_client.get("/static/css/rail.css").text
        assert 'html[data-ui-layout="rail"] body {' not in css, (
            "unconditional body padding is back — it applies on pre-auth pages that render no rail (#1170)"
        )
        assert 'html[data-ui-layout="rail"] body:has(.rail) {' in css

    def test_narrow_override_matches_the_desktop_selector(self, web_client):
        """`:has()` takes its argument's specificity, so the ≤1024px override
        must carry `:has(.rail)` too. A plain `body` there would lose to the
        desktop rule and keep reserving 240px in the top-bar layout — where the
        rail is a static block and the reservation is pure dead margin."""
        css = web_client.get("/static/css/rail.css").text
        narrow = css[css.index("@media (max-width: 1024px)") :]
        assert 'html[data-ui-layout="rail"] body:has(.rail) {\n        padding-left: 0;' in narrow, (
            "the narrow-screen override no longer matches the desktop rule's specificity"
        )


class TestRailOptIn:
    def test_rail_layout_swaps_chrome(self, web_client, admin_cookie, monkeypatch):
        # Probe a real rail landing surface (/stack). /dashboard is no longer a
        # rail render target — it 302s to /chat or /stack (see
        # TestDashboardLandingRedirect).
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        resp = web_client.get("/stack", cookies=admin_cookie)
        assert resp.status_code == 200
        assert 'class="rail"' in resp.text
        assert 'class="app-header"' not in resp.text
        assert 'data-ui-layout="rail"' in resp.text

    def test_rail_keeps_nav_contract(self, web_client, admin_cookie, monkeypatch):
        """Rail must carry the two-zone IA (Library + Agents as the bottom
        zone's flat destinations) and the same JS/id contract as the header:
        user menu, theme toggle."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        resp = web_client.get("/stack", cookies=admin_cookie)
        text = resp.text
        for anchor in (
            'id="userMenu"',
            'id="themeToggle"',
            # Library — private uploads (moved off My Stack) + future data apps.
            'href="/library"',
            # Agents — build an assistant out of the caller's stack. A primary
            # destination directly under Library (it used to sit one hover deep
            # inside the Studio dropdown).
            'href="/agents"',
            # brand lockup: the Agnes orb mark + the Agnes wordmark beside it.
            'class="rail-orb"',
            'class="rail-logo-txt"',
        ):
            assert anchor in text, f"rail chrome is missing {anchor}"
        # Rail nav items carry no WIP badge.
        assert 'class="rail-badge"' not in text
        # Library · Agents are the bottom zone, in that order, with no divider
        # between them (Admin is the only divided group — see
        # TestRailTwoZones). New chat renders only for chat-granted callers, so
        # it's not pinned here.
        assert 'class="rail-nav-sep"' not in text
        positions = [
            text.index('class="rail-nav rail-nav-bottom"'),
            text.index('href="/library"'),
            text.index('href="/agents"'),
        ]
        assert positions == sorted(positions), "rail nav items are out of order"
        # Catalog is a single flat destination — no nested subcategory tree.
        assert 'class="rail-sub"' not in text

    def test_rail_has_no_my_stack_entry(self, web_client, admin_cookie, monkeypatch):
        """My Stack is demoted out of the rail (#1088) — /stack stays a live
        route, it is simply not a primary destination any more. Asserted against
        the rail chrome slice, not the whole document: this probes the /stack
        page itself, whose body legitimately mentions the stack throughout."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        text = web_client.get("/stack", cookies=admin_cookie).text
        nav = text.split('<nav class="rail"', 1)[1].split("</nav>", 1)[0]
        assert 'href="/stack"' not in nav
        assert "My Stack" not in nav
        # ...and the page it points at is still served, not 404/redirected.
        assert web_client.get("/stack", cookies=admin_cookie).status_code == 200

    def test_library_answers_the_stack_question_in_its_toolbar(self, web_client, admin_cookie, monkeypatch):
        """No cross-link to /stack in the Library header — the toolbar's "In
        stack only" toggle answers "what can the default agent use?" against
        this same list, so a header link would point at a narrower view of the
        rows already on screen.

        This toggle is what makes the removal safe, so it is the thing worth
        pinning: if it stops answering the Stack question, My Stack needs an
        entry point again (#1088).

        The toggle is deliberately conditional — it renders only when flipping
        it would change the page (`0 < in_stack < total`), so it is absent when
        everything is in the Stack or nothing is, both cases where filtering is
        a no-op. Asserting its presence outright would just pin the fixture's
        membership mix, so this asserts the equivalence instead."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        resp = web_client.get("/library", cookies=admin_cookie)
        assert resp.status_code == 200
        text = resp.text
        head = text.split('class="lib-head"', 1)[1].split('class="fbar-dock"', 1)[0]
        assert 'href="/stack"' not in head

        rows = re.findall(r"<tr[^>]*\bdata-item-id=[^>]*>", text)
        top_level = [r for r in rows if "data-parent-id=" not in r]
        in_stack = [r for r in top_level if 'data-stack="in_stack"' in r]
        rendered = 'id="lib-stack-toggle"' in text
        assert rendered == (0 < len(in_stack) < len(top_level)), (
            f"stack toggle rendered={rendered} with {len(in_stack)}/{len(top_level)} rows in stack — "
            "it must render exactly when flipping it would change the list"
        )
        if not rendered:
            return

        # It is a toolbar button, NOT a row inside the Filter menu: the condition
        # is consequential enough to be visible at rest.
        assert "In stack only" in text
        assert 'data-facet="stack" data-facet-value="in_stack"' in text
        assert 'aria-pressed="false"' in text
        menu = text.split('id="lib-filter-menu"', 1)[1].split("</div>", 1)[0]
        assert "In stack only" not in menu, "the stack toggle must not also sit in the Filter menu"
        assert "fbar-menu__toggle" not in text, "retired in-menu toggle markup"

        # Order on the bar: Filter · In stack only · Sort. The toggle narrows the
        # list like Filter does, so it reads before the ordering control.
        positions = [
            text.index('id="lib-filter-btn"'),
            text.index('id="lib-stack-toggle"'),
            text.index('id="lib-sort"'),
        ]
        assert positions == sorted(positions), "stack toggle must sit between Filter and Sort"

    def test_stack_toggle_is_wired_as_an_external_facet(self, web_client, admin_cookie, monkeypatch):
        """The toolbar button is driven by the shared engine as an ordinary
        facet with `control`, not by page-local click handlers — so clearing and
        resetting keep working. Because the button shows its own state, the
        engine must leave it out of the Filter badge count and the chip row;
        those two exclusions are the whole reason the move is not a regression
        in discoverability."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        text = web_client.get("/library", cookies=admin_cookie).text
        assert "control: '#lib-stack-toggle'" in text

        js = web_client.get("/static/js/filter_toolbar.js").text
        # Excluded from the Filter button's badge...
        assert "return f.control ? n : n + facetState[f.key].size;" in js
        # ...and from the chip row.
        assert "if (f.control) return;" in js
        # State is pushed back onto the button from the one funnel every
        # mutation goes through, so Clear all / reset can't desync it.
        assert "syncExternalControls" in js

    def test_rail_has_no_studio_or_marketplace_entry(self, web_client, admin_cookie, monkeypatch):
        """Studio is retired from the rail and Marketplace is no longer a rail
        entry. Studio was a hover dropdown holding Agents (now its own top-level
        item), the Skill and Plugin builders (now reached from the Library
        header's "+ Add" menu) and a non-interactive "Corporate Memory builder"
        concept label. Both the trigger markup and the dead .rail-studio-*
        styling must be gone, not merely hidden."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        text = web_client.get("/stack", cookies=admin_cookie).text
        assert "rail-studio" not in text
        assert ">Studio<" not in text
        assert "Corporate Memory builder" not in text
        assert ">Marketplace<" not in text
        assert 'id="nav-catalog"' not in text
        # /catalog and /skills stay live routes — they are simply not rail
        # entries any more, so nothing in the rail should link to them.
        assert 'class="rail-i" href="/catalog"' not in text
        assert 'href="/skills"' not in text
        # The stylesheet carries no orphaned rules for the retired chrome —
        # the Studio dropdown, its "Maybe?" badge, or the group dividers.
        css = web_client.get("/static/css/rail.css").text
        assert "rail-studio" not in css
        assert "rail-badge--maybe" not in css
        assert "rail-nav-sep" not in css
        # The admin <details> section is NOT retired chrome — it is live, so
        # its rules must be present rather than absent. Admin expands in the
        # rail into one row per area, with each area's links in a flyout beside
        # the column (see test_rail_admin_expands_into_area_rows_with_flyouts).
        assert "rail-admin-summary" in css
        assert "rail-admin-groups" in css
        assert "rail-admin-flyout" in css
        # The retired /ask hero (#896) is gone: no rail nav item points at it,
        # and the Chat slot renders only when cloud-chat is actually reachable.
        assert 'href="/ask"' not in text
        # The in-rail global search box was removed — search no longer lives in
        # the sidebar chrome.
        assert 'id="global-search"' not in text

    def test_rail_catalog_renders_unified_page(self, web_client, admin_cookie, monkeypatch):
        """Under the rail layout /catalog is the unified browse surface
        (kind tabs over one grid); /stack is the unified personal
        collection."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        resp = web_client.get("/catalog", cookies=admin_cookie)
        assert resp.status_code == 200
        for anchor in (
            'data-kind="data"',
            'data-kind="plugins"',
            'data-kind="memory"',
            'data-kind="recipes"',
            'class="uc-kindtabs"',
        ):
            assert anchor in resp.text, f"unified catalog is missing {anchor}"
        # Uploads (file collections) are private user resources — they
        # live on My Stack, not in the shared Catalog.
        assert 'data-kind="library"' not in resp.text

        resp = web_client.get("/stack", cookies=admin_cookie)
        assert resp.status_code == 200
        assert "My Stack" in resp.text
        # Uploads moved OFF My Stack onto /library — the stack is a knowledge
        # inventory (data · plugins · artefacts · memory) only.
        assert 'data-kind="upload"' not in resp.text
        # The kind filter is now a Filter dropdown (multi-select "Type" facet)
        # with an active-chips row — the head tabs and the segmented control
        # are both retired.
        assert 'class="uc-kindtabs"' not in resp.text
        assert 'class="fbar-seg"' not in resp.text
        assert 'id="stk-filter-btn"' in resp.text
        assert 'id="stk-filter-menu"' in resp.text
        assert 'id="stk-chips"' in resp.text
        # Type facet options for each kind (incl. the plugins option).
        assert 'data-facet="kind"' in resp.text
        for kind in ("data", "plugins", "artefacts", "memory"):
            assert f'value="{kind}"' in resp.text
        # The manage zone groups resources into Required + Added by you (not a
        # card grid), with the tour anchor on the search + groups zone.
        assert 'id="stack-explore-zone"' in resp.text
        assert "Everything in your Stack" not in resp.text

    def test_library_page_hosts_uploads(self, web_client, admin_cookie, monkeypatch):
        """The caller's things live on /library — the renamed, widened former
        /artefacts. It carries the item count, the "+ Upload" affordance, the
        share dialog, and a "Data apps coming soon" badge on the Files band for
        the not-yet-built kind that will ship into that section."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        resp = web_client.get("/library", cookies=admin_cookie)
        assert resp.status_code == 200
        text = resp.text
        assert "Library" in text
        # Item count (no section heading — the count stands alone) + the
        # create-upload modal trigger.
        assert ">Uploads<" not in text
        assert 'id="lib-item-count"' in text
        assert "data-new-upload" in text
        assert 'id="uploadModal"' in text
        # Owner-initiated sharing: the dialog a grant-backed row's Sharing badge
        # opens ships with the page — as the shared component every detail page's
        # badge opens, not the page-local copy this page used to carry.
        assert "js/components/share_dialog.js" in text
        assert "css/share_dialog.css" in text
        assert 'id="shareModal"' not in text, "the share dialog is a component, not page markup"
        # Every "add something" path sits behind ONE chevron button.
        assert 'id="lib-new-btn"' in text
        assert 'id="lib-new-menu"' in text
        for label in ("Build a skill", "Build a plugin", "Upload a file"):
            assert f"<span>{label}</span>" in text
        # The page-head `.pnote` caveats are retired, and so are the two tinted
        # panels that replaced them. What is left above the inventory is ONE thin
        # row (the caveat about the list), and Data apps states its schedule as a
        # badge on the band it will ship into.
        assert 'class="pnote"' not in text
        assert "Content being prepared" not in text
        assert "lib-status" not in text
        assert 'class="lib-count-note"' in text
        assert ">More coming soon<" in text
        assert "lib-strip" not in text
        # The Data apps badge is NOT asserted here, and its absence is correct:
        # this instance has no files, so there is no Files band to carry it. The
        # schedule now lives on the section the kind will ship into, which means
        # an empty Library states it nowhere — a deliberate consequence of moving
        # it out of the page head. See
        # test_web_library_sharing.test_data_apps_schedule_is_a_badge_on_the_files_band,
        # which seeds content and asserts the badge for real.
        assert 'class="fbar-group__soon"' not in text
        # The banners these replaced are gone, class and all.
        assert "lib-soon" not in text
        assert "lib-apps" not in text
        # The "same knowledge, everywhere" connect banner closes the Library
        # header (it moved here from the My Stack header).
        assert 'class="cbn cbn--bar"' in text
        assert "Connect your AI tools to give them access to the same knowledge." in text
        # Agents are NOT a Library kind — they live on /agents.
        assert 'data-kind="agent"' not in text
        assert "Build an agent" not in text

    def test_artefacts_redirects_to_library(self, web_client, admin_cookie, monkeypatch):
        """/artefacts was renamed to /library and redirects there, so old links,
        bookmarks and the onboarding tour keep working."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        resp = web_client.get("/artefacts", cookies=admin_cookie, follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == "/library"

    def test_agents_page_renders_builder(self, web_client, admin_cookie, monkeypatch):
        """/agents hosts the agent builder (WIP): list + builder views, the
        server-rendered RBAC-scoped knowledge ingredients, and the
        capabilities hydration off everything available to the caller. Agent
        definitions persist SERVER-SIDE in the v103 agents registry, so they
        follow the user across devices and can be shared from the Library."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        resp = web_client.get("/agents", cookies=admin_cookie)
        assert resp.status_code == 200
        text = resp.text
        assert "Agents" in text
        # Both in-page views + the create affordance.
        assert 'id="ag-list-view"' in text
        assert 'id="ag-builder-view"' in text
        # The create affordance moved into the JS-rendered list (the old
        # server-rendered id="ag-new-btn" header button is gone), so assert on
        # the delegated hook the list renders instead.
        assert "data-ag-new" in text
        # Real ingredients: server-embedded knowledge JSON + client-side
        # capabilities hydrated from EVERYTHING available to the caller
        # (curated ∪ community store), not just what's already in their stack.
        assert 'id="ag-knowledge-data"' in text
        assert "pull('curated')" in text and "pull('flea')" in text
        assert "tab=my" not in text  # subscribed-only pool is retired
        # Knowledge now spans a third kind — the caller's files/artefacts —
        # alongside governed data + memory.
        assert "data, memory & files" in text
        # Available-but-not-yet-added marketplace items are surfaced and the
        # ones already in the stack are marked (not filtered out).
        assert "In your stack" in text
        assert "any plugin or skill available to you" in text.lower()
        # Persistence note reflects the v103 server-side registry: agents are
        # saved to the workspace and listed on THIS page (they are not Library
        # entries and there is no sharing affordance yet), and the remaining
        # WIP is actually RUNNING them on the surfaces the builder offers.
        assert "saved to your workspace" in text
        assert "live on this page" in text
        assert "saved in this browser" not in text
        assert "where you can share them" not in text
        # It reads as the same quiet product notice the Library uses, and it
        # sits in the page head under the description rather than trailing the
        # grid. The builder keeps its own copy (the head is hidden there), so
        # the markup appears twice — once server-rendered, once in the JS.
        assert 'class="pnote"' in text
        assert "ag-localnote" not in text
        assert text.index('class="lede"') < text.index('class="pnote"')
        assert text.index('class="pnote"') < text.index('id="ag-list-view"')

    def test_agents_page_opens_builder_from_query(self, web_client, admin_cookie, monkeypatch):
        """The builder is an in-page view, so the Library reaches it through
        query params: `?new=1` (the "Build an agent" CTA) lands straight in the
        builder on a fresh agent, and `?open=<id>` (the Library's agent cards)
        opens that agent. Without this the page always rendered the LIST and
        both deep links silently dead-ended one click short of the builder."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        resp = web_client.get("/agents", cookies=admin_cookie)
        assert resp.status_code == 200
        text = resp.text
        assert "routeFromQuery" in text
        assert "params.get('open')" in text
        assert "params.get('new')" in text
        # Boot routes through the query BEFORE falling back to the list.
        assert "if (!routeFromQuery()) renderList();" in text
        # One-shot — the param is stripped so a reload can't mint a second agent.
        assert "params.delete('new')" in text
        # `?new=1` shares the create path with the "+ New agent" button rather
        # than rendering an unsaved shell (the server owns the id).
        assert "createAgent(null)" in text

    def test_agent_builder_has_delete_action(self, web_client, admin_cookie, monkeypatch):
        """The builder can delete the agent it is configuring — previously the
        only Delete lived on the list card, so the detail view was a dead end
        for the one destructive action. It sits LEFT of the status button
        (Mark ready / Back to draft), reuses the list's `data-ag-del` hook and
        its DELETE /api/agents/{id} handler, and — unlike the list card —
        confirms first, because here it is one button away from a primary
        action on the config the caller is looking at."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        resp = web_client.get("/agents", cookies=admin_cookie)
        assert resp.status_code == 200
        text = resp.text
        assert 'class="cc-btn ag-del-btn" data-ag-del=' in text
        # Left of the status button, inside the builder's action group.
        actions = text.index('<div class="ag-build-actions">')
        assert actions < text.index('class="cc-btn ag-del-btn"') < text.index("data-ag-status")
        # Confirms only for the builder button; the list card is unchanged.
        assert "window.confirm(" in text
        assert "t.classList.contains('ag-del-btn')" in text
        # A pending debounced PATCH must not outlive the row it would write to.
        assert "clearTimeout(saveTimers[id]);" in text

    def test_agents_page_has_no_default_agent_card(self, web_client, admin_cookie, monkeypatch):
        """/agents lists the caller's OWN agents only — the always-on baseline
        assistant is not a card here (it is configured from /stack)."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        resp = web_client.get("/agents", cookies=admin_cookie)
        assert resp.status_code == 200
        text = resp.text
        assert "ag-card--default" not in text
        assert "Default agent" not in text
        assert "ag-badge--system" not in text
        # With no agents yet the grid falls back to the build-your-first empty
        # state rather than a stack-derived card.
        assert "No agents yet" in text

    def test_agents_page_requires_auth(self, web_client, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        resp = web_client.get("/agents", follow_redirects=False)
        assert resp.status_code in (302, 303, 307, 401, 403)

    def test_topnav_catalog_keeps_classic_page(self, web_client, admin_cookie, monkeypatch):
        """Default layout must keep the classic catalog.html — the
        unified page is rail-only."""
        monkeypatch.delenv("AGNES_UI_LAYOUT", raising=False)
        resp = web_client.get("/catalog", cookies=admin_cookie)
        assert resp.status_code == 200
        assert 'class="uc-kindtabs"' not in resp.text
        assert "stack-tabs" in resp.text

    def test_paper_theme_stamped(self, web_client, admin_cookie, monkeypatch):
        monkeypatch.setenv("AGNES_INSTANCE_THEME", "paper")
        resp = web_client.get("/dashboard", cookies=admin_cookie)
        assert resp.status_code == 200
        assert 'data-theme="paper"' in resp.text

    def test_paper_footer_is_config_driven_and_keeps_the_orb(self, web_client, admin_cookie, monkeypatch):
        """The redesign carries no vendor branding: the paper footer renders the
        same config-driven copyright as every other chrome (an instance puts its
        own name there via INSTANCE_COPYRIGHT), while the orb favicon — a
        neutral product mark — stays redesign-only."""
        monkeypatch.setenv("AGNES_INSTANCE_THEME", "paper")
        resp = web_client.get("/dashboard", cookies=admin_cookie)
        assert resp.status_code == 200
        assert "AI Harness" in resp.text
        assert "<b>Keboola</b>" not in resp.text
        assert "img/agnes-orb.png" in resp.text


class TestRailChatHistory:
    """Rail chat-history migration (#896): the conversation history lives in the
    left rail, directly under New chat and present on every page (not just
    /chat). It is TWO collapsible sections — Pinned, then Chats — with no
    truncation and no "View all chats" control. The standalone "+ New chat"
    button is retired and the chat entry is renamed "New chat" (id="new-chat", so
    chat.js resets in place on /chat). All gated on can_chat. Topnav is
    unaffected — its in-page chat sidebar is unchanged."""

    def _enable_chat(self, web_client, monkeypatch):
        """Make can_chat true: chat enabled AND an explicit CHAT grant (admin
        god-mode does NOT short-circuit has_explicit_grant, so patch it)."""
        import app.auth.access as access

        monkeypatch.setattr(access, "has_explicit_grant", lambda *a, **k: True)
        web_client.app.state.chat_config = SimpleNamespace(enabled=True)

    def test_rail_renders_history_section(self, web_client, admin_cookie, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        self._enable_chat(web_client, monkeypatch)
        # Probe a NON-chat rail page — the history must render everywhere.
        resp = web_client.get("/stack", cookies=admin_cookie)
        assert resp.status_code == 200
        text = resp.text
        # History section + the reused chat list ids live in the rail.
        assert 'class="rail-history"' in text
        assert 'id="chat-list"' in text
        assert 'id="cloud-chat-empty-state"' in text
        # The chat entry is renamed and carries id="new-chat" (chat.js hook).
        assert 'id="new-chat"' in text
        assert "New chat" in text
        # The standalone +New chat button above the nav (old markup) is retired.
        assert 'class="rail-newchat"' not in text
        # The conversations are TWO collapsible sections (see
        # TestRailChatSections for the full contract).
        assert 'id="rail-pinned"' in text
        assert 'id="rail-chats"' in text
        # The heading is a quiet section LABEL, not the `.rail-i`-styled summary
        # row with a clock glyph that #896 shipped and retired: styled as a nav
        # destination, it read as a fourth Library/Agents/Admin.
        assert 'class="rail-i rail-history-summary"' not in text
        assert "rail-history-summary-txt" not in text
        assert "rail-history-caret" not in text
        # The recent feed is capped and closes on a LINK to the Chats page — not
        # on an in-place expander, which is what the retired "Show more" was
        # (see test_recents_are_capped_by_a_link_not_by_an_expander).
        assert "rail-history-more" not in text
        assert "Show less" not in text
        assert 'id="rail-view-all-chats"' in text
        assert 'href="/chats"' in text
        # The loader that fills the list off /chat is wired in.
        assert "js/rail_history.js" in text

    def test_rail_onboarding_card_hosts_the_panel(self, web_client, admin_cookie, monkeypatch):
        """Onboarding's rail presence is ONE compact card at the head of the
        bottom zone, opening the checklist as a popover. It replaces the
        "Finish setup · N/5" text row (and, before that, the "Your journey"
        checklist inline at the bottom of the chat list): a row in a column of
        rows is easy to read past."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        self._enable_chat(web_client, monkeypatch)
        resp = web_client.get("/stack", cookies=admin_cookie)
        assert resp.status_code == 200
        # Asserted against the rail chrome slice, not the whole document — the
        # page body is free to say "Get started" in its own copy.
        text = resp.text.split('<nav class="rail"', 1)[1].split("</nav>", 1)[0]
        # The card + its popover. The ids are the JS contract chat_onboarding.js
        # and rail_history.js bind to, so they outlive the relabelling.
        assert 'id="rail-getstarted-toggle"' in text
        assert 'id="rail-getstarted-panel"' in text
        # Card anatomy: title, progress sentence, bar, chevron.
        assert 'id="rail-getstarted-title"' in text
        assert ">Set up Agnes<" in text
        assert 'id="rail-getstarted-count"' in text
        assert 'id="rail-getstarted-bar"' in text
        assert "rail-getstarted-chev" in text
        # The progress line renders EMPTY — a static "0 of 5" would flash the
        # wrong number at anyone mid-way through.
        assert '<span class="rail-getstarted-sub" id="rail-getstarted-count"></span>' in text
        # Retired labels.
        assert "Your Journey" not in text
        assert "Get started" not in text
        assert "rail-getstarted-check" not in text
        # The card CLOSES the bottom zone: under Admin, above the profile.
        card_pos = text.find('class="rail-getstarted"')
        assert text.find('class="rail-admin"') < card_pos < text.find('class="rail-foot"'), (
            "the onboarding card belongs under Admin, above the profile"
        )
        # ...and the foot below it is the profile alone.
        foot = text.split('class="rail-foot"', 1)[1]
        assert "rail-getstarted" not in foot
        # The journey render target moved into the popover — and out of the list.
        journey_pos = text.find('id="chat-journey"')
        panel_pos = text.find('id="rail-getstarted-panel"')
        assert journey_pos != -1 and panel_pos != -1
        assert journey_pos > panel_pos, "#chat-journey must render inside the card's popover"
        assert text.find('id="chat-journey"', text.find('class="rail-history"'), panel_pos) == -1, (
            "#chat-journey must no longer sit in the chat-history section"
        )
        # Off /chat, the standalone mount fills the popover (a script AFTER the
        # rail chrome, so this one is checked against the whole document).
        assert "mountJourneyPanel" in resp.text

    def test_onboarding_card_styling_contract(self, web_client, admin_cookie, monkeypatch):
        """Subtle blue (the DS's informational family, NOT the brand primary —
        that stays reserved for the active destination), and gone entirely at
        5/5."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        css = web_client.get("/static/css/rail.css").text
        btn = css.split('html[data-ui-layout="rail"] .rail-getstarted-btn {', 1)[1].split("}", 1)[0]
        assert "background: var(--ds-accent-info-bg)" in btn
        # Declarations only — the block's comment explains why the brand accent
        # is NOT used here, so a naive substring check would trip on itself.
        decls = re.sub(r"/\*.*?\*/", "", btn, flags=re.S)
        assert "--ds-primary" not in decls
        fill = css.split('html[data-ui-layout="rail"] .rail-getstarted-bar-fill {', 1)[1].split("}", 1)[0]
        assert "background: var(--ds-accent-info-line)" in fill
        assert "width: 0" in fill  # server renders empty; JS sets the real width
        complete = css.split('html[data-ui-layout="rail"] .rail-getstarted.is-complete {', 1)[1].split("}", 1)[0]
        assert "display: none" in complete

    def test_onboarding_card_title_and_progress_are_js_driven(self, web_client, admin_cookie, monkeypatch):
        """ "Set up Agnes" until the first step lands, "Continue setup" after —
        and the bar width follows the same count."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        js = web_client.get("/static/js/chat_onboarding.js").text
        body = js.split("function updateGetStartedIndicator(", 1)[1].split("\n}", 1)[0]
        assert 'done > 0 ? "Continue setup" : "Set up Agnes"' in body
        assert "${done} of ${total} steps complete" in body
        assert "(done / total) * 100" in body
        assert 'classList.toggle("is-complete"' in body

    def test_new_token_button_cancels_the_summary_toggle(self, web_client, admin_cookie):
        """`+ New token` lives inside a <summary>, so it must cancel the disclosure.

        stopPropagation() alone is NOT enough and was the original bug: it keeps
        the click off ancestor listeners, but a <details> toggle is the summary's
        default ACTIVATION BEHAVIOUR, which only preventDefault() cancels. With
        just the former, minting a token also collapsed the section it was
        launched from.
        """
        resp = web_client.get("/me/profile", cookies=admin_cookie)
        assert resp.status_code == 200
        marker = 'id="new-token-btn"'
        assert marker in resp.text
        handler = resp.text[resp.text.index(marker) - 400 : resp.text.index(marker) + 400]
        assert "preventDefault()" in handler, "New token must cancel the <summary> default action, not only bubbling"
        assert "stopPropagation()" in handler

    def test_profile_menu_can_restart_onboarding(self, web_client, admin_cookie, monkeypatch):
        """The way back once the Finish setup row has retired itself at 5/5: the
        row's own "Start over" goes with it, so the profile menu — the one thing
        pinned to the rail in every state — carries the entry."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        self._enable_chat(web_client, monkeypatch)
        rail = (
            web_client.get("/stack", cookies=admin_cookie).text.split('<nav class="rail"', 1)[1].split("</nav>", 1)[0]
        )
        assert 'id="rail-restart-onboarding"' in rail
        assert "Start over onboarding" in rail
        # Inside the profile menu panel, after Profile. The landmark used to be
        # "Learn how it works" (href="/home"), which no longer lives in this
        # menu — it became a rail row of its own pointing at /how-it-works, so
        # Profile is now the first item the entry must follow.
        panel_pos = rail.find('id="userMenuPanel"')
        profile_pos = rail.find('href="/me/profile"')
        entry_pos = rail.find('id="rail-restart-onboarding"')
        assert -1 < panel_pos < profile_pos < entry_pos
        # A <button>, not a link — it has no page to navigate to; the handler
        # lives in chat_onboarding.js.
        assert '<button type="button" class="app-user-menu-item app-user-menu-btn"' in rail

    def test_restart_onboarding_entry_is_chat_gated(self, web_client, admin_cookie, monkeypatch):
        """No chat grant → no onboarding row and nothing to restart."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        resp = web_client.get("/stack", cookies=admin_cookie)
        assert 'id="rail-restart-onboarding"' not in resp.text

    def test_pinned_and_chats_are_two_collapsible_sections(self, web_client, admin_cookie, monkeypatch):
        """Pinned is its OWN section, above Chats, and either can be put away.

        The two lists answer different questions — a curated shelf vs. a
        chronological feed — and as sibling groups in one list they could only be
        told apart by a header that scrolled away, with neither one closable. So:
        a section each, Pinned first, each with a disclosure and its own list."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        self._enable_chat(web_client, monkeypatch)
        text = web_client.get("/stack", cookies=admin_cookie).text
        rail = text.split('<nav class="rail"', 1)[1].split("</nav>", 1)[0]

        # Anatomy of each section: a labelled disclosure button wired to its own
        # body, and a list of its own.
        # "Recent", not "Chats": the feed is a capped slice with "View all chats"
        # under it, and a section labelled "Chats" over a slice would make that
        # link read as a second route to the same list.
        for sec, toggle, body, label in (
            ("rail-pinned", "rail-pinned-toggle", "rail-pinned-body", "Pinned"),
            ("rail-chats", "rail-chats-toggle", "rail-chats-body", "Recent"),
        ):
            assert f'id="{sec}"' in rail
            assert f'id="{toggle}"' in rail
            assert f'aria-controls="{body}"' in rail, f"{toggle} must point at its own body"
            assert f'id="{body}"' in rail
            assert f'<span class="rail-chatsec-txt">{label}</span>' in rail
        # Both start expanded server-side; rail_history.js applies the persisted
        # state before any rows arrive.
        assert rail.count('aria-expanded="true"') >= 2
        # Two lists, and the chat page's ids are still the ones the renderers
        # bind to (chat.js owns #chat-list on /chat unchanged).
        assert 'id="pinned-chat-list"' in rail
        assert 'id="chat-list"' in rail
        # Pinned above Chats, and the empty state lives in Chats (the section
        # that survives a first run) rather than between them.
        assert rail.find('id="rail-pinned"') < rail.find('id="rail-chats"') < rail.find('id="cloud-chat-empty-state"')
        # Pinned starts hidden — a "Pinned" header over nothing is dead chrome,
        # so a renderer unhides it only once it has rows.
        pinned_open = rail[rail.find('<section class="rail-chatsec" id="rail-pinned"') :][:120]
        assert " hidden" in pinned_open, "the Pinned section must start hidden"

    def test_chat_section_headers_are_labels_not_rows(self, web_client, admin_cookie, monkeypatch):
        """The headers keep the group-label voice they were promoted from —
        10.5px/700 uppercase muted ink — and stay LABELS: no background in any
        state, no border, no divider.

        This is the rail's colour rule, not taste (see
        test_accent_marks_where_you_are_and_nothing_else): the accent marks the
        active conversation and the neutral wash marks hovering an actual row, so
        a header that filled on hover would read as a selectable row. Ink is the
        one channel still free, so hover spends that."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        css = web_client.get("/static/css/rail.css").text

        def block_for(selector):
            return css.split(selector, 1)[1].split("}", 1)[0]

        hd = block_for('html[data-ui-layout="rail"] .rail-chatsec-hd {')
        assert "background: none" in hd
        assert "border: none" in hd
        assert "border-top" not in hd and "border-bottom" not in hd
        assert "color: var(--ds-text-muted)" in hd
        # Left edge shared with the conversation titles below (rows: `padding:
        # 4px 9px`) — a section label indented differently from its own list
        # reads as a misalignment in a 240px column.
        assert "9px" in hd
        hover = block_for('html[data-ui-layout="rail"] .rail-chatsec-hd:hover {')
        assert "color: var(--ds-text-secondary)" in hover
        assert "background" not in hover, "a filled header would read as a row"
        txt = block_for('html[data-ui-layout="rail"] .rail-chatsec-txt {')
        assert "font-size: 10.5px" in txt
        assert "font-weight: 700" in txt
        assert "text-transform: uppercase" in txt
        # The caret annotates the label, so it sits against it — not flushed to
        # the rail's right edge, where it read as a second column of controls
        # beside the conversation rows' own "⋮". No count rides out there either.
        caret = block_for('html[data-ui-layout="rail"] .rail-chatsec-caret {')
        assert "margin-left: auto" not in caret
        assert "rail-chatsec-count" not in css
        # Keyboard reachability — it is a real <button>, so it must show focus.
        assert "outline: var(--ds-focus-outline)" in block_for(
            'html[data-ui-layout="rail"] .rail-chatsec-hd:focus-visible {'
        )
        # Whitespace separates the sections; the rail's only nav divider is
        # Admin's (test_admin_is_the_only_divided_group).
        gap = block_for('html[data-ui-layout="rail"] .rail-chatsec + .rail-chatsec {')
        assert "margin-top" in gap
        assert "border" not in gap

    def test_any_date_boundary_stays_subordinate_to_the_section_label(self, web_client, admin_cookie, monkeypatch):
        """A date boundary ("Older") lives INSIDE a section, so it cannot wear the
        same uppercase-700 label voice as that section's own header — two labels
        of equal weight nested one inside the other read as siblings, i.e. as two
        sections with the rows above the second one orphaned.

        Conditional on the rail styling date headers at all: whether a capped
        feed is short enough to need no date labels is a separate call, and this
        guard is about the hierarchy that applies whenever they DO render."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        css = web_client.get("/static/css/rail.css").text
        selector = 'html[data-ui-layout="rail"] .rail-history .cloud-chat-list-group-header {'
        if selector not in css:
            return  # the rail renders no date headers — nothing to keep in line
        block = css.split(selector, 1)[1].split("}", 1)[0]
        assert "text-transform: none" in block
        assert "letter-spacing: 0" in block
        assert "font-weight: 600" in block

    def test_section_collapse_is_persisted(self, web_client, admin_cookie, monkeypatch):
        """One owner for the section chrome — rail_history.js, loaded on every
        rail page INCLUDING /chat (where chat.js owns only the rows, and calls in
        through window.railChatSections).

        The open state survives navigation: a disclosure that forgets what you
        did to it on every page load reads as broken, and the choice ("I live in
        my pins") is about how the caller works, not about the page they're on."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        js = web_client.get("/static/js/rail_history.js").text
        assert "agnes.rail.chatsec." in js, "the open state must be persisted per section"
        assert "localStorage.setItem" in js and "localStorage.getItem" in js
        # Default OPEN: only an explicit "0" closes a section, so a first-time
        # caller never has to discover a disclosure to see their chats.
        assert '!== "0"' in js
        assert 'setAttribute("aria-expanded"' in js
        assert "window.railChatSections" in js, "chat.js needs a seam to re-sync after a render"
        chat_js = web_client.get("/static/js/chat.js").text
        assert "window.railChatSections" in chat_js, "/chat must re-sync the sections it re-renders"
        css = web_client.get("/static/css/rail.css").text
        assert 'html[data-ui-layout="rail"] .rail-chatsec.is-collapsed .rail-chatsec-caret {' in css

    def test_rail_history_absent_without_chat_grant(self, web_client, admin_cookie, monkeypatch):
        """No chat reachability → no history section, no New chat item, no
        Finish setup row, no loader (matches the "Chat slot only when
        reachable" contract)."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        # Chat is disabled by default in tests, so can_chat is False.
        resp = web_client.get("/stack", cookies=admin_cookie)
        assert resp.status_code == 200
        assert 'class="rail-history"' not in resp.text
        assert 'id="new-chat"' not in resp.text
        assert 'id="rail-getstarted-toggle"' not in resp.text
        assert "js/rail_history.js" not in resp.text


class TestRailTwoZones:
    """Rail IA: two fixed zones with the conversation list between them.

    Top zone   — New chat, then the Pinned + Chats sections.
    (scroll)   — the rest of both lists, inside .rail-history-body.
    Bottom     — Library · Agents, then Admin behind a divider, then the
                 onboarding card, then the profile pinned to the very bottom.

    The order is the whole point of the layout, so it is asserted as one
    top-to-bottom sequence rather than per-item.
    """

    def _enable_chat(self, web_client, monkeypatch):
        import app.auth.access as access

        monkeypatch.setattr(access, "has_explicit_grant", lambda *a, **k: True)
        web_client.app.state.chat_config = SimpleNamespace(enabled=True)

    def _rail(self, web_client, admin_cookie):
        text = web_client.get("/stack", cookies=admin_cookie).text
        return text.split('<nav class="rail"', 1)[1].split("</nav>", 1)[0]

    def test_zone_order(self, web_client, admin_cookie, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        self._enable_chat(web_client, monkeypatch)
        rail = self._rail(web_client, admin_cookie)
        sequence = [
            'class="rail-nav rail-nav-top"',  # zone 1
            'id="new-chat"',
            'class="rail-history"',  # the conversations region…
            'id="rail-pinned"',  # …Pinned first,
            'id="chat-list"',  # …then Chats
            'class="rail-nav rail-nav-bottom"',  # zone 2
            'href="/library"',
            'href="/agents"',
            'class="rail-admin"',  # ...Admin behind a divider
            'class="rail-getstarted"',  # ...then the onboarding card
            'class="rail-foot"',
            'id="userMenu"',  # profile, at the very bottom
        ]
        positions = [rail.find(anchor) for anchor in sequence]
        assert -1 not in positions, [a for a, p in zip(sequence, positions) if p == -1]
        assert positions == sorted(positions), "rail zones are out of order"

    def test_admin_is_the_only_divided_group(self, web_client, admin_cookie, monkeypatch):
        """Admin carries the divider; Library/Agents and the recents do not —
        the two zones are separated by the scroll region between them, not by
        rule lines. Pinned in CSS because that is where the dividers live."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        css = web_client.get("/static/css/rail.css").text

        def block_for(selector):
            body = css.split(selector, 1)[1].split("}", 1)[0]
            return body

        assert "border-top" in block_for('html[data-ui-layout="rail"] .rail-admin {')
        # The profile keeps its own divider (it is the very bottom row).
        assert "border-top" in block_for('html[data-ui-layout="rail"] .rail-user-menu {')
        # The conversations region draws none — the Pinned/Chats section labels
        # are separated by whitespace, not rules (see
        # test_chat_section_headers_are_labels_not_rows).
        assert "border-top" not in block_for('html[data-ui-layout="rail"] .rail-history {')
        assert "border-top" not in block_for('html[data-ui-layout="rail"] .rail-nav-bottom {')

    def test_accent_marks_where_you_are_and_nothing_else(self, web_client, admin_cookie, monkeypatch):
        """The rail's colour rule: the accent tint marks the ACTIVE row, hover is
        a neutral wash, and nothing carries a resting tint of its own.

        The rail has held every arrangement of this, so what the guard protects
        is the invariant rather than the values: exactly ONE thing in the column
        may own the accent. It broke when New chat took a standing tint while
        active rows were tinted too (one hue, four meanings), and again when
        hover took the accent while New chat still had it (a hovered row looked
        selected). Active wins the accent because it is persistent wayfinding;
        hover only has to be perceptible, since the pointer is already there."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        css = web_client.get("/static/css/rail.css").text

        def block_for(selector):
            return css.split(selector, 1)[1].split("}", 1)[0]

        # One shared token per state, so the three rules of each cannot drift.
        assert "--rail-active-bg:" in css
        assert "--rail-hover-bg:" in css
        for selector in (
            'html[data-ui-layout="rail"] .rail-i.on {',
            'html[data-ui-layout="rail"] .rail-history .cloud-chat-list li.active,',
            'html[data-ui-layout="rail"] .rail-admin-flyout-item.is-active {',
        ):
            assert "background: var(--rail-active-bg)" in block_for(selector), selector
        for selector in (
            'html[data-ui-layout="rail"] .rail-i:hover {',
            'html[data-ui-layout="rail"] .rail-history .cloud-chat-list li[data-id]:hover {',
            'html[data-ui-layout="rail"] .rail-admin-flyout-item:hover {',
        ):
            assert "background: var(--rail-hover-bg)" in block_for(selector), selector

        # Active owns the accent; hover is neutral. Exactly one owner.
        active_token = css.split("--rail-active-bg:", 1)[1].split(";", 1)[0]
        hover_token = css.split("--rail-hover-bg:", 1)[1].split(";", 1)[0]
        assert "--ds-primary" in active_token, "the active row must own the accent"
        assert "--ds-primary" not in hover_token, "hover must not spend the accent a second time"
        # If both states ever go neutral again, the stronger must be an ink-mix:
        # the surface ramp is not monotonic across themes (in dark, `sunken`
        # lands darker than `dim`), so the two would swap places.
        assert "--ds-surface-sunken" not in active_token

    def test_active_row_keeps_its_tint_on_hover(self, web_client, admin_cookie, monkeypatch):
        """Pointing at the conversation you are reading must not un-highlight it.

        This is a specificity trap, not a style choice. The conversation hover
        rule is `… .cloud-chat-list li[data-id]:hover` (five units) and the
        active rule is `… .cloud-chat-list li.is-active` (four), so the hover
        wash wins on the active row unless the active rule ALSO carries
        `:hover` selectors — meaning the open chat would visibly lose its accent
        exactly when you reached for it. The nav rows and Admin links tie on
        specificity, so source order already protects them."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        css = web_client.get("/static/css/rail.css").text
        for selector in (
            'html[data-ui-layout="rail"] .rail-history .cloud-chat-list li[data-id].is-active:hover',
            'html[data-ui-layout="rail"] .rail-history .cloud-chat-list li[data-id].active:hover',
        ):
            assert selector in css, f"missing {selector} — the open chat loses its tint on hover"
        # ...and they must resolve to the ACTIVE fill, not the hover one.
        block = css.split('html[data-ui-layout="rail"] .rail-history .cloud-chat-list li[data-id].active:hover', 1)[
            1
        ].split("}", 1)[0]
        assert "background: var(--rail-active-bg)" in block
        # Source order still has to keep `.rail-i.on` after `.rail-i:hover`,
        # since those two tie and nothing else separates them.
        assert css.index('html[data-ui-layout="rail"] .rail-i.on {') > css.index(
            'html[data-ui-layout="rail"] .rail-i:hover {'
        ), "`.rail-i.on` must come after `.rail-i:hover` — they tie on specificity"

        # The retired CTA treatments, in markup and CSS.
        assert "rail-newchat-item" not in css
        rail = self._rail(web_client, admin_cookie)
        assert "rail-newchat-item" not in rail

    def test_new_chat_is_an_ordinary_row(self, web_client, admin_cookie, monkeypatch):
        """New chat is a plain `.rail-i`, styled identically to Library / Agents /
        Admin, with NO treatment of its own.

        It held a dedicated `.rail-compose` control look in three variants —
        grey fill + border + tinted icon chip, solid `--ds-primary`, and pale
        `--ds-primary-light` — and all three are retired. The reason is the
        accent budget, not taste: a standing tint on one row meant hover could
        not use the accent (a hovered Library row became identical to New chat at
        rest) and `.on` needed an inset ring purely to separate two pale-blue
        things. Feedback that fires on every row beats decoration on one.

        Asserted as an absence, so a fourth variant cannot land without
        confronting `test_accent_marks_where_you_are_and_nothing_else`."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        self._enable_chat(web_client, monkeypatch)
        rail = self._rail(web_client, admin_cookie)
        # It carries the ORDINARY row class and its bare glyph, like every
        # other nav row — no wrapper span, no icon chip.
        assert re.search(r'class="rail-i[^"]*"\s+id="new-chat"', rail)
        assert "rail-compose" not in rail

        css = web_client.get("/static/css/rail.css").text
        # Selector forms, not the bare word: the stylesheet's New-chat section
        # names `.rail-compose` to record the three retired variants, and a guard
        # that bans the name would force that history to be deleted.
        for selector in (".rail-compose {", ".rail-compose:", ".rail-compose.", ".rail-compose-icon"):
            assert selector not in css, f"New chat must not have rules of its own ({selector})"
        # Whitespace, not a heading and not a treatment, separates it from the
        # list — the one thing that still marks the boundary.
        top = css.split('html[data-ui-layout="rail"] .rail-nav-top {', 1)[1].split("}", 1)[0]
        assert "margin-bottom" in top
        narrow = css.split("@media (max-width: 1024px)", 1)[1]
        # In the bar the column gap reads as a stray gap, so it is dropped.
        assert ".rail-nav-top {\n        margin-bottom: 0;" in narrow

    def test_rows_share_one_height(self, web_client, admin_cookie, monkeypatch):
        """Consistent row heights across the ladder: nav rows, conversation
        rows and the profile row all size off `--rail-row-h`."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        css = web_client.get("/static/css/rail.css").text
        assert "--rail-row-h:" in css
        for selector in (
            'html[data-ui-layout="rail"] .rail-i {',
            'html[data-ui-layout="rail"] .rail-history .cloud-chat-list li[data-id] {',
            'html[data-ui-layout="rail"] .rail-user {',
        ):
            body = css.split(selector, 1)[1].split("}", 1)[0]
            assert "min-height: var(--rail-row-h)" in body, selector

    def test_recents_are_capped_by_a_link_not_by_an_expander(self, web_client, admin_cookie, monkeypatch):
        """The region still fills the free space between the two zones and scrolls
        inside its own box, in ONE state — so the bottom zone never moves.

        What changed with /chats: the recent feed is capped again. The cap that
        was removed had nowhere to go (five rows on a screen with room for nine,
        plus a "Show more" whose only job was to undo a limit we imposed
        ourselves); this one hands the long tail to a page built for it. So the
        contract is: a cap, and a LINK — never a second state of this list. The
        two-state machinery stays retired in CSS and in JS."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        css = web_client.get("/static/css/rail.css").text
        body = css.split('html[data-ui-layout="rail"] .rail-history-body {', 1)[1].split("}", 1)[0]
        assert "flex: 1 1 0" in body
        assert "overflow-y: auto" in body
        # min-height:0 (and no floor) so a short viewport shrinks the list rather
        # than pushing the bottom zone past the fold — `.rail` cannot scroll.
        assert "min-height: 0" in body
        # No collapsed/expanded pair, and no in-place expander.
        assert ".rail-history.is-expanded" not in css
        assert "rail-history-more" not in css
        js = web_client.get("/static/js/rail_history.js").text
        assert "recentsExpanded" not in js
        # The constructor CALL, not the bare word: the comment that records why
        # the observer was removed names it, and asserting on the name alone
        # would forbid explaining the removal.
        assert "new MutationObserver" not in js
        assert "applyTruncation()" not in js
        # The cap itself, and its destination.
        assert "slice(0, RAIL_RECENT_LIMIT)" in js
        assert 'html[data-ui-layout="rail"] .rail-history-all {' in css


class TestRailAdminSubitems:
    """Admin's areas are SUBITEMS with side flyouts, never an inline tree.

    The old version nested a `<details>` per area that expanded in the column,
    so opening one added its links to the rail's height — and since the areas
    restored their own open state, the height (and therefore where every row
    below sat) differed from page to page. The flyout is absolutely positioned,
    so `Admin` open now costs a fixed seven rows and the two zones never drift.
    """

    def _rail(self, web_client, admin_cookie, path="/stack"):
        text = web_client.get(path, cookies=admin_cookie).text
        return text.split('<nav class="rail"', 1)[1].split("</nav>", 1)[0]

    def test_areas_are_subitem_rows_with_flyouts(self, web_client, admin_cookie, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        rail = self._rail(web_client, admin_cookie)
        # Seven areas, each a subitem row + its own flyout.
        assert rail.count('class="rail-admin-sub"') == 7
        assert rail.count("rail-admin-sub-row") == 7
        assert rail.count('class="rail-admin-flyout"') == 7
        # Every link the header dropdown carries is still reachable, now inside
        # a flyout rather than inline in the column.
        for href in (
            "/admin/adoption",
            "/admin/activity",
            "/admin/telemetry",
            "/admin/sessions",
            "/admin/chat",
            "/admin/users",
            "/admin/groups",
            "/admin/access",
            "/admin/tokens",
            "/admin/tables",
            "/admin/sync",
            "/admin/data-sources",
            "/admin/mcp-sources",
            "/admin/datasource-credentials",
            "/admin/marketplaces",
            "/admin/initial-workspace",
            "/admin/news",
            "/admin/corporate-memory",
            "/admin/knowledge-digests",
            "/admin/store/submissions",
            "/admin/prompts",
            "/documentation/api",
            "/docs",
            "/redoc",
            "/admin/server-config",
            "/admin/database",
        ):
            assert f'href="{href}"' in rail, f"admin flyouts dropped {href}"

    def test_area_row_is_a_button_not_a_nested_details(self, web_client, admin_cookie, monkeypatch):
        """A closed `<details>` hides its content via `::details-content
        { content-visibility: hidden }` in Chrome, which an author `display`
        rule cannot override — a hover-revealed panel inside one never appears.
        The area row must stay a plain <button>."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        rail = self._rail(web_client, admin_cookie)
        assert '<button type="button" class="rail-admin-sub-row' in rail
        assert '<details class="rail-admin-sub"' not in rail
        assert '<summary class="rail-admin-sub-row' not in rail
        # The topnav dropdown-panel classes are gone from the rail — the rail
        # styles its own rows now instead of resetting a leaked skin.
        assert "app-nav-menu-group" not in rail
        assert "app-nav-menu-section" not in rail
        assert "app-nav-menu-item" not in rail

    def test_flyout_is_positioned_not_inline(self, web_client, admin_cookie, monkeypatch):
        """The whole point: an area's links cost the column no height, and are
        revealed by hover AND focus (the latter covers click + keyboard)."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        css = web_client.get("/static/css/rail.css").text
        block = css.split('html[data-ui-layout="rail"] .rail-admin-flyout {', 1)[1].split("}", 1)[0]
        assert "position: absolute" in block
        assert "display: none" in block
        assert "left: 100%" in block  # beside the rail, in the page's left band
        assert "bottom: -6px" in block  # grows upward — Admin sits at the foot
        reveal = 'html[data-ui-layout="rail"] .rail-admin-sub:hover > .rail-admin-flyout'
        assert reveal in css
        assert 'html[data-ui-layout="rail"] .rail-admin-sub:focus-within > .rail-admin-flyout' in css

    def test_active_admin_page_marks_the_link_and_traces_its_area(self, web_client, admin_cookie, monkeypatch):
        """On an admin page: the LINK takes the primary tint (`is-active`), and
        its area row only gets the quiet `has-active` trace — the active
        destination stays the one tinted row."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        resp = web_client.get("/admin/tables", cookies=admin_cookie)
        assert resp.status_code == 200
        rail = resp.text.split('<nav class="rail"', 1)[1].split("</nav>", 1)[0]
        # Admin auto-opens on its own pages.
        assert '<details class="rail-admin" open>' in rail
        # The Tables link is the active destination (class attr precedes href).
        tables_link = rail.split('href="/admin/tables"', 1)[0].rsplit("<a ", 1)[1]
        assert "rail-admin-flyout-item is-active" in tables_link
        # Exactly one area row carries the trace (Data Packages).
        assert rail.count("has-active") == 1
        dp = rail.split("Data Packages", 1)[0].rsplit('class="rail-admin-sub-row', 1)[1]
        assert "has-active" in dp
        # ...and area rows never take the `.on` destination tint.
        for row in rail.split('class="rail-admin-sub-row')[1:]:
            assert ' on"' not in row.split(">", 1)[0]


class TestDashboardLandingRedirect:
    """Layout-aware /dashboard split. Topnav instances must be byte-for-byte
    unchanged — the legacy table-inventory dashboard.html still renders
    there. Under the rail, the Dashboard IS Chat's pre-conversation state
    (chat.html's rail empty state, see TestRailDashboard), so /dashboard
    302s to /chat for chat-granted users; grant-less users keep the 302 to
    My Stack (the page exists to start Agnes conversations, so without a
    grant it would be a dead shell)."""

    def test_topnav_dashboard_still_renders(self, web_client, admin_cookie, monkeypatch):
        monkeypatch.delenv("AGNES_UI_LAYOUT", raising=False)
        resp = web_client.get("/dashboard", cookies=admin_cookie, follow_redirects=False)
        assert resp.status_code == 200
        assert 'data-ui-layout="topnav"' in resp.text
        # The rail dashboard's markup/assets must never leak into topnav.
        assert 'class="rdb"' not in resp.text
        assert "chat_dashboard" not in resp.text

    def test_rail_dashboard_redirects_to_chat_with_grant(self, web_client, admin_cookie, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        import app.auth.access as access

        monkeypatch.setattr(access, "has_explicit_grant", lambda *a, **k: True)
        web_client.app.state.chat_config = SimpleNamespace(enabled=True)
        resp = web_client.get("/dashboard", cookies=admin_cookie, follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/chat"

    def test_rail_dashboard_redirects_to_library_without_chat_grant(self, web_client, admin_cookie, monkeypatch):
        """The grant-less landing is the Library, not My Stack: /stack is no
        longer a rail destination (#1088), so landing there would strand the
        caller on a page the rail neither links to nor highlights."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        # Chat is disabled by default in tests, so can_chat is False.
        resp = web_client.get("/dashboard", cookies=admin_cookie, follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/library"

    def test_ask_is_retired(self, web_client, admin_cookie, monkeypatch):
        """The /ask hero is retired — it 302s to / rather than rendering."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        resp = web_client.get("/ask", cookies=admin_cookie, follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/"


class TestRailDashboard:
    """The rail Dashboard = Chat's pre-conversation state: /chat with no
    active conversation renders the Agnes-centric dashboard (greeting, the
    REAL composer, activity panels, guided task starters) and hides it the
    moment a conversation starts. One composer, one conversation flow —
    there is no separate dashboard page or second chat input."""

    def _enable_chat(self, web_client, monkeypatch):
        """Make can_chat true — same recipe as TestRailChatHistory."""
        import app.auth.access as access

        monkeypatch.setattr(access, "has_explicit_grant", lambda *a, **k: True)
        web_client.app.state.chat_config = SimpleNamespace(enabled=True)

    def test_rail_chat_renders_dashboard_empty_state(self, web_client, admin_cookie, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        self._enable_chat(web_client, monkeypatch)
        resp = web_client.get("/chat", cookies=admin_cookie, follow_redirects=False)
        assert resp.status_code == 200
        text = resp.text
        assert 'data-ui-layout="rail"' in text
        for anchor in (
            # The Knowledge Layer hero — one premium banner: text lead + green
            # CTA on the left, orb in the centre, floating integration chips on
            # the right (no inner "Ask Agnes" / tools cards).
            'class="klb"',  # self-contained hero banner
            '<div class="klb-lead">',  # left text lead block
            "Agnes is your knowledge layer.",  # hero headline line 1
            "Use it here or connect your tools.",  # hero headline line 2 (gradient)
            '<p class="klb-sub">',  # the supporting sentence renders
            'href="/how-it-works#connect"',  # the green "Connect your tools" CTA target
            'class="klb-chips"',  # floating integration chips (no card)
            "Claude Code",  # a chip label
            "CLI and more",  # a chip label
            # Below the banner: the trust caption + "Ask Agnes anything" heading.
            "klb-hub-label--lead",  # the trust-caption wrapper
            "Secure. Private. Always in sync.",  # caption text, below the banner
            'class="rdb-ask-heading"',  # the usage heading above the composer
            "Ask Agnes anything",
            'id="rdb-actions"',  # the one personalized section
            'id="rdb-actions-list"',  # suggested-actions list
            "css/chat_dashboard.css",  # dashboard styles
            'id="chat-input"',  # the REAL composer serves the dashboard
        ):
            assert anchor in text, f"rail chat dashboard is missing {anchor}"
        # The retired three-panel layout is gone (one actions list instead).
        # `rdb-semantic-links` joins it: the dashboard carried a "Browse metrics
        # & glossary" button (#1108) directly above the composer, and this is
        # the moment of INTENT — the reader came here to ask something. A
        # control whose only function is to navigate away from the composer,
        # offered before they have an answer to check, is a detour.
        #
        # Asserted on the BUTTON'S wrapper class, not on the bare
        # `/catalog/semantics` URL: the rail chrome now carries a Definitions
        # nav row, so that URL is legitimately present on every rail page
        # including this one. What must not come back is the in-page button.
        for retired in (
            'id="rdb-continue-list"',
            'id="rdb-tasks"',
            "Recent updates",
            "rdb-semantic-links",
        ):
            assert retired not in text, f"retired dashboard panel leaked back: {retired}"
        # The banner's old two-button CTA row is retired: "Connect your tools"
        # moved into the tools card (klb-card-cta), "Learn how it works" into
        # the user menu. Neither the CTA row nor the outline secondary remain.
        assert 'class="klb-ctas"' not in text
        assert "klb-cta-secondary" not in text
        # One composer only — the retired standalone dashboard's look-alike
        # input and its prompt-handoff module must be gone.
        assert 'id="rdb-composer"' not in text
        assert "dashboard_rail" not in text
        # The retired ask-hero brand block is gone too.
        assert "Ask anything." not in text
        # The "Agnes Knowledge Layer" hub title was dropped as redundant with the
        # "Agnes is your knowledge layer." headline — it must not render anywhere.
        assert "Agnes Knowledge Layer" not in text

    def test_rail_dashboard_actions_section(self, web_client, admin_cookie, monkeypatch):
        """One Suggested-next-actions section below the composer: list +
        loading + empty-state elements are all server-rendered (js toggles
        them), and there are no department/role tabs."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        self._enable_chat(web_client, monkeypatch)
        resp = web_client.get("/chat", cookies=admin_cookie)
        assert resp.status_code == 200
        text = resp.text
        assert 'id="rdb-actions-loading"' in text
        assert 'id="rdb-actions-empty"' in text
        assert "No suggested actions yet" in text
        # js/chat_dashboard.js drives the list through chat.js's one flow.
        assert "js/chat_dashboard.js" not in text  # loaded via chat.js import, not a script tag

    def test_topnav_chat_keeps_classic_empty_state(self, web_client, admin_cookie, monkeypatch):
        """The dashboard empty state is rail-only — topnav /chat keeps the
        classic capability cards, byte-for-byte."""
        monkeypatch.delenv("AGNES_UI_LAYOUT", raising=False)
        self._enable_chat(web_client, monkeypatch)
        resp = web_client.get("/chat", cookies=admin_cookie)
        assert resp.status_code == 200
        assert "What can I help you with?" in resp.text
        assert 'id="rdb-tasks"' not in resp.text
        assert "chat_dashboard" not in resp.text

    def test_rail_nav_new_chat_is_the_single_chat_entry(self, web_client, admin_cookie, monkeypatch):
        """There is no separate Dashboard nav item — /dashboard is just Chat's
        pre-conversation state, so it and New chat pointed at the same surface.
        New chat is the single chat entry point; the only /dashboard href left
        is the rail logo (href = home_route, default /dashboard)."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        self._enable_chat(web_client, monkeypatch)
        resp = web_client.get("/stack", cookies=admin_cookie)
        assert resp.status_code == 200
        text = resp.text
        assert 'id="new-chat"' in text
        assert "New chat" in text
        # The retired Dashboard nav item is gone: the only /dashboard href is
        # the logo (even with a chat grant), never a second nav-item occurrence.
        assert text.count('href="/dashboard"') == 1
        assert 'class="rail-logo" href="/dashboard"' in text

    def test_rail_nav_new_chat_active_on_empty_chat(self, web_client, admin_cookie, monkeypatch):
        """New chat carries the `.on` active state (folded over from the retired
        Dashboard item) exactly while the pre-conversation state is showing —
        /chat with no session deep link."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        self._enable_chat(web_client, monkeypatch)
        # Empty /chat → New chat is active.
        resp = web_client.get("/chat", cookies=admin_cookie)
        assert resp.status_code == 200
        assert re.search(r'class="rail-i[^"]*\bon\b[^"]*"\s+id="new-chat"', resp.text)
        # Deep-linked into a conversation → New chat is not active, and carries
        # no standing tint of its own either (test_new_chat_is_an_ordinary_row),
        # so the row is genuinely plain.
        resp = web_client.get("/chat?session=abc", cookies=admin_cookie)
        assert resp.status_code == 200
        assert not re.search(r'class="rail-i[^"]*\bon\b[^"]*"\s+id="new-chat"', resp.text)

    def test_rail_nav_hides_new_chat_without_chat_grant(self, web_client, admin_cookie, monkeypatch):
        """Without a chat grant the chat slot renders nothing; the only
        /dashboard href left is the logo (whose route bounces grant-less
        callers to /stack)."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        resp = web_client.get("/stack", cookies=admin_cookie)
        assert resp.status_code == 200
        assert 'id="new-chat"' not in resp.text
        assert resp.text.count('href="/dashboard"') == 1

    def test_topnav_nav_untouched(self, web_client, admin_cookie, monkeypatch):
        """The topnav chrome gains no Dashboard-first IA — its header link
        row is unchanged."""
        monkeypatch.delenv("AGNES_UI_LAYOUT", raising=False)
        resp = web_client.get("/dashboard", cookies=admin_cookie)
        assert resp.status_code == 200
        assert 'class="app-header"' in resp.text
        assert 'class="rail"' not in resp.text


class TestProfileNotifications:
    """The Notifications channels moved off the retired /dashboard onto the
    account page (/me/profile), where they belong. Rendered on both layouts."""

    def test_profile_renders_notifications_section(self, web_client, admin_cookie, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        resp = web_client.get("/me/profile", cookies=admin_cookie)
        assert resp.status_code == 200
        assert "Notifications" in resp.text
        assert 'class="pf-notif-list"' in resp.text
        # Telegram link affordance is present (unlinked state → Link button).
        assert "showTelegramVerify()" in resp.text


class TestStackWorkspace:
    """My Stack is the persistent context the Main Agent uses. Every resource
    shown is already in the stack, so the page never repeats "In stack" or
    exposes download states; instead it groups resources into Required
    (admin-granted, locked) and Added by you (optional, removable). Uploads
    moved off to /library. Growing the stack happens on /catalog."""

    def test_stack_has_no_status_strip_below_table(self, web_client, admin_cookie, monkeypatch):
        """The workspace stat strip that used to sit below the inventory has
        been removed — the page ends at the groups."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        resp = web_client.get("/stack", cookies=admin_cookie)
        assert resp.status_code == 200
        assert 'class="stk-stats"' not in resp.text
        assert 'class="stk-stat__label"' not in resp.text

    def test_stack_groups_required_and_added(self, web_client, admin_cookie, monkeypatch):
        """The inventory is ONE table (shared .data-table primitive, same as
        Artefacts) with column headers and two collapsible <tbody> groups —
        Required, then Added by you — a dominant search field, and a small
        secondary sort control. No Added/Status columns, no download wording."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        resp = web_client.get("/stack", cookies=admin_cookie)
        assert resp.status_code == 200
        text = resp.text
        # One data-table with headers + the two collapsible group bodies.
        assert 'class="data-table stk-table"' in text
        assert 'id="stk-required-body"' in text
        assert 'id="stk-added-body"' in text
        assert 'data-stk-collapse="required"' in text
        assert 'data-stk-collapse="added"' in text
        assert ">Required</span>" in text
        assert ">Added by you</span>" in text
        # Column headers present; retired Added/Status columns are gone.
        for col in ("Name", "Type", "Details", "Source", "Actions"):
            assert "<th" in text and col in text
        assert ">Added<" not in text
        assert ">Status<" not in text
        # Toolbar is the shared .fbar filter-bar component (same as Artefacts):
        # search + a Filter dropdown (Type facet) + sort (default Name, never
        # "Recently added"). Origin (Required/Added) stays the group split, not
        # a toolbar control.
        assert 'class="fbar"' in text
        assert 'id="stk-search"' in text
        assert 'id="stk-sort"' in text
        assert 'id="stk-filter-btn"' in text
        assert '<option value="name" selected>' in text
        assert "Recently added" not in text
        # No download/technical states leak in.
        assert "Downloaded" not in text
        assert "In stack" not in text
        assert 'data-toggle-kind="download"' not in text
        # No card grid — recommendations moved to /catalog.
        assert 'class="uc-grid"' not in text
        assert "Recommended for you" not in text
        assert "stk-recs" not in text

    def test_required_grant_lands_in_required_group_with_badge(self, web_client, admin_cookie, monkeypatch):
        """A required-tier grant clusters in the Required group, rendered into
        the required tbody with the subtle Required badge and NO overflow
        (remove) affordance — required resources cannot be removed."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        import uuid

        from src.db import get_system_db
        from src.repositories.data_packages import DataPackagesRepository

        conn = get_system_db()
        pkg_id = DataPackagesRepository(conn).create(
            name="Mandatory Revenue Pkg",
            slug="mandatory-revenue",
            description="Locked finance data",
            icon=None,
            color=None,
            created_by="test",
        )
        admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = 'Admin'").fetchone()[0]
        conn.execute(
            "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
            "requirement, assigned_at, assigned_by) "
            "VALUES (?, ?, 'data_package', ?, 'required', CURRENT_TIMESTAMP, 'test')",
            [str(uuid.uuid4()), admin_gid, pkg_id],
        )
        conn.close()

        resp = web_client.get("/stack", cookies=admin_cookie)
        assert resp.status_code == 200
        text = resp.text
        # The required package renders in the required tbody, ahead of the
        # Added-by-you tbody, with the subtle Required badge.
        req_body = text.split('id="stk-required-body"', 1)[1].split('id="stk-added-body"', 1)[0]
        assert "Mandatory Revenue Pkg" in req_body
        assert 'class="stk-req"' in req_body
        # ...and carries no remove/overflow affordance.
        assert "More actions for Mandatory Revenue Pkg" not in text
        assert "Remove from My Stack" not in req_body


class TestCatalogRecommendations:
    """Catalog reshape: the Catalog surfaces ONLY resources the caller does
    not already have. Auto-membership puts every granted package in the
    caller's stack the moment it's granted, so a granted package appears
    ONLY on My Stack — never on /catalog (not in the addable grids, and not
    in the "Recommended for you" row, which stays empty for granted
    content). The download-a-local-copy action for a granted-but-not-yet-
    materialized package lives on My Stack, not here."""

    def test_granted_package_absent_from_catalog_present_on_my_stack(self, web_client, admin_cookie, monkeypatch):
        """A granted-but-not-yet-downloaded package must not appear anywhere
        on /catalog. It lives on My Stack. Materializing (subscribing) it
        must not pull it back into the Catalog — it still shows only on My
        Stack."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        import uuid

        from src.db import get_system_db
        from src.repositories.data_packages import DataPackagesRepository

        conn = get_system_db()
        pkg_id = DataPackagesRepository(conn).create(
            name="Unstacked Package XYZ",
            slug="unstacked-xyz",
            description="d",
            icon=None,
            color=None,
            created_by="test",
        )
        admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = 'Admin'").fetchone()[0]
        conn.execute(
            "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
            "requirement, assigned_at, assigned_by) "
            "VALUES (?, ?, 'data_package', ?, 'available', CURRENT_TIMESTAMP, 'test')",
            [str(uuid.uuid4()), admin_gid, pkg_id],
        )
        conn.close()

        # Granted → auto-membership in_stack=True → absent from the entire
        # Catalog page (Recommended row + addable grids alike).
        resp = web_client.get("/catalog", cookies=admin_cookie)
        assert resp.status_code == 200
        assert "Unstacked Package XYZ" not in resp.text, "granted package must not appear anywhere on the Catalog"

        # ...but it IS on My Stack, where the caller's holdings live.
        resp = web_client.get("/stack", cookies=admin_cookie)
        assert "Unstacked Package XYZ" in resp.text

        # Materializing (subscribing) it must not pull it back into the Catalog.
        from src.repositories.user_stack_subscriptions import UserStackSubscriptionsRepository

        conn = get_system_db()
        UserStackSubscriptionsRepository(conn).subscribe("admin1", "data_package", pkg_id)
        conn.close()

        resp = web_client.get("/catalog", cookies=admin_cookie)
        assert "Unstacked Package XYZ" not in resp.text
        resp = web_client.get("/stack", cookies=admin_cookie)
        assert "Unstacked Package XYZ" in resp.text


class TestPaperThemeAssets:
    """The paper value must resolve to real CSS, not a silent no-op."""

    def test_design_tokens_define_paper_block(self):
        css = open("app/web/static/css/design-tokens.css").read()
        assert ':root[data-theme="paper"]' in css

    def test_paper_block_covers_core_ds_tokens(self):
        css = open("app/web/static/css/design-tokens.css").read()
        block = re.search(r':root\[data-theme="paper"\]\s*\{(.*?)\n\}', css, re.DOTALL)
        assert block, "paper block missing"
        body = block.group(1)
        for token in (
            "--ds-primary:",
            "--ds-bg:",
            "--ds-surface:",
            "--ds-border:",
            "--ds-text-primary:",
            "--primary:",  # legacy compat shim
            "--background:",  # legacy compat shim
        ):
            assert token in body, f"paper theme must override {token}"

    def test_bases_load_rail_and_paper_sheets(self):
        for base in ("app/web/templates/base_ds.html", "app/web/templates/base.html"):
            html = open(base).read()
            assert "css/rail.css" in html, f"{base} must load rail.css"
            assert "css/paper-skin.css" in html, f"{base} must load paper-skin.css"
            assert "css/detail-page.css" in html, f"{base} must load detail-page.css"

    def test_detail_page_sheet_loads_before_head_extra(self):
        """The detail pages emit `detail.styles()` from `head_extra`, so the
        shared sheet must be linked ABOVE that block — otherwise every rule
        it overrides would lose the cascade to a per-page <style>."""
        for base in ("app/web/templates/base_ds.html", "app/web/templates/base.html"):
            html = open(base).read()
            assert html.index("css/detail-page.css") < html.index("{% block head_extra %}"), (
                f"{base} must link detail-page.css before the head_extra block"
            )

    @staticmethod
    def _selectors(path: str) -> list[str]:
        """Rule selectors from a flat CSS sheet — comments stripped,
        at-rules (@media/@supports wrappers) skipped; rules nested in
        at-rule bodies still surface as ordinary selectors.

        `@keyframes` bodies are dropped whole: their `from` / `to` / `50%`
        stops are parsed as selectors by the scan below, and a keyframe stop
        can no more carry a theme scope than it can carry a class."""
        css = re.sub(r"/\*.*?\*/", "", open(path).read(), flags=re.DOTALL)
        css = re.sub(r"@(?:-\w+-)?keyframes\s+[\w-]+\s*\{(?:[^{}]|\{[^{}]*\})*\}", "", css)
        raw = re.findall(r"(?:^|[{}])\s*([^{}]+?)\s*\{", css)
        return [s.strip() for s in raw if s.strip() and not s.strip().startswith("@")]

    def test_rail_css_rules_are_scoped_to_activation(self):
        """Every rule in rail.css must be scoped to the rail layout
        attribute so the sheet is inert under topnav."""
        for sel in self._selectors("app/web/static/css/rail.css"):
            assert 'html[data-ui-layout="rail"]' in sel, f"rail.css selector not scoped to rail layout: {sel!r}"

    def test_paper_skin_rules_are_scoped_to_theme(self):
        for sel in self._selectors("app/web/static/css/paper-skin.css"):
            assert '[data-theme="paper"]' in sel, f"paper-skin.css selector not scoped to paper theme: {sel!r}"

    def test_detail_page_rules_are_scoped_to_theme(self):
        """The shared resource-detail layout is opt-in like every other
        redesign sheet: default blue/topnav instances load it inert."""
        for sel in self._selectors("app/web/static/css/detail-page.css"):
            assert '[data-theme="paper"]' in sel, f"detail-page.css selector not scoped to paper theme: {sel!r}"

    def test_trustmark_css_rules_are_scoped_to_theme(self):
        """The trust markers are opt-in like every other redesign sheet.

        This started life as its mirror image — a test asserting the sheet was
        deliberately GLOBAL, on the reading that the markers were a documented
        default-look change. That was wrong: the CHANGELOG documents the two
        FLAGS defaulting on (markers appearing where a flag had hidden them),
        not the theme scoping, which was never a decision. A default blue
        instance must render its own spelling — `.cc-trust` on catalog cards,
        the amber `Curated` badge on package cards and detail heroes, nothing on
        Library rows — so `.ds-trust` has to stay inert there.

        Markup gating is the other half and cannot be seen from here: `mark()`
        in macros/_trustmark.html takes `paper=False`, so an ungated callsite
        emits nothing rather than an unstyled marker.
        """
        for sel in self._selectors("app/web/static/css/trustmark.css"):
            assert '[data-theme="paper"]' in sel, f"trustmark.css selector not scoped to paper theme: {sel!r}"

    def test_keyframe_stops_are_not_mistaken_for_selectors(self):
        """Guard on the guard: without the @keyframes strip, a stop like
        `from {` parses as an unscoped selector, which would fail the scoping
        test above for a reason that has nothing to do with scoping."""
        selectors = self._selectors("app/web/static/css/detail-page.css")
        assert selectors, "selector scan returned nothing — the regex broke"
        assert "from" not in selectors and "to" not in selectors


class TestSharedDetailLayout:
    """One editorial layout for every resource type, opt-in.

    Under the redesign a detail page is: a header on the page ground (no
    gradient slab, no nested frosted panel), a resource-type badge beside
    the title, and a two-column shell with a sticky right rail. Default
    instances must still get the legacy gradient hero + stacked cards, which
    is the half of this that is a regression guard rather than a feature."""

    @staticmethod
    def _package(slug: str = "detail-layout-pkg") -> str:
        from src.repositories import data_packages_repo

        return data_packages_repo().create(
            name="Detail Layout Package",
            slug=slug,
            description="A package used to assert the shared detail layout.",
            icon=None,
            color=None,
            created_by="admin1",
        )

    def test_redesign_renders_the_two_column_shell_and_type_badge(self, web_client, admin_cookie, monkeypatch):
        monkeypatch.setenv("AGNES_INSTANCE_THEME", "paper")
        self._package("paper-detail-pkg")
        text = web_client.get("/catalog/p/paper-detail-pkg", cookies=admin_cookie).text
        assert "detail-cols" in text, "the editorial layout must open the two-column shell"
        assert "detail-main" in text
        assert "detail-aside" in text, "the sticky right rail must render"
        # The resource-type badge names the type in words, next to the title.
        assert 'class="detail-type"' in text
        assert ">Data package<" in text
        # The rail answers "is this on my laptop?".
        assert "Availability" in text
        assert 'class="detail-side__rows"' in text

    def test_default_instance_renders_no_ds_trust_marker(self, web_client, admin_cookie, monkeypatch):
        """A default instance shows its OWN trust spelling, never `.ds-trust`.

        This assertion belongs next to the one below and its absence is exactly
        how the leak shipped: that test pins the absence of `detail-cols`,
        `detail-aside` and `detail-type`, so the trust pill sailed through the
        one test written to protect the default hero.
        """
        monkeypatch.delenv("AGNES_INSTANCE_THEME", raising=False)
        for path in ("/library", "/catalog"):
            resp = web_client.get(path, cookies=admin_cookie)
            assert resp.status_code == 200, path
            # Match the EMITTED markup, not the string: the page's own CSS
            # comments legitimately name the class while explaining where the
            # markers went.
            assert 'class="ds-trust' not in resp.text, (
                f"{path} leaked the paper-only trust marker into the default theme"
            )

    def test_default_instance_gets_neither_shell_nor_badge(self, web_client, admin_cookie, monkeypatch):
        """The whole layout is gated: a default instance renders the TRUE
        pre-redesign page (the frozen ``catalog_package_detail_legacy.html``
        served by ``_detail_template`` — see TestDetailPageParity), not the
        redesigned template's blue variant it briefly got."""
        monkeypatch.delenv("AGNES_INSTANCE_THEME", raising=False)
        monkeypatch.delenv("AGNES_UI_LAYOUT", raising=False)
        self._package("blue-detail-pkg")
        text = web_client.get("/catalog/p/blue-detail-pkg", cookies=admin_cookie).text
        assert "detail-cols" not in text, "the two-column shell must not reach default instances"
        assert "detail-aside" not in text
        assert 'class="detail-type"' not in text
        assert 'class="detail-side__rows"' not in text, "rail content must not append itself as extra sections"
        assert "detail-hero" not in text, "redesigned scaffold markup must not reach default instances"
        # …and the pre-redesign page's own anatomy still renders.
        assert 'class="pkg-hero"' in text
        assert 'class="back-link"' in text

    def test_overflow_menu_holds_the_secondary_action(self, web_client, admin_cookie, monkeypatch):
        """One prominent action per header; the admin errand moves into the
        overflow menu (a <details>, so it needs no JavaScript to open)."""
        monkeypatch.setenv("AGNES_INSTANCE_THEME", "paper")
        self._package("menu-detail-pkg")
        text = web_client.get("/catalog/p/menu-detail-pkg", cookies=admin_cookie).text
        assert '<details class="detail-menu">' in text
        assert 'class="detail-menu__item' in text
        assert "Edit package metadata" in text
        # The header's icon-button spelling of the same action is gone, so the
        # action is not offered twice.
        assert 'class="detail-edit-icon"' not in text


class TestResourceColourTokens:
    """One semantic accent per resource type, consumed product-wide through
    the `--ds-kind-*` aliases rather than by any page directly."""

    RESOURCES = ("data", "skill", "plugin", "file", "collection", "agent", "memory", "recipe")

    @staticmethod
    def _tokens_css() -> str:
        return open("app/web/static/css/design-tokens.css").read()

    def test_every_resource_has_the_full_three_role_set(self):
        css = self._tokens_css()
        for resource in self.RESOURCES:
            for role in ("ink", "soft", "line"):
                token = f"--ds-resource-{resource}-{role}:"
                assert token in css, f"resource colour system is missing {token}"

    def test_dark_theme_flips_both_halves_of_every_pair(self):
        """A resource tint is a near-white fill in light mode. Under dark it
        has to be re-derived, or the accent ink lands on a light fill while
        the page around it went dark."""
        css = self._tokens_css()
        # More than one dark block exists (the brand-variant one included), so
        # scan them all rather than assuming which of them declares these.
        joined = "\n".join(
            m.group(1) for m in re.finditer(r':root\[data-theme="dark"\][^{]*\{(.*?)\n\}', css, re.DOTALL)
        )
        assert joined, "no dark theme block found"
        for resource in self.RESOURCES:
            assert f"--ds-resource-{resource}-ink:" in joined, f"{resource} ink not re-derived under dark"
            assert f"--ds-resource-{resource}-soft:" in joined, f"{resource} tint not re-derived under dark"

    def test_paper_routes_kind_aliases_onto_the_resource_family(self):
        """The remap is the whole distribution mechanism: every existing
        consumer reads `--ds-kind-*`, so pointing those at `--ds-resource-*`
        under paper is what carries the palette to the Library table, the
        cards, the detail pages, search results and the Stack at once."""
        css = self._tokens_css()
        block = re.search(r':root\[data-theme="paper"\]\s*\{(.*?)\n\}', css, re.DOTALL)
        assert block, "paper block missing"
        body = block.group(1)
        for kind, resource in (
            ("data", "data"),
            ("skill", "skill"),
            ("plugin", "plugin"),
            ("file", "file"),
            ("library", "collection"),  # `library` is the scaffold's name for a collection
            ("agent", "agent"),
            ("memory", "memory"),
            ("recipe", "recipe"),
        ):
            assert f"--ds-kind-{kind}: var(--ds-resource-{resource}-ink);" in body, (
                f"paper must alias --ds-kind-{kind} onto the {resource} resource colour"
            )

    def test_default_theme_keeps_its_original_kind_hues(self):
        """The palette is opt-in. A default (blue) instance must still resolve
        the pre-redesign hues, so the remap may only live in the paper block."""
        css = self._tokens_css()
        # The unscoped `:root` is split across several append-only blocks.
        globals_ = "\n".join(m.group(1) for m in re.finditer(r"^:root\s*\{(.*?)\n\}", css, re.DOTALL | re.MULTILINE))
        assert "--ds-kind-data: #185a57;" in globals_, "default data hue changed"
        assert "--ds-kind-plugin: #391c57;" in globals_, "default plugin hue changed"
        assert "--ds-kind-library: #0a5aa8;" in globals_, "default collection hue changed"
        assert "--ds-kind-skill: #0e7c57;" in globals_, "default skill hue changed"
        assert "--ds-kind-memory: #523410;" in globals_, "default memory hue changed"


class TestDetailPageTemplateIsShared:
    """Every resource detail page is the SAME template, not nine lookalikes.

    The point of `macros/_detail.html` is that a reader meets one page shape
    on a data package, a plugin, a skill, an agent, a file, a collection and a
    memory domain — same header, same container language, same rail, same
    place for every shared concept. Two things drift without a guard:

      1. a page keeps hand-writing its own header (the marketplace pages did
         exactly this, for the sake of four hydration hooks, and lost the
         type badge, the rail and the overflow menu in the process), and
      2. a page opts out of the panels container language, so it renders
         borderless sections beside another page's panels.

    The blue half is the regression guard: none of it may reach a default
    instance, whose page is a contract.
    """

    # (path-builder key, the type badge the header must print)
    PAPER_PAGES = (
        ("/catalog/p/{pkg}", "Data package"),
        ("/marketplace/curated/agnes-builtin/agnes-analyst", "Plugin"),
    )

    @staticmethod
    def _package(slug: str) -> str:
        from src.repositories import data_packages_repo

        return data_packages_repo().create(
            name="Shared Template Package",
            slug=slug,
            description="A package used to assert the shared detail template.",
            icon=None,
            color=None,
            created_by="admin1",
        )

    def test_every_detail_page_speaks_the_panels_language(self, web_client, admin_cookie, monkeypatch):
        """`detail--panels` is the scaffold's shared default, not a per-page
        opt-in — a default each page has to remember to ask for is a default
        that drifts."""
        monkeypatch.setenv("AGNES_INSTANCE_THEME", "paper")
        self._package("panels-detail-pkg")
        for path in ("/catalog/p/panels-detail-pkg", "/marketplace/curated/agnes-builtin/agnes-analyst"):
            resp = web_client.get(path, cookies=admin_cookie)
            assert resp.status_code == 200, path
            assert "detail--panels" in resp.text, f"{path} is not on the shared container language"
            assert "detail-cols" in resp.text, f"{path} is missing the two-column shell"
            assert "detail-aside" in resp.text, f"{path} is missing the rail"

    def test_the_marketplace_pages_render_through_the_shared_hero(self, web_client, admin_cookie, monkeypatch):
        """They used to hand-write the header. The tell is the type badge and
        the overflow menu, which only the shared hero emits."""
        monkeypatch.setenv("AGNES_INSTANCE_THEME", "paper")
        text = web_client.get("/marketplace/curated/agnes-builtin/agnes-analyst", cookies=admin_cookie).text
        assert 'class="detail-type"' in text, "the plugin header must name the resource type"
        assert ">Plugin<" in text
        assert '<details class="detail-menu">' in text, "secondary actions belong in the overflow menu"
        # The hydration hooks survived the move onto the shared hero.
        assert 'id="hero-name"' in text
        assert 'id="hero-icon"' in text
        assert 'id="details-list"' in text

    def test_the_marketplace_pages_keep_the_legacy_page_on_a_default_instance(
        self, web_client, admin_cookie, monkeypatch
    ):
        """The whole redesign is gated. A default instance renders the TRUE
        pre-redesign page (the frozen ``*_legacy.html`` copy served by
        ``_detail_template`` — see TestDetailPageParity), not the redesigned
        template's blue variant it briefly got: none of the shared-scaffold
        anatomy, and none of the restyled hero-panel markup either."""
        monkeypatch.delenv("AGNES_INSTANCE_THEME", raising=False)
        monkeypatch.delenv("AGNES_UI_LAYOUT", raising=False)
        text = web_client.get("/marketplace/curated/agnes-builtin/agnes-analyst", cookies=admin_cookie).text
        assert "detail--panels" not in text
        assert "detail-cols" not in text, "the two-column shell must not reach default instances"
        assert "detail-aside" not in text
        assert 'class="detail-type"' not in text
        assert "detail-hero--paneled" not in text, "restyled hero markup must not reach default instances"
        # The pre-redesign page's own anatomy — including the async hydration
        # hooks, which predate the redesign — still renders.
        assert 'id="lead-text"' in text
        assert 'id="details-list"' in text
        assert 'id="hero-name"' in text

    def test_shared_concepts_use_one_component_each(self):
        """Sharing, versions, the admin ladder and 'what is inside this' are
        defined once in the scaffold. A page that re-specifies one of them
        locally is how two surfaces come to disagree about the same fact."""
        scaffold = open("app/web/templates/macros/_detail.html").read()
        for macro in (
            "macro visibility_chip(",
            "macro side_sharing(",
            "macro version_timeline(",
            "macro store_menu(",
            "macro objects(",
        ):
            assert macro in scaffold, f"the shared scaffold is missing `{macro}`"

        # The store-entity action ladder existed twice, byte-similar, on the
        # plugin and the skill/agent pages. Neither may rebuild it.
        for page in (
            "app/web/templates/marketplace_plugin_detail.html",
            "app/web/templates/marketplace_item_detail.html",
        ):
            assert "detail.store_menu(" in open(page).read(), (
                f"{page} must reach the Edit/Archive/Hard-delete ladder through the shared macro"
            )


class TestDefaultContentParity:
    """Topnav keeps the pre-redesign PAGES, not just the chrome.

    The catalog already does this (classic ``catalog.html`` on topnav,
    ``catalog_unified.html`` under rail); these tests extend the same
    contract to the other surfaces the redesign rewrote in place, so a
    default instance's upgrade changes nothing it renders:

    - ``/library``: the legacy "Your collections" page vs the unified Library
    - ``/marketplace``: the two-shelf Curated/Flea page vs one Browse shelf
    - ``/chat``: no composer "+" upload menu, no journey checklist, no
      conversation row menu, no auto-launched tour outside the rail layout
    """

    def test_topnav_library_is_the_legacy_collections_page(self, web_client, admin_cookie, monkeypatch):
        monkeypatch.delenv("AGNES_UI_LAYOUT", raising=False)
        monkeypatch.delenv("AGNES_INSTANCE_THEME", raising=False)
        resp = web_client.get("/library", cookies=admin_cookie)
        assert resp.status_code == 200
        assert "Your collections" in resp.text, "topnav /library must stay the legacy collections page"
        assert 'id="lib-search"' not in resp.text, "unified Library toolbar leaked into topnav"

    def test_rail_library_is_the_unified_library(self, web_client, admin_cookie, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        resp = web_client.get("/library", cookies=admin_cookie)
        assert resp.status_code == 200
        assert 'id="lib-search"' in resp.text
        assert "Your collections" not in resp.text

    def test_topnav_marketplace_keeps_the_curated_and_flea_shelves(self, web_client, admin_cookie, monkeypatch):
        monkeypatch.delenv("AGNES_UI_LAYOUT", raising=False)
        monkeypatch.delenv("AGNES_INSTANCE_THEME", raising=False)
        resp = web_client.get("/marketplace", cookies=admin_cookie)
        assert resp.status_code == 200
        assert 'data-tab="flea"' in resp.text, "topnav /marketplace must keep the Curated/Flea tab split"
        assert "data-count-browse" not in resp.text, "unified Browse shelf leaked into topnav"

    def test_rail_marketplace_is_one_browse_shelf(self, web_client, admin_cookie, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        resp = web_client.get("/marketplace", cookies=admin_cookie)
        assert resp.status_code == 200
        assert "data-count-browse" in resp.text
        assert 'data-tab="flea"' not in resp.text

    def _chat(self, web_client, admin_cookie):
        """GET /chat with chat enabled AND explicitly granted to the Admin
        group — ``can_chat`` (the rail card's gate) deliberately reads the
        explicit grant, not god-mode."""
        import uuid

        from src.db import get_system_db

        web_client.app.state.chat_config = SimpleNamespace(enabled=True)
        conn = get_system_db()
        try:
            gid = conn.execute("SELECT id FROM user_groups WHERE name = 'Admin'").fetchone()[0]
            already = conn.execute(
                "SELECT 1 FROM resource_grants WHERE group_id = ? AND resource_type = 'chat'", [gid]
            ).fetchone()
            if not already:
                conn.execute(
                    "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
                    "requirement, assigned_at, assigned_by) "
                    "VALUES (?, ?, 'chat', 'chat', 'available', CURRENT_TIMESTAMP, 'test')",
                    [str(uuid.uuid4()), gid],
                )
        finally:
            conn.close()
        return web_client.get("/chat", cookies=admin_cookie)

    def test_topnav_chat_has_no_upload_menu_journey_or_row_menu(self, web_client, admin_cookie, monkeypatch):
        """The redesign's chat additions are rail-only. A topnav instance's
        composer, sidebar and conversation rows read exactly as before."""
        monkeypatch.delenv("AGNES_UI_LAYOUT", raising=False)
        monkeypatch.delenv("AGNES_INSTANCE_THEME", raising=False)
        resp = self._chat(web_client, admin_cookie)
        assert resp.status_code == 200
        # Positive anchors first: prove the REAL chat page rendered (a future
        # redirect away from topnav /chat must not turn the negatives vacuous),
        # and pin the classic composer shape.
        assert 'id="chat-input"' in resp.text
        assert 'rows="2"' in resp.text, "topnav composer keeps the classic two-row textarea"
        assert 'id="chat-plus-menu"' not in resp.text, "composer + upload menu leaked into topnav"
        assert 'id="chat-journey"' not in resp.text, "journey checklist leaked into topnav"
        assert "chat_row_menu.js" not in resp.text, "conversation row menu leaked into topnav"

    def test_rail_chat_keeps_upload_menu_journey_and_row_menu(self, web_client, admin_cookie, monkeypatch):
        """Under rail the additions stay: the composer "+" menu and the row
        menu in the page, the journey checklist as the rail's own
        ``railGetStarted`` card (chat.html's ``#chat-journey`` div is the
        TOPNAV sidebar's slot — rail never renders it)."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        resp = self._chat(web_client, admin_cookie)
        assert resp.status_code == 200
        assert 'id="chat-plus-menu"' in resp.text
        assert 'id="railGetStarted"' in resp.text
        assert "chat_row_menu.js" in resp.text

    def test_topnav_composer_grid_keeps_two_columns(self):
        """The redesign widened `.cloud-chat-form`'s grid to three columns for
        the rail composer's leading "+" button. On topnav that button no
        longer renders, and a 3-column grid with two children drops the
        textarea into the content-sized `auto` column — a visibly narrower
        input (caught by the screenshot audit). The BASE rule is the topnav
        contract: two columns, exactly as before the redesign; rail lays its
        composer out with its own flex rules and never reads this grid."""
        from pathlib import Path

        css = Path("app/web/static/css/chat.css").read_text()
        import re

        m = re.search(r"^\.cloud-chat-form \{(.*?)^\}", css, re.S | re.M)
        assert m, "base .cloud-chat-form rule missing"
        assert "grid-template-columns: 1fr auto;" in m.group(1), (
            "base composer grid must stay two-column (textarea + actions) — "
            'rail-only columns belong under html[data-ui-layout="rail"]'
        )

    def test_chat_onboarding_module_is_rail_gated(self):
        """chat.js statically imports chat_onboarding.js, so the module loads
        on every chrome — the gate has to live in its behavior. Pin the seam:
        the module reads ``data-ui-layout`` off the root element and its boot
        path early-returns off the rail, so topnav gets no journey fetch, no
        greeting bubbles, and no auto-launched coach-mark tour."""
        from pathlib import Path

        src = Path("app/web/static/js/chat_onboarding.js").read_text()
        assert 'dataset.uiLayout === "rail"' in src, (
            "chat_onboarding.js must derive IS_RAIL from the chrome layout attribute"
        )
        # Both boot paths (initChatOnboarding + mountJourneyPanel) must
        # early-return off the rail — a name surviving in a comment is not a
        # gate, so pin the return statements themselves.
        assert len(re.findall(r"if \(!IS_RAIL\) return", src)) >= 2, (
            "both chat_onboarding.js boot paths must early-return when the chrome is not rail"
        )


class TestDetailPageParity:
    """The redesign restructured seven DETAIL templates in place (the
    kind-coloured hero + columns anatomy from ``macros/_detail.html``).
    Topnav keeps the pre-redesign pages — same contract as
    ``TestDefaultContentParity``, extended to the detail level: every render
    site resolves through ``_detail_template()``, which serves
    ``<name>_legacy.html`` (a frozen pre-redesign copy) off the rail.

    Layered: a unit test on the switch, a closed-set static sweep proving all
    seven pairs are wired (no bare literal left behind), and live render
    pairs for the two cheaply-seedable pages (collection + catalog table).
    """

    DETAIL_TEMPLATES = (
        "catalog_table_detail",
        "catalog_package_detail",
        "catalog_recipe_detail",
        "marketplace_plugin_detail",
        "marketplace_item_detail",
        "library_detail",
        "memory_domain_detail",
    )

    def test_detail_template_switch_resolves_by_redesign_opt_in(self, monkeypatch):
        """Same condition as the base templates' chrome gate: rail OR paper
        opts into the redesigned detail anatomy; a default instance gets the
        frozen pre-redesign page."""
        from app.web.router import _detail_template

        monkeypatch.delenv("AGNES_UI_LAYOUT", raising=False)
        monkeypatch.delenv("AGNES_INSTANCE_THEME", raising=False)
        assert _detail_template("catalog_table_detail") == "catalog_table_detail_legacy.html"
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        assert _detail_template("catalog_table_detail") == "catalog_table_detail.html"
        monkeypatch.delenv("AGNES_UI_LAYOUT", raising=False)
        monkeypatch.setenv("AGNES_INSTANCE_THEME", "paper")
        assert _detail_template("catalog_table_detail") == "catalog_table_detail.html"

    #: Behaviours that must hold on BOTH halves of a frozen pair, as the token
    #: that implements them. Freezing a copy forks the page permanently, so a
    #: fix that lands on the redesigned template alone silently reverts itself
    #: for every default instance — which is what happened when this branch met
    #: #1177/#1178 in main: the copies were snapshotted before those fixes, so
    #: the author of a Private entity lost Archive AND install on the default
    #: look while the redesign kept both (Devin Review on #1195).
    #:
    #: A token list rather than a diff: the two halves are SUPPOSED to differ
    #: (that is the whole point of the freeze), so only the load-bearing
    #: predicates can be asserted equal. Add a row whenever a fix has to reach
    #: both.
    FORKED_PAIR_INVARIANTS = (
        (
            "marketplace_plugin_detail",
            "own_private",
            "#1177 — the author's own Private row sits at 'hidden' and must stay deletable",
        ),
        ("marketplace_item_detail", "own_private", "#1177 — same gate on the skill/agent page"),
        (
            "marketplace_plugin_detail",
            "d.installable !== true",
            "#1178 — install is gated on the server-resolved flag, not on the status alone",
        ),
        ("marketplace_item_detail", "d.installable !== true", "#1178 — same gate on the skill/agent page"),
        (
            "library_detail",
            "/f/",
            "the per-file page's only entry point on a default instance — without it, "
            "`/library/<slug>/f/<id>` is reachable only by typing the URL",
        ),
    )

    @pytest.mark.parametrize("base,token,why", FORKED_PAIR_INVARIANTS)
    def test_frozen_copy_carries_the_same_invariant(self, base, token, why):
        from pathlib import Path

        live = Path(f"app/web/templates/{base}.html").read_text()
        legacy = Path(f"app/web/templates/{base}_legacy.html").read_text()

        assert token in live, f"premise moved — {token!r} is no longer in {base}.html ({why})"
        assert token in legacy, (
            f"{base}_legacy.html is missing {token!r} — {why}. A default instance renders the "
            "frozen copy, so a fix applied only to the redesigned template reverts itself there."
        )

    def test_every_detail_render_site_is_switched(self):
        """No render site may keep the bare redesigned template literal — a
        new call site that bypasses the switch reintroduces the redesign on
        topnav silently."""
        from pathlib import Path

        src = Path("app/web/router.py").read_text()
        for name in self.DETAIL_TEMPLATES:
            assert Path(f"app/web/templates/{name}_legacy.html").exists(), f"{name}_legacy.html missing"
            assert f'_detail_template("{name}")' in src, f"{name} render not switched"
            assert f'"{name}.html"' not in src, f"bare {name}.html literal left in router"

    def _seed_collection(self, web_client, admin_cookie, name):
        r = web_client.post("/api/collections", json={"name": name}, cookies=admin_cookie)
        assert r.status_code == 201, r.text
        return r.json()

    def test_topnav_library_detail_is_legacy(self, web_client, admin_cookie, monkeypatch):
        monkeypatch.delenv("AGNES_UI_LAYOUT", raising=False)
        monkeypatch.delenv("AGNES_INSTANCE_THEME", raising=False)
        col = self._seed_collection(web_client, admin_cookie, "Parity Files")
        resp = web_client.get(f"/library/{col['slug']}", cookies=admin_cookie)
        assert resp.status_code == 200
        assert 'class="lib-sec"' in resp.text, "topnav must render the legacy collection detail"
        assert 'class="detail-page"' not in resp.text, "redesigned detail anatomy leaked into topnav"

    def test_rail_library_detail_is_redesigned(self, web_client, admin_cookie, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        col = self._seed_collection(web_client, admin_cookie, "Parity Files Rail")
        resp = web_client.get(f"/library/{col['slug']}", cookies=admin_cookie)
        assert resp.status_code == 200
        assert 'class="detail-page"' in resp.text
        assert 'class="lib-sec"' not in resp.text

    def _seed_table(self, name):
        from src.repositories import table_registry_repo

        table_registry_repo().register(
            id=name,
            name=name,
            source_type="keboola",
            bucket="in.c-test",
            source_table=name,
            query_mode="local",
        )

    def test_topnav_catalog_table_detail_is_legacy(self, web_client, admin_cookie, monkeypatch):
        monkeypatch.delenv("AGNES_UI_LAYOUT", raising=False)
        monkeypatch.delenv("AGNES_INSTANCE_THEME", raising=False)
        self._seed_table("parity_table")
        resp = web_client.get("/catalog/t/parity_table", cookies=admin_cookie)
        assert resp.status_code == 200
        assert "td-back" in resp.text, "topnav must render the legacy table detail"
        assert 'class="detail-page"' not in resp.text

    def test_rail_catalog_table_detail_is_redesigned(self, web_client, admin_cookie, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        self._seed_table("parity_table_rail")
        resp = web_client.get("/catalog/t/parity_table_rail", cookies=admin_cookie)
        assert resp.status_code == 200
        assert 'class="detail-page"' in resp.text
        assert "td-back" not in resp.text
