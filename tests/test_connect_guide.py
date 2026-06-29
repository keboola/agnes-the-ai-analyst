"""Tests for the /connect-guide page (OAuth MCP connector setup guide)."""

from __future__ import annotations


class TestConnectGuidePage:
    """GET /connect-guide — informational page renders for authenticated users."""

    def test_requires_auth(self, seeded_app):
        r = seeded_app["client"].get("/connect-guide", follow_redirects=False)
        # Unauthenticated GET on a non-API path → 302 to /login
        assert r.status_code == 302
        assert "/login" in r.headers.get("location", "")

    def test_renders_for_authed_user(self, seeded_app):
        token = seeded_app["analyst_token"]
        r = seeded_app["client"].get(
            "/connect-guide",
            headers={"Authorization": f"Bearer {token}"},
            cookies={"access_token": token},
        )
        assert r.status_code == 200
        body = r.text
        # Per-agent picker + connector path are present in the rendered guide.
        assert "connect" in body.lower()
        assert "/api/mcp/http" in body
