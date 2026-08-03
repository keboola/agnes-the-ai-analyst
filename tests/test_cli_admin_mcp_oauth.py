"""CLI tests for `agnes admin mcp source oauth-register` / `oauth-client`
(2026-07-30 outbound MCP OAuth sources spec §2).

Mocks ``cli.commands.admin_mcp``'s ``api_*`` helpers directly (no HTTP, no
full app import) — same style as ``tests/test_cli_admin_analytics.py``.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from cli.main import app

pytest.importorskip("mcp", reason="mcp SDK not installed")

runner = CliRunner()


def _resp(status_code=200, json_data=None, text=""):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data if json_data is not None else {}
    r.text = text
    return r


def _sources_list_resp():
    return _resp(200, [{"id": "src_abc123", "name": "oauth-src"}])


class TestOAuthRegister:
    def test_success_prints_client_id(self):
        body = {
            "issuer": "https://as.example.com",
            "client_id": "new-client-id",
            "has_client_secret": True,
            "authorization_endpoint": "https://as.example.com/authorize",
            "token_endpoint": "https://as.example.com/token",
            "scopes": None,
        }
        with (
            patch("cli.commands.admin_mcp.api_get", return_value=_sources_list_resp()),
            patch("cli.commands.admin_mcp.api_post", return_value=_resp(200, body)) as mock_post,
        ):
            result = runner.invoke(app, ["admin", "mcp", "source", "oauth-register", "src_abc123"])
        assert result.exit_code == 0, result.output
        assert "new-client-id" in result.output
        mock_post.assert_called_once_with("/api/admin/mcp-sources/src_abc123/oauth/register", json=None)

    def test_passes_scopes(self):
        with (
            patch("cli.commands.admin_mcp.api_get", return_value=_sources_list_resp()),
            patch("cli.commands.admin_mcp.api_post", return_value=_resp(200, {"client_id": "c1"})) as mock_post,
        ):
            result = runner.invoke(
                app, ["admin", "mcp", "source", "oauth-register", "src_abc123", "--scopes", "read write"]
            )
        assert result.exit_code == 0, result.output
        mock_post.assert_called_once_with(
            "/api/admin/mcp-sources/src_abc123/oauth/register", json={"scopes": "read write"}
        )

    def test_json_output(self):
        body = {"client_id": "c1", "issuer": "https://as.example.com"}
        with (
            patch("cli.commands.admin_mcp.api_get", return_value=_sources_list_resp()),
            patch("cli.commands.admin_mcp.api_post", return_value=_resp(200, body)),
        ):
            result = runner.invoke(app, ["admin", "mcp", "source", "oauth-register", "src_abc123", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == body

    def test_discovery_failure_exits_nonzero(self):
        with (
            patch("cli.commands.admin_mcp.api_get", return_value=_sources_list_resp()),
            patch(
                "cli.commands.admin_mcp.api_post",
                return_value=_resp(502, {"detail": "oauth_discovery_failed: OAuthDiscoveryError: no S256"}),
            ),
        ):
            result = runner.invoke(app, ["admin", "mcp", "source", "oauth-register", "src_abc123"])
        assert result.exit_code == 1
        assert "oauth_discovery_failed" in result.output


class TestOAuthClient:
    def test_success_with_secret_via_option(self):
        with (
            patch("cli.commands.admin_mcp.api_get", return_value=_sources_list_resp()),
            patch(
                "cli.commands.admin_mcp.api_put", return_value=_resp(200, {"client_id": "manual-client"})
            ) as mock_put,
        ):
            result = runner.invoke(
                app,
                [
                    "admin",
                    "mcp",
                    "source",
                    "oauth-client",
                    "src_abc123",
                    "--client-id",
                    "manual-client",
                    "--authorization-endpoint",
                    "https://as.example.com/authorize",
                    "--token-endpoint",
                    "https://as.example.com/token",
                    "--client-secret",
                    "s3cr3t",
                ],
            )
        assert result.exit_code == 0, result.output
        assert "manual-client" in result.output
        mock_put.assert_called_once_with(
            "/api/admin/mcp-sources/src_abc123/oauth/client",
            json={
                "client_id": "manual-client",
                "authorization_endpoint": "https://as.example.com/authorize",
                "token_endpoint": "https://as.example.com/token",
                "client_secret": "s3cr3t",
            },
        )

    def test_public_client_clears_any_secret_on_file(self):
        """--public-client must send an explicit "" — the endpoint reads an
        OMITTED client_secret as "keep whatever is stored", so omitting it
        would leave an existing confidential secret in place and Agnes would
        keep sending Basic auth (Devin Review on #1124)."""
        with (
            patch("cli.commands.admin_mcp.api_get", return_value=_sources_list_resp()),
            patch("cli.commands.admin_mcp.api_put", return_value=_resp(200, {"client_id": "pub-client"})) as mock_put,
        ):
            result = runner.invoke(
                app,
                [
                    "admin",
                    "mcp",
                    "source",
                    "oauth-client",
                    "src_abc123",
                    "--client-id",
                    "pub-client",
                    "--authorization-endpoint",
                    "https://as.example.com/authorize",
                    "--token-endpoint",
                    "https://as.example.com/token",
                    "--public-client",
                ],
            )
        assert result.exit_code == 0, result.output
        _, kwargs = mock_put.call_args
        assert kwargs["json"]["client_secret"] == ""

    def test_ssrf_rejection_exits_nonzero(self):
        with (
            patch("cli.commands.admin_mcp.api_get", return_value=_sources_list_resp()),
            patch(
                "cli.commands.admin_mcp.api_put",
                return_value=_resp(
                    400, {"detail": "token_endpoint failed SSRF/https validation: address_in_blocked_range"}
                ),
            ),
        ):
            result = runner.invoke(
                app,
                [
                    "admin",
                    "mcp",
                    "source",
                    "oauth-client",
                    "src_abc123",
                    "--client-id",
                    "c1",
                    "--authorization-endpoint",
                    "https://as.example.com/authorize",
                    "--token-endpoint",
                    "http://127.0.0.1/token",
                    "--public-client",
                ],
            )
        assert result.exit_code == 1
        assert "SSRF" in result.output
