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
        """Rail must carry the prototype IA (Library + Agents as flat
        destinations) and the same JS/id contract as the header: user menu,
        theme toggle."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        resp = web_client.get("/stack", cookies=admin_cookie)
        text = resp.text
        for anchor in (
            'id="userMenu"',
            'id="themeToggle"',
            # Library — private uploads (moved off My Stack) + future data
            # apps; flagged work-in-progress with a WIP badge.
            'href="/library"',
            # Agents — build an assistant out of the caller's stack. A primary
            # destination directly under Library (it used to sit one hover deep
            # inside the Studio dropdown); WIP badge.
            'href="/agents"',
            # brand lockup: the Agnes orb mark + the Agnes wordmark beside it.
            'class="rail-orb"',
            'class="rail-logo-txt"',
        ):
            assert anchor in text, f"rail chrome is missing {anchor}"
        # The Library entry carries a WIP badge.
        assert 'class="rail-badge"' in text
        assert ">WIP<" in text
        # Nav order, top → bottom: New chat · Library · Agents — one flat group,
        # no dividers (the rail is down to three destinations, so grouping rules
        # just add noise). New chat renders only for chat-granted callers, so
        # it's not pinned here.
        assert 'class="rail-nav-sep"' not in text
        positions = [
            text.index('href="/library"'),
            text.index('href="/agents"'),
        ]
        assert positions == sorted(positions), "rail nav items are out of order"
        # Catalog is a single flat destination — no nested subcategory tree.
        assert 'class="rail-sub"' not in text

    def test_rail_has_no_my_stack_entry(self, web_client, admin_cookie, monkeypatch):
        """My Stack is demoted out of the rail (#1088) — /stack stays a live
        route, it is simply not a primary destination any more. Asserted against
        the rail nav slice, not the whole document: this probes the /stack page
        itself, whose body legitimately mentions the stack throughout."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        text = web_client.get("/stack", cookies=admin_cookie).text
        nav = text.split('class="rail-nav"', 1)[1].split("</nav>", 1)[0]
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
        head = text.split('class="lib-actions"', 1)[1].split("</div>", 1)[0]
        assert 'href="/stack"' not in head

        rows = re.findall(r"<tr[^>]*\bdata-item-id=[^>]*>", text)
        top_level = [r for r in rows if "data-parent-id=" not in r]
        in_stack = [r for r in top_level if 'data-stack="in_stack"' in r]
        rendered = 'data-facet="stack" value="in_stack"' in text
        assert rendered == (0 < len(in_stack) < len(top_level)), (
            f"stack toggle rendered={rendered} with {len(in_stack)}/{len(top_level)} rows in stack — "
            "it must render exactly when flipping it would change the list"
        )
        if rendered:
            assert "In stack only" in text

    def test_rail_has_no_studio_or_marketplace_entry(self, web_client, admin_cookie, monkeypatch):
        """Studio is retired from the rail and Marketplace is no longer a rail
        entry. Studio was a hover dropdown holding Agents (now its own top-level
        item), the Skill and Plugin builders (now reached from the Library
        header's "+ New" menu) and a non-interactive "Corporate Memory builder"
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
        share dialog, and a WIP banner for the not-yet-built data apps."""
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
        # Owner-initiated sharing: every grant-backed row gets a Share action,
        # and the dialog it opens ships with the page.
        assert 'id="shareModal"' in text
        assert 'id="shareGroups"' in text
        # Every "add something" path sits behind ONE chevron button.
        assert 'id="lib-new-btn"' in text
        assert 'id="lib-new-menu"' in text
        for label in ("Build a skill", "Build a plugin", "Upload a file"):
            assert f"<span>{label}</span>" in text
        # Data apps are still in design — a WIP banner stands in for them.
        assert "Data apps" in text
        assert "lib-apps" in text
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


class TestRailChatHistory:
    """Rail chat-history migration (#896): the conversation history lives in the
    left rail — a collapsible, scrollable Chats section under the primary
    destinations, present on every page (not just /chat). The standalone
    "+ New chat" button is retired and the chat entry is renamed "New chat"
    (id="new-chat", so chat.js resets in place on /chat). All gated on can_chat.
    Topnav is unaffected — its in-page chat sidebar is unchanged."""

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
        # Collapsible history section + the reused chat list ids live in the rail.
        assert 'class="rail-history"' in text
        assert 'id="chat-list"' in text
        assert 'id="cloud-chat-empty-state"' in text
        # The chat entry is renamed and carries id="new-chat" (chat.js hook).
        assert 'id="new-chat"' in text
        assert "New chat" in text
        # The standalone +New chat button above the nav (old markup) is retired.
        assert 'class="rail-newchat"' not in text
        # The loader that fills the list off /chat is wired in.
        assert "js/rail_history.js" in text

    def test_rail_getstarted_launcher_hosts_the_journey(self, web_client, admin_cookie, monkeypatch):
        """The onboarding "Your Journey" panel moved out of the (cramped) Chats
        list into a "Get started" popover pinned in the rail foot. #chat-journey
        now lives in that popover, and a standalone module mounts it on non-chat
        pages."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        self._enable_chat(web_client, monkeypatch)
        resp = web_client.get("/stack", cookies=admin_cookie)
        assert resp.status_code == 200
        text = resp.text
        # Launcher + popover in the foot.
        assert 'id="rail-getstarted-toggle"' in text
        assert 'id="rail-getstarted-panel"' in text
        # The journey render target moved into the popover — and out of the list.
        journey_pos = text.find('id="chat-journey"')
        panel_pos = text.find('id="rail-getstarted-panel"')
        assert journey_pos != -1 and panel_pos != -1
        assert journey_pos > panel_pos, "#chat-journey must render inside the Get started popover"
        assert text.find('id="chat-journey"', text.find('class="rail-history"'), panel_pos) == -1, (
            "#chat-journey must no longer sit in the Chats history section"
        )
        # Off /chat, the standalone mount fills the popover.
        assert "mountJourneyPanel" in text

    def test_rail_history_absent_without_chat_grant(self, web_client, admin_cookie, monkeypatch):
        """No chat reachability → no history section, no New chat item, no
        Get started launcher, no loader (matches the "Chat slot only when
        reachable" contract)."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        # Chat is disabled by default in tests, so can_chat is False.
        resp = web_client.get("/stack", cookies=admin_cookie)
        assert resp.status_code == 200
        assert 'class="rail-history"' not in resp.text
        assert 'id="new-chat"' not in resp.text
        assert 'id="rail-getstarted-toggle"' not in resp.text
        assert "js/rail_history.js" not in resp.text


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
            'href="/me/ai-connector"',  # the green "Connect your tools" CTA target
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
        for retired in ('id="rdb-continue-list"', 'id="rdb-tasks"', "Recent updates"):
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
        assert re.search(r'class="rail-i rail-newchat-item[^"]*\bon\b[^"]*"\s+id="new-chat"', resp.text)
        # Deep-linked into a conversation → New chat is not active.
        resp = web_client.get("/chat?session=abc", cookies=admin_cookie)
        assert resp.status_code == 200
        assert not re.search(r'rail-newchat-item[^"]*\bon\b[^"]*"\s+id="new-chat"', resp.text)

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

    @staticmethod
    def _selectors(path: str) -> list[str]:
        """Rule selectors from a flat CSS sheet — comments stripped,
        at-rules (@media/@supports wrappers) skipped; rules nested in
        at-rule bodies still surface as ordinary selectors."""
        css = re.sub(r"/\*.*?\*/", "", open(path).read(), flags=re.DOTALL)
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
