"""Per-instance feature flag for the "connect your AI client" UI/instructions
(`ai_connector.enabled` / `AGNES_AI_CONNECTOR_ENABLED`) — same mechanism as
the Studio flag (`studio.enabled` / `get_studio_enabled`) and the Agent
profiles flag (`agent_profiles.enabled` / `get_agent_profiles_enabled`, see
`tests/test_agent_profiles_flag.py`).

Covers (#1024):
- `get_ai_connector_enabled()` defaults on (registry + resolver) — a
  disabled flag must be an explicit opt-out, never the out-of-the-box
  behavior.
- `GET /me/ai-connector` and `GET /mcp-connect` redirect home when disabled.
- `GET /how-it-works` drops the `#connect` section (and its two TOC rows,
  `#connect` and `#cli`) when disabled, while the rest of the page
  (`#overview`, `#knowledge`, `#surfaces`, `#first-run`, `#privacy`,
  `#reference`) keeps rendering.
- The underlying `/api/mcp/http` connector endpoint is unaffected by the
  flag either way — this switch hides UI/instructions only, never the
  connector capability itself.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestResolverDefaultsOn:
    def test_flag_defaults_on_out_of_the_box(self):
        """No env var, no instance.yaml `ai_connector` block — must behave
        exactly as before this flag was introduced."""
        from app.instance_config import get_ai_connector_enabled

        assert get_ai_connector_enabled() is True

    def test_env_var_off_disables_it(self, monkeypatch):
        from app.instance_config import get_ai_connector_enabled

        monkeypatch.setenv("AGNES_AI_CONNECTOR_ENABLED", "0")
        assert get_ai_connector_enabled() is False

    def test_env_var_on_stays_enabled(self, monkeypatch):
        from app.instance_config import get_ai_connector_enabled

        monkeypatch.setenv("AGNES_AI_CONNECTOR_ENABLED", "1")
        assert get_ai_connector_enabled() is True


class TestRegistryEntry:
    def test_switch_is_registered(self):
        from app.switches import get_switch

        s = get_switch("ai_connector")
        assert s.config_keys == ("ai_connector", "enabled")
        assert s.env_var == "AGNES_AI_CONNECTOR_ENABLED"
        assert s.default is True
        assert s.kind == "bool"
        assert s.editable is True


class TestMeAiConnectorPage:
    def test_renders_when_enabled(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/me/ai-connector", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200

    def test_redirects_home_when_disabled(self, seeded_app, monkeypatch):
        monkeypatch.setattr("app.web.router.get_ai_connector_enabled", lambda: False)
        c = seeded_app["client"]
        resp = c.get(
            "/me/ai-connector",
            headers=_auth(seeded_app["analyst_token"]),
            follow_redirects=False,
        )
        assert resp.status_code in (302, 307)
        assert resp.headers.get("location", "") == "/"


class TestMcpConnectPage:
    def test_renders_when_enabled(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/mcp-connect", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200

    def test_redirects_home_when_disabled(self, seeded_app, monkeypatch):
        monkeypatch.setattr("app.web.router.get_ai_connector_enabled", lambda: False)
        c = seeded_app["client"]
        resp = c.get(
            "/mcp-connect",
            headers=_auth(seeded_app["analyst_token"]),
            follow_redirects=False,
        )
        assert resp.status_code in (302, 307)
        assert resp.headers.get("location", "") == "/"


class TestHowItWorksConnectSection:
    def _get(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/how-it-works", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        return resp.text

    def test_connect_section_present_when_enabled(self, seeded_app):
        page = self._get(seeded_app)
        assert 'id="connect"' in page
        assert 'id="cli"' in page
        # The TOC rows for the section — unique via their `title` attribute
        # (a bare `href="#connect"` also appears on the #surfaces card's
        # "Connect your tool" link, which stays regardless of the flag —
        # see test_rest_of_the_page_still_renders_when_disabled).
        assert 'title="Set up your tools"' in page
        assert 'title="Terminal &amp; data"' in page

    def test_connect_section_and_toc_rows_absent_when_disabled(self, seeded_app, monkeypatch):
        monkeypatch.setattr("app.web.router.get_ai_connector_enabled", lambda: False)
        page = self._get(seeded_app)
        assert 'id="connect"' not in page
        assert 'id="cli"' not in page
        assert 'title="Set up your tools"' not in page
        assert 'title="Terminal &amp; data"' not in page

    def test_rest_of_the_page_still_renders_when_disabled(self, seeded_app, monkeypatch):
        """Only the #connect section (and its two TOC rows) drops out — every
        other section on the page is unaffected."""
        monkeypatch.setattr("app.web.router.get_ai_connector_enabled", lambda: False)
        page = self._get(seeded_app)
        for section_id in ("overview", "knowledge", "surfaces", "first-run", "privacy", "reference"):
            assert f'id="{section_id}"' in page, f"#{section_id} should still render"
            assert f'href="#{section_id}"' in page, f"TOC row for #{section_id} should still render"
        assert "One knowledge layer" in page
        # Out of scope per #1024: only the three named surfaces are gated,
        # nothing else — the #surfaces card's own "Connect your tool" link
        # (a dead link once #connect is gone, harmless no-op via the page's
        # own JS null-checks) stays exactly where it was.
        assert 'href="#connect">Connect your tool' in page


class TestMcpHttpEndpointUnaffected:
    """The flag hides UI/instructions only — the connector endpoint itself
    must behave identically whether the flag is on or off."""

    MCP_ENDPOINT = "/api/mcp/http/mcp"

    def _unauthenticated_challenge(self, seeded_app):
        client = seeded_app["client"]
        return client.post(
            self.MCP_ENDPOINT,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={"Accept": "application/json, text/event-stream"},
        )

    def test_endpoint_reachable_when_enabled(self, seeded_app):
        r = self._unauthenticated_challenge(seeded_app)
        assert r.status_code == 401
        assert r.headers.get("www-authenticate", "").lower().startswith("bearer")

    def test_endpoint_unaffected_when_disabled(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_AI_CONNECTOR_ENABLED", "0")
        r = self._unauthenticated_challenge(seeded_app)
        assert r.status_code == 401
        assert r.headers.get("www-authenticate", "").lower().startswith("bearer")
