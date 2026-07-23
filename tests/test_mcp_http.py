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

    def test_sets_current_user_id_during_request(self, seeded_app):
        """_AuthMiddleware exposes the resolved caller id via _current_user_id
        for the duration of the request (read by passthrough closures so a
        per_user source forwards the caller's own credential), and resets it
        after."""
        import asyncio
        from app.api.mcp_http import _AuthMiddleware, _current_user_id

        tok = seeded_app["analyst_token"]
        seen = {}

        async def _inner_app(scope, receive, send):
            seen["uid"] = _current_user_id.get()

        middleware = _AuthMiddleware(_inner_app)
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/mcp/sse",
            "query_string": b"",
            "headers": [(b"authorization", f"Bearer {tok}".encode())],
        }
        asyncio.run(middleware(scope, None, None))
        assert seen["uid"] == "analyst1"
        # Reset after the request — no leak into the next context.
        assert _current_user_id.get() == ""

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
            # Full agent/skill lifecycle parity (REST × CLI × MCP) — discover,
            # inspect, install/remove marketplace items, and edit/delete owned
            # store entities from the chat. Pairs with `agnes marketplace
            # search/detail/add/remove` and `agnes store update/delete`.
            "marketplace_search",
            "marketplace_detail",
            "marketplace_add",
            "marketplace_remove",
            "store_update",
            "store_delete",
            # Collections read surfaces (Slice 2) — list collections and read
            # one collection's files. Upload/delete are CLI-only (multipart /
            # mutation). See tests/test_documentation_api_triple_surface.py.
            "collections_list",
            "collection_get",
            "collections_search",
            # Unified knowledge search (K2, #797) — one query across
            # Collections chunks + knowledge items + table catalog cards.
            # Triple-surface with GET /api/knowledge/search + `agnes search`.
            "knowledge_search",
            # Keboola glossary import (2026-07-17 design) — relevance-ranked
            # (BM25) search over Keboola-imported business-term definitions.
            # Triple-surface with GET /api/glossary/search + `agnes glossary
            # search`.
            "glossary_search",
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
            # Job management for scheduler — list, get, enqueue tasks.
            # Triple-surface with GET /api/jobs + GET /api/jobs/{job_id} +
            # POST /api/jobs + `agnes admin jobs`.
            "admin_jobs_list",
            "admin_job_get",
            "admin_job_enqueue",
            # DuckLake analytics-backend migration (wave-2G Task 6). Triple-
            # surface with POST /api/admin/analytics/migrate + `agnes admin
            # analytics migrate`.
            "admin_analytics_migrate",
            # Contributed-skill triple-surface — admin can list, publish, and
            # delete skills in the Agnes Contributed marketplace without leaving
            # the chat. Mirrors REST + `agnes admin skill` CLI surface.
            "list_contributed_skills",
            "contribute_skill",
            "delete_contributed_skill",
            # Web chat composer slash-menu catalog (issue #780). Triple-surface
            # with GET /api/chat/skills + `agnes chat skills`.
            "chat_skills",
            # Chat composer "+" upload (#966) — upload a file into the chat
            # workspace without leaving the conversation. Triple-surface with
            # POST /api/chat/uploads + `agnes chat upload`. The server-hosted
            # variant refuses by-path reads (client-side stdio does the actual
            # file read); see app/api/mcp/foundation_tools.py.
            "chat_upload_file",
            # Markdown-first skill publish (studio Skill Builder, issue #688).
            # Triple-surface with POST /api/store/entities/from-markdown +
            # `agnes store publish-md`.
            "store_publish_markdown",
            # Maintained digests (K4, #799) — admin CRUD over LLM-regenerated
            # digest documents. Triple-surface with the
            # /api/admin/knowledge-digests* REST surface + `agnes admin digest`.
            "admin_knowledge_digests_list",
            "admin_knowledge_digest_get",
            "admin_knowledge_digest_create",
            "admin_knowledge_digest_update",
            "admin_knowledge_digest_delete",
            # Skill-linter admin moderation surface (v89, #687) — findings
            # list, manual full-corpus audit, per-finding dismiss. Triple-
            # surface with /api/admin/store/lint-* + `agnes admin store lint-*`.
            "admin_store_lint_findings",
            "admin_store_lint_audit",
            "admin_store_lint_dismiss",
            # Per-user MCP credential connectivity check — an analyst's Claude
            # verifies their own stored token. Triple-surface with POST
            # /api/mcp/sources/{id}/my-secret/test + `agnes mcp my-secret test`.
            "my_secret_test",
            # Hosted data apps (data-apps platform plan, Task 11) — list/get
            # for any authenticated user with view access, deploy/logs for
            # app owner or Admin. Triple-surface with /api/data-apps* +
            # `agnes app list/show/deploy/logs`.
            "data_apps_list",
            "data_app_get",
            "data_app_deploy",
            "data_app_logs",
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


class TestStorePublishMarkdownTool:
    def test_posts_full_payload(self):
        mod = _import_mod()
        data = {"id": "ent_1", "name": "my-skill", "version": 1, "visibility_status": "pending"}

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_post = AsyncMock(return_value=_mock_resp(data, status=201))
            MC.return_value.__aenter__.return_value.post = mock_post
            result = _run(
                mod.store_publish_markdown(
                    "my-skill",
                    "# My Skill\n\nBody text.",
                    description="Use when doing X",
                    category="analytics",
                )
            )

        assert result == data
        called_url = mock_post.call_args[0][0]
        assert "/api/store/entities/from-markdown" in called_url
        posted = mock_post.call_args[1]["json"]
        assert posted == {
            "type": "skill",
            "name": "my-skill",
            "skill_md": "# My Skill\n\nBody text.",
            "description": "Use when doing X",
            "category": "analytics",
        }

    def test_omits_optional_fields_when_absent(self):
        mod = _import_mod()
        data = {"id": "ent_2", "name": "bare-skill", "version": 1, "visibility_status": "approved"}

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_post = AsyncMock(return_value=_mock_resp(data, status=201))
            MC.return_value.__aenter__.return_value.post = mock_post
            result = _run(mod.store_publish_markdown("bare-skill", "# Bare Skill"))

        assert result == data
        posted = mock_post.call_args[1]["json"]
        assert posted == {"type": "skill", "name": "bare-skill", "skill_md": "# Bare Skill"}

    def test_accepts_agent_type(self):
        """type="agent" (#865) — MCP callers can now publish an agent, not just a skill."""
        mod = _import_mod()
        data = {"id": "ent_3", "name": "my-agent", "version": 1, "visibility_status": "pending"}

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_post = AsyncMock(return_value=_mock_resp(data, status=201))
            MC.return_value.__aenter__.return_value.post = mock_post
            result = _run(mod.store_publish_markdown("my-agent", "# My Agent\n\nBody text.", type="agent"))

        assert result == data
        posted = mock_post.call_args[1]["json"]
        assert posted == {"type": "agent", "name": "my-agent", "skill_md": "# My Agent\n\nBody text."}


# ── marketplace lifecycle tools (agent-management triple-surface parity) ────────


class TestMarketplaceLifecycleTools:
    """MCP mirrors of `agnes marketplace search/detail/add/remove` and
    `agnes store update/delete` — full agent/skill lifecycle without a CLI."""

    def test_search_defaults_to_both_tabs_with_labels(self):
        mod = _import_mod()
        curated_item = {"id": "eng/reviewer", "source": "curated", "type": "agent"}
        flea_item = {"id": "ent_9", "source": "flea", "type": "agent"}

        def _side(url, **kw):
            tab = kw["params"]["tab"]
            return _mock_resp({"items": [curated_item if tab == "curated" else flea_item]})

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_get = AsyncMock(side_effect=_side)
            MC.return_value.__aenter__.return_value.get = mock_get
            result = _run(mod.marketplace_search(query="review", type="agent"))

        assert result == {"items": [curated_item, flea_item], "total": 2}
        assert mock_get.call_count == 2
        tabs = [c[1]["params"]["tab"] for c in mock_get.call_args_list]
        assert tabs == ["curated", "flea"]
        for c in mock_get.call_args_list:
            assert "/api/marketplace/items" in c[0][0]
            assert c[1]["params"]["q"] == "review"
            assert c[1]["params"]["type"] == "agent"

    def test_search_single_source_hits_one_tab(self):
        mod = _import_mod()

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_get = AsyncMock(return_value=_mock_resp({"items": []}))
            MC.return_value.__aenter__.return_value.get = mock_get
            result = _run(mod.marketplace_search(source="flea"))

        assert result == {"items": [], "total": 0}
        assert mock_get.call_count == 1
        assert mock_get.call_args[1]["params"]["tab"] == "flea"

    def test_detail_parses_curated_and_flea_ids(self):
        mod = _import_mod()

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_get = AsyncMock(return_value=_mock_resp({"type": "agent"}))
            MC.return_value.__aenter__.return_value.get = mock_get
            _run(mod.marketplace_detail("eng/reviewer"))
            _run(mod.marketplace_detail("ent_9"))

        urls = [c[0][0] for c in mock_get.call_args_list]
        assert "/api/marketplace/curated/eng/reviewer" in urls[0]
        assert "/api/marketplace/flea/ent_9/detail" in urls[1]

    def test_tools_accept_tab_prefixed_search_ids(self):
        """Ids exactly as `/api/marketplace/items` prints them — `curated-<mid>/<plugin>`,
        `flea-<uuid>` — must route to the bare-form REST paths (Devin Review on #982)."""
        mod = _import_mod()

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_get = AsyncMock(return_value=_mock_resp({"type": "agent"}))
            mock_post = AsyncMock(return_value=_mock_resp({"installed": True}))
            MC.return_value.__aenter__.return_value.get = mock_get
            MC.return_value.__aenter__.return_value.post = mock_post
            _run(mod.marketplace_detail("curated-eng/reviewer"))
            _run(mod.marketplace_detail("flea-ent_9"))
            _run(mod.marketplace_add("curated-eng/reviewer"))
            _run(mod.marketplace_add("flea-ent_9"))

        get_urls = [c[0][0] for c in mock_get.call_args_list]
        assert "/api/marketplace/curated/eng/reviewer" in get_urls[0]
        assert "/api/marketplace/flea/ent_9/detail" in get_urls[1]
        post_urls = [c[0][0] for c in mock_post.call_args_list]
        assert "/api/marketplace/curated/eng/reviewer/install" in post_urls[0]
        assert "/api/store/entities/ent_9/install" in post_urls[1]

    def test_add_routes_by_id_shape_and_hints_refresh(self):
        mod = _import_mod()

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_post = AsyncMock(return_value=_mock_resp({"installed": True}))
            MC.return_value.__aenter__.return_value.post = mock_post
            flea = _run(mod.marketplace_add("ent_9"))
            curated = _run(mod.marketplace_add("eng/reviewer"))

        assert flea["installed"] is True and "update-agnes-plugins" in flea["next_step"]
        assert curated["installed"] is True
        urls = [c[0][0] for c in mock_post.call_args_list]
        assert "/api/store/entities/ent_9/install" in urls[0]
        assert "/api/marketplace/curated/eng/reviewer/install" in urls[1]

    def test_remove_routes_by_id_shape(self):
        mod = _import_mod()

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_delete = AsyncMock(return_value=_mock_resp({}, status=204))
            MC.return_value.__aenter__.return_value.delete = mock_delete
            flea = _run(mod.marketplace_remove("ent_9"))
            curated = _run(mod.marketplace_remove("eng/reviewer"))

        assert flea["removed"] is True and curated["removed"] is True
        urls = [c[0][0] for c in mock_delete.call_args_list]
        assert "/api/store/entities/ent_9/install" in urls[0]
        assert "/api/marketplace/curated/eng/reviewer/install" in urls[1]

    def test_store_update_sends_only_provided_fields(self):
        mod = _import_mod()
        data = {"id": "ent_9", "version": 1}

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_put = AsyncMock(return_value=_mock_resp(data))
            MC.return_value.__aenter__.return_value.put = mock_put
            result = _run(mod.store_update("ent_9", description="Better trigger line"))

        assert result == data
        assert "/api/store/entities/ent_9" in mock_put.call_args[0][0]
        assert mock_put.call_args[1]["data"] == {"description": "Better trigger line"}

    def test_store_update_refuses_empty_edit_without_http_call(self):
        mod = _import_mod()

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_put = AsyncMock()
            MC.return_value.__aenter__.return_value.put = mock_put
            result = _run(mod.store_update("ent_9"))

        assert result["error"] == "nothing_to_update"
        mock_put.assert_not_called()

    def test_store_delete_calls_entity_endpoint(self):
        mod = _import_mod()

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_delete = AsyncMock(return_value=_mock_resp({}, status=204))
            MC.return_value.__aenter__.return_value.delete = mock_delete
            result = _run(mod.store_delete("ent_9"))

        assert result == {"deleted": True, "entity_id": "ent_9"}
        assert "/api/store/entities/ent_9" in mock_delete.call_args[0][0]


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


# ── my_secret_test tool ──────────────────────────────────────────────────────


class TestMySecretTestTool:
    def test_success_passthrough(self):
        mod = _import_mod()
        data = {"ok": True, "tool_count": 3, "message": "ok"}

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            MC.return_value.__aenter__.return_value.post = AsyncMock(return_value=_mock_resp(data))
            result = _run(mod.my_secret_test("src_test"))

        assert result == data

    def test_403_remedy_reaches_the_model_instead_of_raising(self):
        """raise_for_status() would discard the response body and surface only
        a generic 'Forbidden' — the connect-here remedy in `detail` must reach
        the caller instead (audit finding on PR #919)."""
        mod = _import_mod()
        remedy = "not connected — visit /me/connections?source=src_test to add your token"

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            resp = _mock_resp({"detail": remedy}, status=403)
            MC.return_value.__aenter__.return_value.post = AsyncMock(return_value=resp)
            result = _run(mod.my_secret_test("src_test"))

        assert result == {"ok": False, "tool_count": None, "message": remedy}
        resp.raise_for_status.assert_not_called()

    def test_other_4xx_also_returns_detail_without_raising(self):
        mod = _import_mod()

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            resp = _mock_resp({"detail": "not_granted"}, status=429)
            MC.return_value.__aenter__.return_value.post = AsyncMock(return_value=resp)
            result = _run(mod.my_secret_test("src_test"))

        assert result["ok"] is False
        assert result["message"] == "not_granted"

    def test_5xx_still_raises(self):
        mod = _import_mod()

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            resp = _mock_resp({}, status=500)
            resp.raise_for_status.side_effect = RuntimeError("boom")
            MC.return_value.__aenter__.return_value.post = AsyncMock(return_value=resp)
            with pytest.raises(RuntimeError):
                _run(mod.my_secret_test("src_test"))
