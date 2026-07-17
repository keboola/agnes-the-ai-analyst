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
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        resp = web_client.get("/dashboard", cookies=admin_cookie)
        assert resp.status_code == 200
        assert 'class="rail"' in resp.text
        assert 'class="app-header"' not in resp.text
        assert 'data-ui-layout="rail"' in resp.text

    def test_rail_keeps_nav_contract(self, web_client, admin_cookie, monkeypatch):
        """Rail must carry the prototype IA (My Stack + Catalog with the
        content surfaces as subcategories) and the same JS/id contract
        as the header: global search combobox, user menu, theme toggle,
        tour anchors."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        resp = web_client.get("/dashboard", cookies=admin_cookie)
        text = resp.text
        for anchor in (
            'id="global-search"',
            'id="userMenu"',
            'id="themeToggle"',
            # prototype IA: My Stack page + Catalog parent
            'href="/stack"',
            'data-tour="nav-stack"',
            'data-tour="nav-catalog"',
            # content surfaces as Catalog subcategories (kind tabs of
            # the unified /catalog page)
            'class="rail-sub"',
            'data-tour="nav-marketplace"',
            'data-tour="nav-library"',
            'data-tour="nav-memory"',
            'href="/catalog?kind=plugins"',
            'href="/catalog?kind=library"',
            'href="/catalog?kind=memory"',
            'href="/catalog"',
            # default brand lockup: the orb + wordmark
            'class="rail-orb"',
        ):
            assert anchor in text, f"rail chrome is missing {anchor}"

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
            'data-kind="library"',
            'class="uc-kindtabs"',
        ):
            assert anchor in resp.text, f"unified catalog is missing {anchor}"

        resp = web_client.get("/stack", cookies=admin_cookie)
        assert resp.status_code == 200
        assert "My Stack" in resp.text
        assert 'data-kind="plugins"' in resp.text

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
