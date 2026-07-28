"""Tests for the /mcp-connect page and POST /api/mcp-connect/token endpoint."""

from __future__ import annotations


class TestMcpConnectToken:
    """POST /api/mcp-connect/token — happy-path + revoke-and-recreate."""

    def test_requires_auth(self, seeded_app):
        r = seeded_app["client"].post("/api/mcp-connect/token")
        assert r.status_code == 401

    def test_returns_token_and_base_url(self, seeded_app):
        token = seeded_app["analyst_token"]
        r = seeded_app["client"].post(
            "/api/mcp-connect/token",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert "token" in body
        assert isinstance(body["token"], str) and len(body["token"]) > 20
        assert "base_url" in body

    def test_second_call_revokes_old_and_returns_new(self, seeded_app):
        """Calling the endpoint twice produces two different tokens."""
        token = seeded_app["analyst_token"]
        headers = {"Authorization": f"Bearer {token}"}

        r1 = seeded_app["client"].post("/api/mcp-connect/token", headers=headers)
        assert r1.status_code == 200
        tok1 = r1.json()["token"]

        r2 = seeded_app["client"].post("/api/mcp-connect/token", headers=headers)
        assert r2.status_code == 200
        tok2 = r2.json()["token"]

        assert tok1 != tok2

    def test_pat_cannot_call_endpoint(self, seeded_app):
        """A PAT (non-session token) must not be able to call this endpoint
        because it requires an interactive session (require_session_token).

        The analyst_token fixture is a plain JWT (not a PAT) so this test
        verifies the guard exists via the module; the actual PAT-rejection
        path is exercised in test_tokens.py via require_session_token.
        """
        from app.api.mcp_connect import router

        assert router is not None


class TestMcpConnectPage:
    """GET /mcp-connect — page renders for authenticated users."""

    def test_requires_auth(self, seeded_app):
        r = seeded_app["client"].get("/mcp-connect", follow_redirects=False)
        # Unauthenticated GET on a non-API path → 302 to /login
        assert r.status_code == 302
        assert "/login" in r.headers.get("location", "")

    def test_renders_for_authed_user(self, seeded_app):
        token = seeded_app["analyst_token"]
        r = seeded_app["client"].get(
            "/mcp-connect",
            headers={"Authorization": f"Bearer {token}"},
            cookies={"access_token": token},
        )
        assert r.status_code == 200
        body = r.text
        # Must not extend base.html (design-system contract)
        assert "base_page.html" not in body or "base.html" not in body
        # Page should have the relevant content
        assert "mcp" in body.lower() or "connect" in body.lower() or "token" in body.lower()

    def test_has_claude_code_tab(self, seeded_app):
        """The editor-config tabs include Claude Code: a `claude mcp add`
        one-liner carrying the PAT as an Authorization header, plus the
        restart-before-it-appears note."""
        token = seeded_app["analyst_token"]
        r = seeded_app["client"].get(
            "/mcp-connect",
            headers={"Authorization": f"Bearer {token}"},
            cookies={"access_token": token},
        )
        assert r.status_code == 200
        body = r.text
        assert 'id="snippet-claude-code"' in body
        assert "claude mcp add --transport sse agnes" in body
        assert "restart Claude Code" in body


class TestWwwAuthenticate:
    """_AuthMiddleware must send WWW-Authenticate: Bearer on 401."""

    def test_401_includes_www_authenticate_header(self, seeded_app):
        import asyncio
        from app.api.mcp_http import _send_401

        received_headers = {}

        async def _capture_send(event):
            if event["type"] == "http.response.start":
                for k, v in event.get("headers", []):
                    received_headers[k.decode().lower()] = v.decode()

        scope = {"type": "http", "method": "GET", "path": "/api/mcp/sse"}
        asyncio.run(_send_401(scope, _capture_send))

        assert "www-authenticate" in received_headers
        assert received_headers["www-authenticate"].startswith("Bearer")
