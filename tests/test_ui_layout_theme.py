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
        header: global search combobox, user menu, theme toggle, tour
        anchors. The content surfaces (Plugins/Library/Memory) are reached
        as kind tabs on the unified /catalog page, not as rail
        subcategories."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        resp = web_client.get("/stack", cookies=admin_cookie)
        text = resp.text
        for anchor in (
            'id="global-search"',
            'id="userMenu"',
            'id="themeToggle"',
            # prototype IA: My Stack page + Catalog parent
            'href="/stack"',
            'data-tour="nav-stack"',
            'data-tour="nav-catalog"',
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


class TestDashboardLandingRedirect:
    """Rail IA convergence (#896): the legacy table-inventory /dashboard is not
    a landing surface under the rail. It 302s to the working chat (when
    reachable) or My Stack. Topnav instances must be byte-for-byte unchanged —
    /dashboard still renders there."""

    def test_topnav_dashboard_still_renders(self, web_client, admin_cookie, monkeypatch):
        monkeypatch.delenv("AGNES_UI_LAYOUT", raising=False)
        resp = web_client.get("/dashboard", cookies=admin_cookie, follow_redirects=False)
        assert resp.status_code == 200
        assert 'data-ui-layout="topnav"' in resp.text

    def test_rail_dashboard_redirects_to_landing(self, web_client, admin_cookie, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        resp = web_client.get("/dashboard", cookies=admin_cookie, follow_redirects=False)
        assert resp.status_code == 302
        # Chat when the caller can reach it, else My Stack — never back to the
        # legacy dashboard (that would loop through the home route).
        assert resp.headers["location"] in ("/chat", "/stack")
        assert resp.headers["location"] != "/dashboard"

    def test_ask_is_retired(self, web_client, admin_cookie, monkeypatch):
        """The /ask hero is retired — it 302s to / rather than rendering."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        resp = web_client.get("/ask", cookies=admin_cookie, follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/"


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


class TestStackCapabilityStrip:
    """The My Stack strip reads as "what your agents can draw on" — the
    caller's activity (Questions this week) plus the estate's reach (Data
    sources / Skills / Memory facts), not the old raw data-shape metrics
    (Tables/Rows/Columns/Data size) and not consumption telemetry."""

    def test_capability_stats_counts_knowledge_and_skills(self, web_client, admin_cookie):
        """`_compute_capability_stats(user)` counts approved, non-personal
        corporate memory as facts and curated+store entities as skills, with
        formatted display strings. `admin_cookie` bootstraps the DB. With no
        seeded usage/source rows, questions_week and sources degrade to 0."""
        from app.web.router import _compute_capability_stats
        from src.db import get_system_db
        from src.repositories.knowledge import KnowledgeRepository
        from src.repositories.store_entities import StoreEntitiesRepository

        conn = get_system_db()
        krepo = KnowledgeRepository(conn)
        # Approved + shareable → counts. Pending and personal must NOT.
        krepo.create(
            id="k-ok",
            title="Fact",
            content="Revenue is booked at invoice.",
            category="finance",
            status="approved",
            is_personal=False,
        )
        krepo.create(
            id="k-pending", title="Draft", content="unreviewed", category="finance", status="pending", is_personal=False
        )
        krepo.create(
            id="k-personal", title="Mine", content="personal note", category="misc", status="approved", is_personal=True
        )
        StoreEntitiesRepository(conn).create(
            id="s-1",
            owner_user_id="admin1",
            owner_username="admin",
            type="skill",
            name="churn-analysis",
            description="d",
            category="analytics",
            version="1.0.0",
        )
        conn.close()

        stats = _compute_capability_stats({"id": "admin1", "email": "admin@example.com"})
        assert stats["memory_facts"] == 1, "only approved, non-personal facts count"
        assert stats["memory_facts_display"] == "1"
        assert stats["skills"] >= 1, "store entity counts as a skill"
        assert stats["skills_display"] == f"{stats['skills']:,}"
        # Activity + sources are present as keys and non-negative (no rows → 0).
        # `sources` is distinct source_type of registered tables (NOT the
        # source_connections credential table, which is empty on bundled-data
        # instances), so it populates whenever there is data.
        assert stats["questions_week"] >= 0
        assert stats["sources"] >= 0

    def test_stack_strip_hidden_when_all_metrics_zero(self, web_client, admin_cookie, monkeypatch):
        """A sparse instance (every metric 0) renders NO strip — not an empty
        band with a lone freshness caption."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        import app.web.router as router

        monkeypatch.setattr(
            router,
            "_compute_data_stats",
            lambda: {
                "tables": 3,
                "total_tables": 3,
                "columns": 0,
                "rows_display": "0",
                "size_display": "0 MB",
                "total_rows": 0,
                "size_bytes": 0,
                "last_updated": "2026-07-21 11:25:07",
                "last_updated_display": "2026-07-21 11:25:07",
                "remote_tables": 0,
                "local_tables": 3,
            },
        )
        monkeypatch.setattr(
            router,
            "_compute_capability_stats",
            lambda user: {
                "questions_week": 0,
                "questions_week_display": "0",
                "sources": 0,
                "sources_display": "0",
                "skills": 0,
                "skills_display": "0",
                "memory_facts": 0,
                "memory_facts_display": "0",
            },
        )
        resp = web_client.get("/stack", cookies=admin_cookie)
        assert resp.status_code == 200
        # The strip container must be absent (the class also appears in the
        # page's <style> block as `.stk-stats {…}`, so match the rendered
        # markup — the opening tag — not the bare class name).
        assert '<div class="stk-stats"' not in resp.text
        # …and with it the freshness caption text (present only inside the strip).
        assert "Updated 2026-07-21 11:25:07" not in resp.text

    def test_stack_strip_shows_capability_labels(self, web_client, admin_cookie, monkeypatch):
        """When the estate is non-empty, the /stack strip renders the new
        Questions/Data sources/Skills/Memory facts cards and drops the retired
        Tables/Rows/Columns/Data-size metrics."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        import app.web.router as router

        # Force a populated footprint so the strip renders (it gates on
        # tables>0) without seeding the whole data pipeline.
        monkeypatch.setattr(
            router,
            "_compute_data_stats",
            lambda: {
                "tables": 7,
                "total_tables": 7,
                "columns": 40,
                "rows_display": "5,500",
                "size_display": "43.2 KB",
                "total_rows": 5500,
                "size_bytes": 44237,
                "last_updated": None,
                "last_updated_display": None,
                "remote_tables": 0,
                "local_tables": 7,
            },
        )
        monkeypatch.setattr(
            router,
            "_compute_capability_stats",
            lambda user: {
                "questions_week": 142,
                "questions_week_display": "142",
                "sources": 3,
                "sources_display": "3",
                "skills": 420,
                "skills_display": "420",
                "memory_facts": 12,
                "memory_facts_display": "12",
            },
        )
        resp = web_client.get("/stack", cookies=admin_cookie)
        assert resp.status_code == 200
        for label in ("Questions this week", "Data sources", "Skills", "Memory facts"):
            assert f">{label}<" in resp.text, f"strip missing {label!r}"
        # Retired metrics must be gone as stat labels.
        assert ">Data size<" not in resp.text
        assert ">Columns<" not in resp.text
        assert ">Rows<" not in resp.text


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
