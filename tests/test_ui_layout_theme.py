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
        """Rail must carry the prototype IA (Chat + My Stack + Catalog as
        three flat destinations) and the same JS/id contract as the
        header: user menu, theme toggle. The content surfaces
        (Plugins/Library/Memory) are reached as kind tabs on the unified
        /catalog page, not as rail subcategories."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        resp = web_client.get("/stack", cookies=admin_cookie)
        text = resp.text
        for anchor in (
            'id="userMenu"',
            'id="themeToggle"',
            # prototype IA: My Stack page + Catalog parent
            'href="/stack"',
            'href="/catalog"',
            # default brand lockup: the orb + wordmark
            'class="rail-orb"',
        ):
            assert anchor in text, f"rail chrome is missing {anchor}"
        # Catalog is a single flat destination — no nested subcategory tree.
        assert 'class="rail-sub"' not in text
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
        assert 'data-kind="plugins"' in resp.text
        # The Uploads tab moved here from the Catalog.
        assert 'data-kind="upload"' in resp.text
        # An "All" tab is the default view over the inventory table, so the
        # page never lands empty.
        assert 'data-kind="all"' in resp.text
        assert 'class="uc-kindtab on" data-kind="all"' in resp.text
        # The manage zone is ONE inventory table (not another card grid).
        assert 'id="stk-table"' in resp.text
        assert "Everything in your Stack" in resp.text

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
    My Stack (the page exists to start Kai conversations, so without a
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

    def test_rail_dashboard_redirects_to_stack_without_chat_grant(self, web_client, admin_cookie, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        # Chat is disabled by default in tests, so can_chat is False.
        resp = web_client.get("/dashboard", cookies=admin_cookie, follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/stack"

    def test_ask_is_retired(self, web_client, admin_cookie, monkeypatch):
        """The /ask hero is retired — it 302s to / rather than rendering."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        resp = web_client.get("/ask", cookies=admin_cookie, follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/"


class TestRailDashboard:
    """The rail Dashboard = Chat's pre-conversation state: /chat with no
    active conversation renders the Kai-centric dashboard (greeting, the
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
            'id="rdb-greeting-tod"',  # greeting
            'class="klb klb--bare"',  # Knowledge Layer banner fused into the hero box
            "One knowledge layer. Everywhere you work.",  # banner headline
            "Ask Kai in Agnes",  # banner LEFT card
            "Use your own AI tools",  # banner RIGHT card
            "Agnes Knowledge Layer",  # banner CENTER hub
            'class="klb-cta-primary" href="/me/ai-connector"',  # banner primary CTA → connect page
            'class="klb-cta-secondary" href="/home"',  # banner secondary CTA → how-it-works walkthrough
            "Suggested next actions",  # the one personalized section
            'id="rdb-actions-list"',  # suggested-actions list
            "css/chat_dashboard.css",  # dashboard styles
            'id="chat-input"',  # the REAL composer serves the dashboard
        ):
            assert anchor in text, f"rail chat dashboard is missing {anchor}"
        # The retired three-panel layout is gone (one actions list instead).
        for retired in ('id="rdb-continue-list"', 'id="rdb-tasks"', "Recent updates"):
            assert retired not in text, f"retired dashboard panel leaked back: {retired}"
        # One composer only — the retired standalone dashboard's look-alike
        # input and its prompt-handoff module must be gone.
        assert 'id="rdb-composer"' not in text
        assert "dashboard_rail" not in text
        # The retired ask-hero brand block is gone too.
        assert "Ask anything." not in text

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

    def test_rail_nav_shows_dashboard_first(self, web_client, admin_cookie, monkeypatch):
        """Dashboard sits ABOVE New chat in the rail nav, and the rail logo
        lands on it (href = home_route, default /dashboard)."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        self._enable_chat(web_client, monkeypatch)
        resp = web_client.get("/stack", cookies=admin_cookie)
        assert resp.status_code == 200
        text = resp.text
        dash = text.find('href="/dashboard"')
        newchat = text.find('id="new-chat"')
        assert dash != -1, "rail nav is missing the Dashboard item"
        assert newchat != -1
        assert dash < newchat, "Dashboard must be the first nav item, above New chat"
        assert 'class="rail-logo" href="/dashboard"' in text

    def test_rail_nav_hides_dashboard_without_chat_grant(self, web_client, admin_cookie, monkeypatch):
        """Without a chat grant /dashboard 302s to /stack, so the nav item
        would be a link that bounces — it must not render."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        resp = web_client.get("/stack", cookies=admin_cookie)
        assert resp.status_code == 200
        # Exactly one /dashboard href remains: the logo (whose home_route
        # target is fine — the route itself bounces grant-less users to
        # /stack). The nav item would be a second occurrence.
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
    """My Stack is the manage surface: "Everything in your Stack" — one
    inventory table across kinds. The stat strip counts the stack itself
    (items / plugins / memories / uploads), not estate telemetry. Growing
    the stack happens on /catalog, which carries the "Recommended for
    you" row (see TestCatalogRecommendations)."""

    def test_stack_strip_shows_workspace_stats(self, web_client, admin_cookie, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        resp = web_client.get("/stack", cookies=admin_cookie)
        assert resp.status_code == 200
        # The strip's stat labels are exactly the four workspace counters —
        # the retired capability metrics (Questions this week / Data
        # sources / Skills / Memory facts) are gone.
        labels = re.findall(r'class="stk-stat__label">([^<]+)<', resp.text)
        assert labels == ["Items in your stack", "Plugins", "Memories", "Uploads"]

    def test_stack_inventory_is_a_table_with_toolbar(self, web_client, admin_cookie, monkeypatch):
        """The page is one table (search above, sort control, kind tabs) —
        no card grid anywhere."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        resp = web_client.get("/stack", cookies=admin_cookie)
        assert resp.status_code == 200
        text = resp.text
        assert 'id="stk-table"' in text
        assert 'id="stk-search"' in text
        assert 'id="stk-sort"' in text
        for col in ("Name", "Type", "Details", "Added", "Shared by", "Status"):
            assert "<th" in text and col in text
        # No card grid — recommendations moved to /catalog.
        assert 'class="uc-grid"' not in text
        assert "Recommended for you" not in text
        assert "stk-recs" not in text


class TestCatalogRecommendations:
    """ "Recommended for you" lives on /catalog (moved from /stack): a
    single side-scrollable card row above the kind tabs, listing catalog
    assets NOT yet in the caller's stack, rendered with the same
    catalog_card component as the grids below."""

    def test_recommendations_exclude_stack_items(self, web_client, admin_cookie, monkeypatch):
        """A package NOT in the stack shows under "Recommended for you";
        once subscribed it leaves the recommendations (but stays in the
        browse grid below)."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
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
        conn.close()

        def zones(text: str) -> tuple[str, str]:
            """(recommendations row, everything below it) — the recs section
            renders above the kind tabs."""
            head, _, tail = text.partition('class="uc-kindtabs"')
            return head, tail

        resp = web_client.get("/catalog", cookies=admin_cookie)
        assert resp.status_code == 200
        head, tail = zones(resp.text)
        assert "Recommended for you" in head
        assert "Unstacked Package XYZ" in head, "unstacked package must be recommended"
        # Recommendations reuse the SAME card component as the grids below.
        assert 'class="uc-recs-grid"' in head and 'class="cc-card"' in head
        assert "Unstacked Package XYZ" in tail, "package must still browse in its kind grid"

        from src.repositories.user_stack_subscriptions import UserStackSubscriptionsRepository

        conn = get_system_db()
        UserStackSubscriptionsRepository(conn).subscribe("admin1", "data_package", pkg_id)
        conn.close()

        resp = web_client.get("/catalog", cookies=admin_cookie)
        head, tail = zones(resp.text)
        assert "Unstacked Package XYZ" not in head, "stacked item must leave recommendations"
        assert "Unstacked Package XYZ" in tail, "stacked item must stay browsable in the grid"


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
