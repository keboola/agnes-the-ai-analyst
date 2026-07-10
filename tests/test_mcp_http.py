"""Tests for Agnes HTTP MCP server (app/api/mcp_http.py).

Verifies:
- Auth middleware: 401 without token, 401 with invalid token, pass with valid PAT
- SSE endpoint starts and emits an event stream
- Each tool makes the expected self-calls and returns the right shape
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── helpers ─────────────────────────────────────────────────────────────────────


def _run(coro):
    """Run a coroutine synchronously (no pytest-asyncio dependency)."""
    return asyncio.run(coro)


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _mock_resp(data: Any, status: int = 200) -> MagicMock:
    """Build a mock httpx response."""
    r = MagicMock()
    r.status_code = status
    r.json.return_value = data
    r.raise_for_status = MagicMock()
    return r


def _import_mod():
    pytest.importorskip("mcp", reason="mcp package not installed")
    import app.api.mcp_http as mod

    return mod


# ── auth middleware ─────────────────────────────────────────────────────────────


class TestAuthMiddleware:
    def test_no_token_returns_401(self, seeded_app):
        r = seeded_app["client"].get("/api/mcp/sse")
        assert r.status_code == 401

    def test_bad_token_returns_401(self, seeded_app):
        r = seeded_app["client"].get("/api/mcp/sse", headers={"Authorization": "Bearer not-a-real-jwt"})
        assert r.status_code == 401

    def test_valid_token_passes_through_to_mcp(self, seeded_app):
        """_AuthMiddleware calls the underlying ASGI app when the token is valid."""
        import asyncio
        from app.api.mcp_http import _AuthMiddleware

        tok = seeded_app["analyst_token"]
        reached = []

        async def _inner_app(scope, receive, send):
            reached.append(True)

        middleware = _AuthMiddleware(_inner_app)
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/mcp/sse",
            "query_string": b"",
            "headers": [(b"authorization", f"Bearer {tok}".encode())],
        }
        asyncio.run(middleware(scope, None, None))
        assert reached, "Middleware did not call inner app with valid token"

    def test_query_param_token_passes_through(self, seeded_app):
        """?token= fallback also reaches the inner app when valid."""
        import asyncio
        from app.api.mcp_http import _AuthMiddleware

        tok = seeded_app["analyst_token"]
        reached = []

        async def _inner_app(scope, receive, send):
            reached.append(True)

        middleware = _AuthMiddleware(_inner_app)
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/mcp/sse",
            "query_string": f"token={tok}".encode(),
            "headers": [],
        }
        asyncio.run(middleware(scope, None, None))
        assert reached, "?token= param did not reach inner app"


# ── tool registration ────────────────────────────────────────────────────────────


class TestToolRegistration:
    def test_exact_server_side_tool_set(self):
        # Reload to a pristine module: the static @mcp.tool() set only. Dynamic
        # passthrough tools are registered onto the module-level ``mcp`` singleton
        # by ``create_app()`` (via register_passthrough_tools), so a prior test
        # that built the app would otherwise pollute this set. Reloading re-runs
        # the module body (fresh FastMCP + static decorators, no passthrough).
        import importlib

        import app.api.mcp_http as mod

        mod = importlib.reload(mod)
        tools = {t.name for t in mod.mcp._tool_manager.list_tools()}
        assert tools == {
            "server_info",
            "catalog",
            "schema",
            "describe",
            "query",
            "skills",
            # Triple-surface coverage for /documentation/api — agents read
            # the curated REST guide without leaving the chat. See
            # tests/test_documentation_api_triple_surface.py for the policy.
            "documentation_api",
            # Stack discovery + subscription (issue #621) — an analyst's
            # Claude can browse available resources and subscribe without
            # leaving the chat.
            "stack_browse",
            "stack_subscribe",
            "stack_unsubscribe",
            # Store thumbs up/down ratings (issue #398) — an analyst's Claude
            # can rate a store entity without leaving the chat. See
            # tests/test_documentation_api_triple_surface.py for the policy.
            "store_rate",
            # Owner-facing review-pipeline status — pairs with
            # `agnes store status` and GET /api/store/entities/{id}/status.
            "store_status",
            # Collections read surfaces (Slice 2) — list collections and read
            # one collection's files. Upload/delete are CLI-only (multipart /
            # mutation). See tests/test_documentation_api_triple_surface.py.
            "collections_list",
            "collection_get",
            "collections_search",
            # Re-run ingestion for one stuck file (needs_review/rejected) —
            # status-honesty follow-up (spec 2026-07-08). Triple-surface with
            # POST /api/collections/{cid}/files/{fid}/reingest +
            # `agnes collections reingest`.
            "collections_reingest",
            # Config-surface introspection — an operator's Claude reads this
            # instance's live configurable surface (knobs + sources, registered
            # IWT, marketplaces, infra_repo_url). Triple-surface with
            # GET /api/admin/config-surface + `agnes admin config-surface`.
            "admin_config_surface",
            # Multi-project Keboola: list named source connections (#731).
            # Triple-surface with GET /api/admin/source-connections +
            # `agnes admin connection list`.
            "admin_source_connections_list",
            # Contributed-skill triple-surface — admin can list, publish, and
            # delete skills in the Agnes Contributed marketplace without leaving
            # the chat. Mirrors REST + `agnes admin skill` CLI surface.
            "list_contributed_skills",
            "contribute_skill",
            "delete_contributed_skill",
            # Web chat composer slash-menu catalog (issue #780). Triple-surface
            # with GET /api/chat/skills + `agnes chat skills`.
            "chat_skills",
        }

    def test_no_client_only_tools(self):
        """query_local and pull require a local analyst filesystem — excluded here."""
        mod = _import_mod()
        tools = {t.name for t in mod.mcp._tool_manager.list_tools()}
        assert "query_local" not in tools
        assert "pull" not in tools


# ── catalog tool ────────────────────────────────────────────────────────────────


class TestCatalogTool:
    def test_returns_table_list(self):
        mod = _import_mod()
        data = {"tables": [{"id": "orders", "name": "Orders", "query_mode": "local"}]}

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            MC.return_value.__aenter__.return_value.get = AsyncMock(return_value=_mock_resp(data))
            result = _run(mod.catalog())

        assert result["tables"][0]["id"] == "orders"

    def test_url_contains_v2_catalog(self):
        mod = _import_mod()

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_get = AsyncMock(return_value=_mock_resp({}))
            MC.return_value.__aenter__.return_value.get = mock_get
            _run(mod.catalog())

        called_url = mock_get.call_args[0][0]
        assert "/api/v2/catalog" in called_url


# ── schema tool ─────────────────────────────────────────────────────────────────


class TestSchemaTool:
    def test_passes_table_id_in_url(self):
        mod = _import_mod()
        data = {"table_id": "orders", "columns": [{"name": "id", "type": "VARCHAR"}]}

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_get = AsyncMock(return_value=_mock_resp(data))
            MC.return_value.__aenter__.return_value.get = mock_get
            result = _run(mod.schema("orders"))

        assert result["columns"][0]["name"] == "id"
        assert "orders" in mock_get.call_args[0][0]


# ── describe tool ───────────────────────────────────────────────────────────────


class TestDescribeTool:
    def test_returns_schema_and_sample(self):
        mod = _import_mod()
        schema_data = {"columns": []}
        sample_data = {"rows": []}

        def _side(url, **kw):
            return _mock_resp(schema_data if "schema" in url else sample_data)

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            MC.return_value.__aenter__.return_value.get = AsyncMock(side_effect=_side)
            result = _run(mod.describe("orders"))

        assert "schema" in result
        assert "sample" in result

    def test_clamps_rows_to_50(self):
        mod = _import_mod()
        calls = []

        def _side(url, **kw):
            calls.append((url, kw))
            return _mock_resp({})

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            MC.return_value.__aenter__.return_value.get = AsyncMock(side_effect=_side)
            _run(mod.describe("orders", rows=9999))

        sample = next((c for c in calls if "sample" in c[0]), None)
        assert sample is not None
        assert sample[1].get("params", {}).get("n", 0) <= 50


# ── query tool ──────────────────────────────────────────────────────────────────


class TestQueryTool:
    def test_posts_sql_and_limit(self):
        mod = _import_mod()
        resp_data = {"columns": ["x"], "rows": [[1]], "truncated": False}

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_post = AsyncMock(return_value=_mock_resp(resp_data))
            MC.return_value.__aenter__.return_value.post = mock_post
            result = _run(mod.query("SELECT x FROM t", limit=5))

        assert result["columns"] == ["x"]
        posted = mock_post.call_args[1]["json"]
        assert posted["sql"] == "SELECT x FROM t"
        assert posted["limit"] == 5

    def test_default_limit_is_1000(self):
        mod = _import_mod()

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_post = AsyncMock(return_value=_mock_resp({}))
            MC.return_value.__aenter__.return_value.post = mock_post
            _run(mod.query("SELECT 1"))

        assert mock_post.call_args[1]["json"]["limit"] == 1000


# ── stack tools (issue #621) ──────────────────────────────────────────────────────


class TestStackTools:
    def test_browse_passes_type_param(self):
        mod = _import_mod()
        data = {"items": [{"id": "pkg_a", "name": "A", "in_stack": False}]}

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_get = AsyncMock(return_value=_mock_resp(data))
            MC.return_value.__aenter__.return_value.get = mock_get
            result = _run(mod.stack_browse("data_package"))

        assert result["items"][0]["id"] == "pkg_a"
        called_url = mock_get.call_args[0][0]
        assert "/api/stack/browse" in called_url
        assert mock_get.call_args[1]["params"] == {"type": "data_package"}

    def test_subscribe_posts_payload_and_adds_hint(self):
        mod = _import_mod()

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_post = AsyncMock(return_value=_mock_resp({"subscribed": True}))
            MC.return_value.__aenter__.return_value.post = mock_post
            result = _run(mod.stack_subscribe("data_package", "pkg_a"))

        assert result["subscribed"] is True
        # Post-subscribe hint tells the model what to run next.
        assert "agnes pull" in result["next_step"]
        called_url = mock_post.call_args[0][0]
        assert "/api/stack/subscribe" in called_url
        posted = mock_post.call_args[1]["json"]
        assert posted == {"resource_type": "data_package", "resource_id": "pkg_a"}

    def test_unsubscribe_calls_subscription_endpoint(self):
        mod = _import_mod()

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_delete = AsyncMock(return_value=_mock_resp({}, status=204))
            MC.return_value.__aenter__.return_value.delete = mock_delete
            result = _run(mod.stack_unsubscribe("data_package", "pkg_a"))

        assert result["unsubscribed"] is True
        called_url = mock_delete.call_args[0][0]
        assert "/api/stack/subscription/data_package/pkg_a" in called_url


# ── server_info tool ────────────────────────────────────────────────────────────


class TestServerInfoTool:
    def test_returns_health_and_email(self):
        mod = _import_mod()

        def _side(url, **kw):
            if "/api/health" in url:
                return _mock_resp({"status": "ok"})
            if "/api/me" in url:
                return _mock_resp({"email": "analyst@test.com"})
            return _mock_resp({})

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            MC.return_value.__aenter__.return_value.get = AsyncMock(side_effect=_side)
            result = _run(mod.server_info())

        assert result["authenticated"] is True
        assert result["health"] == {"status": "ok"}
        assert result["user_email"] == "analyst@test.com"

    def test_health_unreachable_doesnt_crash(self):
        mod = _import_mod()

        def _side(url, **kw):
            if "/api/health" in url:
                raise ConnectionError("refused")
            return _mock_resp({"email": "x@y.com"})

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            MC.return_value.__aenter__.return_value.get = AsyncMock(side_effect=_side)
            result = _run(mod.server_info())

        assert result["health"] == "unreachable"
        assert result["authenticated"] is True
