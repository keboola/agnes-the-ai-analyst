"""MCP connector UI visibility switch (#1024).

On a VPN/intranet-only instance the MCP endpoints (`/api/mcp/http`,
`/api/mcp/sse`) work fine for in-network clients, but a cloud-side connector
client (configured from outside the network) can never reach them — so
showing install instructions for that surface is showing a path that cannot
work. `mcp.connector_ui_enabled` (env `AGNES_MCP_CONNECTOR_UI_ENABLED`, default
`true` — current behavior unchanged) lets an operator hide the surface
instead: the standalone connector pages, the MCP tab of `/how-it-works#connect`,
and every nav entry pointing at them. It never touches the MCP protocol itself
— see docs/DEPLOYMENT.md.

The connector surface renders through TWO chrome variants (legacy `/me/
ai-connector` standalone page vs. the redesign's consolidated `/how-it-works`
mode-tab) and TWO context builders (`_build_context` vs. `_chrome_ctx`) — every
test class below exercises both so a fix to one half cannot pass while the
other stays broken.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestSwitchRegistry:
    def test_registry_entry_shape(self):
        from app.switches import get_switch

        s = get_switch("mcp_connector_ui")
        assert s.config_keys == ("mcp", "connector_ui_enabled")
        assert s.env_var == "AGNES_MCP_CONNECTOR_UI_ENABLED"
        assert s.kind == "bool"
        assert s.default is True
        assert s.editable is True


class TestResolver:
    """`get_mcp_connector_ui_enabled()` parses instance.yaml / env exactly
    like its `mcp.*` siblings (`get_mcp_source_url_strict`)."""

    def test_default_true_when_nothing_set(self, monkeypatch):
        import app.instance_config as ic

        ic.reset_cache()
        monkeypatch.delenv("AGNES_MCP_CONNECTOR_UI_ENABLED", raising=False)
        assert ic.get_mcp_connector_ui_enabled() is True

    def test_env_override_false(self, monkeypatch):
        import app.instance_config as ic

        ic.reset_cache()
        monkeypatch.setenv("AGNES_MCP_CONNECTOR_UI_ENABLED", "0")
        assert ic.get_mcp_connector_ui_enabled() is False

    def test_yaml_fallback_and_env_precedence(self, monkeypatch):
        import app.instance_config as ic

        def fake_get_value(*keys, default=None):
            if keys == ("mcp", "connector_ui_enabled"):
                return False
            return default

        monkeypatch.setattr(ic, "get_value", fake_get_value)
        monkeypatch.delenv("AGNES_MCP_CONNECTOR_UI_ENABLED", raising=False)
        assert ic.get_mcp_connector_ui_enabled() is False  # YAML fallback

        monkeypatch.setenv("AGNES_MCP_CONNECTOR_UI_ENABLED", "1")
        assert ic.get_mcp_connector_ui_enabled() is True  # env > YAML


class TestAiConnectorRouteRedirect:
    """`/me/ai-connector` is an unconditional redirect (Wave 0, 2026-08 legacy
    retirement collapsed the legacy-chrome standalone page it used to branch
    to — `me_cowork_legacy.html` is gone, rail is the only chrome) — the
    switch must still short-circuit it home when the MCP connector surface
    is hidden."""

    def test_redirects_home_when_disabled(self, seeded_app, monkeypatch):
        monkeypatch.setattr("app.web.router.get_mcp_connector_ui_enabled", lambda: False)
        c = seeded_app["client"]
        resp = c.get(
            "/me/ai-connector",
            headers=_auth(seeded_app["analyst_token"]),
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == "/"

    def test_redirects_to_connect_section_by_default(self, seeded_app):
        """Default (switch on): redirects to the consolidated connect
        section — it never renders a page of its own (full redirect-chain
        contract in tests/test_web_nav_cowork.py)."""
        c = seeded_app["client"]
        resp = c.get(
            "/me/ai-connector",
            headers=_auth(seeded_app["analyst_token"]),
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == "/how-it-works#connect"


class TestMcpConnectPage:
    def test_redirects_home_when_disabled(self, seeded_app, monkeypatch):
        monkeypatch.setattr("app.web.router.get_mcp_connector_ui_enabled", lambda: False)
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get(
            "/mcp-connect",
            headers=_auth(token),
            cookies={"access_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == "/"

    def test_still_renders_by_default(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get(
            "/mcp-connect",
            headers=_auth(token),
            cookies={"access_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 200


class TestHowItWorksConnectTab:
    """`/how-it-works` is the general orientation page — the switch must hide
    only the MCP mode-tab inside its `#connect` section, not the whole page
    (CLI / chat / privacy content stays)."""

    def test_mcp_tab_and_panel_absent_when_disabled(self, seeded_app, monkeypatch):
        monkeypatch.setattr("app.web.router.get_mcp_connector_ui_enabled", lambda: False)
        c = seeded_app["client"]
        body = c.get("/how-it-works", headers=_auth(seeded_app["analyst_token"])).text

        assert 'data-mode="mcp"' not in body
        assert 'data-mode-panel="mcp"' not in body
        assert 'id="aic-mode-mcp"' not in body
        assert 'id="aic-mode-panel-mcp"' not in body
        # The public #connect / #cli anchors must survive — CLI still works.
        assert '<section class="hiw-sec" id="connect">' in body
        assert 'id="cli" role="tabpanel"' in body

    def test_cli_becomes_the_default_tab_when_disabled(self, seeded_app, monkeypatch):
        monkeypatch.setattr("app.web.router.get_mcp_connector_ui_enabled", lambda: False)
        c = seeded_app["client"]
        body = c.get("/how-it-works", headers=_auth(seeded_app["analyst_token"])).text
        assert 'class="aic-mode-panel is-active" id="cli"' in body

    def test_ai_tool_surface_card_hidden_when_disabled(self, seeded_app, monkeypatch):
        monkeypatch.setattr("app.web.router.get_mcp_connector_ui_enabled", lambda: False)
        c = seeded_app["client"]
        body = c.get("/how-it-works", headers=_auth(seeded_app["analyst_token"])).text
        assert "Your own AI tool" not in body
        assert "Three ways in, one knowledge layer" in body

    def test_everything_still_present_by_default(self, seeded_app):
        c = seeded_app["client"]
        body = c.get("/how-it-works", headers=_auth(seeded_app["analyst_token"])).text
        assert 'data-mode="mcp"' in body
        assert "Your own AI tool" in body
        assert "Four ways in, one knowledge layer" in body


class TestNavEntriesHiddenOnBothContextBuilders:
    """`_build_context` (e.g. /dashboard) and `_chrome_ctx` (e.g.
    /me/memory-mining) are two separate code paths that each render the same
    chrome partials — a fix to only one is the dual-surface bug this repo has
    hit before (Studio's #1190 nav-hidden test uses the same two pages)."""

    def test_build_context_page_hides_nav_and_palette(self, seeded_app, monkeypatch):
        """The admin command palette (``_app_scripts.html``, rendered on
        every page regardless of role — it self-gates at runtime on
        ``#adminMenu`` presence) is the only nav surface left that names
        these routes; the rail chrome's own nav never links to them directly
        (see tests/test_web_nav_cowork.py — /me/ai-connector is reached only
        via /how-it-works#connect)."""
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]

        # Sanity: present by default.
        body = c.get("/dashboard", headers=_auth(token)).text
        assert "href: '/me/ai-connector'" in body
        assert "href: '/mcp-connect'" in body

        monkeypatch.setattr("app.web.router.get_mcp_connector_ui_enabled", lambda: False)
        body = c.get("/dashboard", headers=_auth(token)).text
        assert "href: '/me/ai-connector'" not in body
        assert "href: '/mcp-connect'" not in body

    def test_chrome_ctx_page_hides_palette(self, seeded_app, monkeypatch):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]

        body = c.get("/me/memory-mining", headers=_auth(token)).text
        assert "href: '/me/ai-connector'" in body
        assert "href: '/mcp-connect'" in body

        monkeypatch.setattr("app.web.router.get_mcp_connector_ui_enabled", lambda: False)
        body = c.get("/me/memory-mining", headers=_auth(token)).text
        assert "href: '/me/ai-connector'" not in body
        assert "href: '/mcp-connect'" not in body


class TestUserMenuKeepsAnOrientationEntry:
    """Original concern (Devin Review on #1024): the topnav's default
    (non-paper) chrome rendered an MCP-only "AI Connector" row where paper
    rendered "Learn how it works" — gating the connector row alone left the
    default chrome with an entry to NEITHER surface, so hiding MCP silently
    took `/how-it-works` (which also carries the CLI setup, the
    first-session narrative and the privacy content) out of the user menu as
    a side effect.

    Wave 0 (2026-08 legacy retirement) deleted that topnav chrome along with
    the "AI Connector"/"Learn how it works" fallback branch it required —
    rail is the only chrome now, and its user menu carries ONE unconditional
    "How {brand} works" → /how-it-works entry with no
    `can_mcp_connector_ui` gate at all (confirmed: no such conditional
    wraps it in `_app_rail.html`, and it predates this wave — present since
    the original #896 redesign commit). There is no fallback branch left to
    regress, but the switch-independence itself is worth pinning so it
    cannot quietly grow one back."""

    def test_orientation_entry_survives_the_switch_either_way(self, seeded_app, monkeypatch):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]

        def _rail_orientation_entry_present() -> bool:
            body = c.get("/how-it-works", headers=_auth(token)).text
            rail = body.split('<nav class="rail"', 1)[1].split("</nav>", 1)[0]
            return 'href="/how-it-works"' in rail and ">How Agnes works</a>" in rail

        assert _rail_orientation_entry_present(), "orientation entry must be present with the switch on"

        monkeypatch.setattr("app.web.router.get_mcp_connector_ui_enabled", lambda: False)
        assert _rail_orientation_entry_present(), (
            "orientation entry must survive the MCP connector switch being turned off — "
            "this is the #1024 regression class, now guarded by construction (no shared "
            "conditional), not by a fallback branch"
        )


class TestInPageConnectorReferencesFollowTheSwitch:
    """`/how-it-works` keeps serving with the switch off, so its own copy must
    not keep pointing at the block the switch removed."""

    def test_plugin_packages_note_drops_the_connector_pointer(self, seeded_app, monkeypatch):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]

        body = c.get("/how-it-works", headers=_auth(token)).text
        assert "use the connector above instead" in body

        monkeypatch.setattr("app.web.router.get_mcp_connector_ui_enabled", lambda: False)
        body = c.get("/how-it-works", headers=_auth(token)).text
        assert "use the connector above instead" not in body
        # The rest of the note — the part that is true either way — stays.
        assert "one package per zip" in body

    def test_connector_troubleshooting_entries_are_hidden(self, seeded_app, monkeypatch):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]

        body = c.get("/how-it-works", headers=_auth(token)).text
        assert "MCP endpoint not found" in body
        assert "Can't add a custom connector on Claude.ai?" in body

        monkeypatch.setattr("app.web.router.get_mcp_connector_ui_enabled", lambda: False)
        body = c.get("/how-it-works", headers=_auth(token)).text
        assert "MCP endpoint not found" not in body
        assert "Can't add a custom connector on Claude.ai?" not in body
        # Transport-agnostic entries are not connector-specific and stay.
        assert "First question errors right after sign-in?" in body
        assert "VS Code sign-in stuck?" in body
