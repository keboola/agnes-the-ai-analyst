"""Shared foundation-tool registry for Agnes MCP servers.

The SSE MCP server (``app/api/mcp_http.py``) and the Streamable-HTTP MCP
server (``app/api/mcp_streamable.py``) each expose the same 24 server-side
tools — catalog/schema/query, Collections, knowledge search, skills, stack
subscription, store, and admin surfaces — but authenticate callers through
different mechanisms (PAT context var vs. OAuth access token). Historically
each transport hand-duplicated the ``@mcp.tool()`` definitions, and the
duplicates drifted: the streamable transport silently lost 18 of 24 tools.

This module is the single source of truth. ``register_foundation_tools``
registers every tool onto a caller-supplied ``FastMCP`` instance, parameterized
by a ``base_url`` (for self-calls back into the Agnes REST API) and a
``headers_fn`` callable that produces the ``Authorization`` header for the
current request context — the only two things that differ between the two
transports.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Literal

import httpx
from mcp.server.fastmcp import FastMCP

from src.mcp_tooling import ensure_output_size, progressive_tool


def _split_marketplace_id(item_id: str) -> tuple[str, str, str]:
    """Split a marketplace item id into ``(source, part1, part2)``.

    ``GET /api/marketplace/items`` prefixes row ids with their tab
    (``curated-<marketplace_id>/<plugin_name>``, ``flea-<entity_uuid>``), while
    the REST detail/install paths take the bare forms. Accept both, so an id
    can be passed straight from ``marketplace_search`` output — same
    normalization as the CLI's ``_parse_id`` in ``cli/commands/marketplace.py``.
    """
    if "/" in item_id:
        head, plugin = item_id.split("/", 1)
        return "curated", head.removeprefix("curated-"), plugin
    return "flea", item_id.removeprefix("flea-"), ""


FOUNDATION_TOOL_NAMES: tuple[str, ...] = (
    "server_info",
    "catalog",
    "collections_list",
    "collection_get",
    "collections_search",
    "knowledge_search",
    "glossary_search",
    "collections_reingest",
    "schema",
    "describe",
    "query",
    "skills",
    "chat_skills",
    "stack_browse",
    "stack_subscribe",
    "stack_unsubscribe",
    "store_rate",
    "store_status",
    "store_publish_markdown",
    # Full agent/skill lifecycle parity (REST × CLI × MCP): discover, inspect,
    # install/remove, edit, delete — an agent can manage its own store
    # entities and stack without leaving the chat. Binary paths (ZIP upload,
    # photo, bundle download) stay CLI-only.
    "marketplace_search",
    "marketplace_detail",
    "marketplace_add",
    "marketplace_remove",
    "store_update",
    "store_delete",
    "admin_store_lint_findings",
    "admin_store_lint_audit",
    "admin_store_lint_dismiss",
    "documentation_api",
    # On-demand full tool docs — wire descriptions carry only the first
    # docstring paragraph; this returns the rest. MCP-surface-only (meta-tool
    # over the MCP tool registry itself; no REST/CLI analogue applies).
    "tool_docs",
    "list_contributed_skills",
    "contribute_skill",
    "delete_contributed_skill",
    "admin_config_surface",
    "admin_source_connections_list",
    # Maintained digests (K4, #799) — admin CRUD, triple-surface with
    # /api/admin/knowledge-digests* + `agnes admin digest`.
    "admin_knowledge_digests_list",
    "admin_knowledge_digest_get",
    "admin_knowledge_digest_create",
    "admin_knowledge_digest_update",
    "admin_knowledge_digest_delete",
    # Per-user MCP credential connectivity check (triple-surface with
    # /api/mcp/sources/{id}/my-secret/test + `agnes mcp my-secret test`).
    "my_secret_test",
    # Wave-2B job queue (Task 5) — admin CRUD-lite, triple-surface with
    # /api/jobs* + `agnes admin jobs`.
    "admin_jobs_list",
    "admin_job_get",
    "admin_job_enqueue",
    # DuckLake analytics-backend migration (wave-2G Task 6), triple-surface
    # with /api/admin/analytics/migrate + `agnes admin analytics migrate`.
    "admin_analytics_migrate",
    # Agent profiles (agent-api V1a, Task 12) — triple-surface with
    # /api/v1/agents + `agnes agent list` (management, session-token only)
    # and /api/v1/agents/{slug}/responses + `agnes agent ask` (runtime,
    # sync-only here — background mode + job polling has no MCP tool).
    "agent_list",
    "agent_ask",
    # Agent-as-API monthly usage (agent-api V1b, Task 8) — triple-surface
    # with GET /api/v1/agents/{slug}/usage + `agnes agent usage`.
    "agent_usage",
    # Hosted data apps (data-apps platform plan, Task 11) — triple-surface
    # with /api/data-apps* + `agnes app list/show/deploy/logs`.
    "data_apps_list",
    "data_app_get",
    "data_app_deploy",
    "data_app_logs",
    # Wave 3B draft-iteration model (Task 8) — create/delete a draft copy of
    # a prod app on an iteration branch, and mint a fresh git push credential.
    # Triple-surface with /api/data-apps/{slug}/drafts* + /git-credential and
    # `agnes app draft create/delete` + `agnes app git-credential`.
    "data_app_create_draft",
    "data_app_delete_draft",
    "data_app_git_credential",
    # Linked (externally-hosted) data apps (v108) — set the admin description
    # override on a managed/linked app. Triple-surface with
    # PATCH /api/data-apps/{slug} + `agnes app set-description`.
    "data_app_set_description",
    # Wave 3C in-chat preview loop (Task 4/5) — chat-surface-ONLY render
    # directives for the split-pane preview iframe (spec §7/§9): no REST/CLI
    # analogue exists or is planned (the fixed render-directive JSON these
    # return is the frontend contract, not a general-purpose API response).
    # `agnes_data_app_preview`'s live-URL call mints a short-TTL
    # `data-app-preview:<slug>` scoped grant via
    # POST /api/data-apps/{slug}/preview-grant (_EXEMPT in
    # tests/test_documentation_api_triple_surface.py).
    "agnes_data_app_preview",
    "agnes_data_app_refresh",
    "agnes_data_app_close",
    "agnes_data_app_credentials",
)


# The hosted-data-app tool family — must be exposed on the CLI **stdio**
# `agnes mcp` surface too, not only the HTTP foundation transports, because the
# in-chat authoring agent connects through the stdio server
# (`app/chat/runner.py::_agnes_mcp_servers()` spawns `agnes mcp` =
# `cli/mcp/server.py`). Wave-3C originally registered these only here (the HTTP
# foundation surface) — the stdio server hand-registers a curated analyst
# subset and never got the family, so the chat agent could never emit the
# `data_app_preview` render frame and the in-chat preview pane was inert. A
# guard in tests/test_mcp_tool_parity.py asserts the stdio server exposes every
# name below, and that this tuple stays a subset of FOUNDATION_TOOL_NAMES.
DATA_APP_TOOL_NAMES: tuple[str, ...] = (
    "data_apps_list",
    "data_app_get",
    "data_app_deploy",
    "data_app_logs",
    "data_app_create_draft",
    "data_app_delete_draft",
    "data_app_git_credential",
    "data_app_set_description",
    "agnes_data_app_preview",
    "agnes_data_app_refresh",
    "agnes_data_app_close",
    "agnes_data_app_credentials",
)


# Full docstrings by tool name — the wire description carries only the first
# paragraph; the rest is served on demand by the `tool_docs` tool.
TOOL_DOCS: dict[str, str] = {}


def register_foundation_tools(
    mcp: FastMCP,
    *,
    base_url: str,
    headers_fn: Callable[[], dict[str, str]],
) -> list[str]:
    """Register all foundation tools onto ``mcp``. Returns the registered names.

    Args:
        mcp: The FastMCP instance to register tools onto.
        base_url: Internal base URL for self-calls into the Agnes REST API.
        headers_fn: Returns the ``Authorization`` header for the current
            request context (PAT context var for SSE, OAuth access token for
            the streamable transport).
    """
    tool = progressive_tool(mcp, TOOL_DOCS)

    @tool()
    async def server_info() -> dict:
        """Return Agnes server health and your account email.

        Useful as a quick connectivity check at the start of a session.
        """
        result: dict[str, Any] = {"authenticated": True}
        async with httpx.AsyncClient() as c:
            try:
                r = await c.get(f"{base_url}/api/health", timeout=5)
                if r.status_code == 200:
                    result["health"] = r.json()
            except Exception:
                result["health"] = "unreachable"
            try:
                r = await c.get(f"{base_url}/api/me", headers=headers_fn(), timeout=5)
                if r.status_code == 200:
                    result["user_email"] = r.json().get("email", "")
            except Exception:
                pass
        return result

    @tool()
    async def catalog() -> dict:
        """List all tables available to you (RBAC-filtered).

        Returns a dict with a ``tables`` list.  Each entry has:
        - ``id``         — use this in schema / describe / query calls
        - ``name``       — human-readable label
        - ``query_mode`` — local | remote | materialized
        - ``sql_flavor`` — duckdb or bigquery (affects SQL dialect in query)
        - ``rows``       — approximate row count (may be null)

        Always call this first so you know what data is available.
        """
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{base_url}/api/v2/catalog", headers=headers_fn(), timeout=30)
            r.raise_for_status()
            return r.json()

    @tool()
    async def collections_list() -> dict:
        """List the file Collections you can access (RBAC-filtered).

        A Collection is a user-uploaded set of files Agnes has indexed. Returns a
        dict with an ``items`` list; each entry has ``id``, ``name``,
        ``slug``, and file/table counts. Use ``collection_get`` for the files in
        one collection.
        """
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{base_url}/api/collections", headers=headers_fn(), timeout=30)
            r.raise_for_status()
            return r.json()

    @tool()
    async def collection_get(collection_id: str) -> dict:
        """Show one Collection's detail plus its files and per-file status.

        Args:
            collection_id: Collection id from ``collections_list`` (``col_...``).
        """
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{base_url}/api/collections/{collection_id}",
                headers=headers_fn(),
                timeout=30,
            )
            r.raise_for_status()
            return r.json()

    @tool()
    async def collections_search(query: str, k: int = 10, collection_id: str = "") -> dict:
        """Hybrid search across your accessible file Collections (RBAC-filtered).

        Returns ranked chunks with citations (``filename``, ``ordinal``, ``text``,
        ``score``). Optionally restrict to one collection via ``collection_id``.
        The response's ``retrieval`` field says how results were ranked:
        ``hybrid`` (lexical + semantic) or ``lexical_only`` — the degraded
        mode when the server has no embedding model installed.

        Args:
            query: Natural-language or keyword query.
            k: Max results (default 10).
            collection_id: Optional ``col_...`` id to restrict the search.
        """
        params: dict = {"q": query, "k": k}
        if collection_id:
            params["corpus_id"] = collection_id
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{base_url}/api/collections/search",
                headers=headers_fn(),
                params=params,
                timeout=60,
            )
            r.raise_for_status()
            return r.json()

    @tool()
    async def knowledge_search(query: str, k: int = 10) -> dict:
        """One query across documents, the knowledge base, and the data catalog.

        Fans out server-side over Collections chunks (hybrid lexical+vector),
        corporate-memory knowledge items (fulltext), and table catalog cards —
        all RBAC-filtered. Results are typed ``chunk | knowledge | table``;
        a ``table`` hit means structured data: pivot to SQL via the ``query``
        tool with the hit's ``table_id`` instead of reading text chunks.
        The response's ``retrieval`` field labels the chunk engine's mode:
        ``hybrid`` (lexical + semantic) or ``lexical_only`` — the degraded
        mode when the server has no embedding model installed.

        Args:
            query: Natural-language or keyword query.
            k: Max results (default 10).
        """
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{base_url}/api/knowledge/search",
                headers=headers_fn(),
                params={"q": query, "k": k},
                timeout=60,
            )
            r.raise_for_status()
            return r.json()

    @tool()
    async def glossary_search(query: str, k: int = 10) -> dict:
        """Search Keboola-imported business-term definitions (glossary).

        Relevance-ranked (BM25) search across term + definition, RBAC tier
        matches knowledge_search (any authenticated user). Use this to
        resolve business terminology (e.g. "what does MRR mean here?")
        before assuming a term's meaning.

        Args:
            query: Natural-language or keyword query.
            k: Max results (default 10).
        """
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{base_url}/api/glossary/search",
                headers=headers_fn(),
                params={"q": query, "limit": k},
                timeout=30,
            )
            r.raise_for_status()
            return r.json()

    @tool()
    async def collections_reingest(collection_id: str, file_id: str) -> dict:
        """Re-run ingestion for one file in a Collection (requires access to the collection).

        Use after the file or extraction config was fixed — e.g. a file stuck
        in ``needs_review`` (empty extraction) or ``rejected``. Returns the file
        row reset to ``pending``; ingestion runs server-side in the background.

        Args:
            collection_id: Collection id from ``collections_list`` (``col_...``).
            file_id: File id from ``collection_get`` (``cf_...``).
        """
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{base_url}/api/collections/{collection_id}/files/{file_id}/reingest",
                json={},
                headers=headers_fn(),
                timeout=30,
            )
            r.raise_for_status()
            return r.json()

    @tool()
    async def schema(table_id: str) -> dict:
        """Show column names, types, and SQL dialect hints for a table.

        Args:
            table_id: Table ID from the catalog (e.g. ``crm_accounts``).

        Returns column list with name, type, nullable, description plus
        sql_flavor and where_dialect_hints where relevant.
        """
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{base_url}/api/v2/schema/{table_id}", headers=headers_fn(), timeout=30)
            r.raise_for_status()
            return r.json()

    @tool()
    async def describe(table_id: str, rows: int = 5) -> dict:
        """Show schema plus sample rows for a table.

        Args:
            table_id: Table ID from the catalog.
            rows:     How many sample rows to return (default 5, max 50).

        Returns ``{"schema": {...}, "sample": {"table_id": ..., "rows": [...],
        "source": ...}}`` where ``sample.rows`` is a list of ``{column: value}``
        objects (empty when the table has no rows — there is no ``columns``
        key; column names come from ``schema.columns``).
        """
        rows = min(max(1, rows), 50)
        async with httpx.AsyncClient() as c:
            rs = await c.get(f"{base_url}/api/v2/schema/{table_id}", headers=headers_fn(), timeout=30)
            rs.raise_for_status()
            rm = await c.get(
                f"{base_url}/api/v2/sample/{table_id}",
                headers=headers_fn(),
                params={"n": rows},
                timeout=30,
            )
            rm.raise_for_status()
        return ensure_output_size(
            {"schema": rs.json(), "sample": rm.json()},
            "describe",
            hint="lower `rows` or select specific columns with the query tool",
        )

    @tool()
    async def query(sql: str, limit: int = 1000) -> dict:
        """Execute a SQL query against Agnes data.

        For local and materialized tables the query runs against the server-side
        DuckDB view.  For remote (BigQuery) tables it passes through to BigQuery.

        Args:
            sql:   SQL statement.  Use DuckDB dialect for local/materialized;
                   BigQuery dialect for remote tables (check sql_flavor in catalog).
            limit: Maximum rows to return (default 1000).

        Returns ``{"columns": [...], "rows": [[...], ...], "truncated": bool}``.
        """
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{base_url}/api/query",
                json={"sql": sql, "limit": limit},
                headers=headers_fn(),
                timeout=60,
            )
            r.raise_for_status()
            return ensure_output_size(r.json(), "query")

    @tool()
    async def skills() -> dict:
        """List all skills from marketplace plugins you are authorised to access.

        Returns a ``skills`` list.  Each entry has:
        - ``marketplace_id`` — marketplace slug
        - ``plugin_name``    — plugin directory name
        - ``skill_name``     — skill directory name (unique invocation key)
        - ``name``           — human-readable label
        - ``description``    — short description (may be null)
        - ``invocation``     — slash-command or invocation hint (may be null)
        - ``body``           — full SKILL.md text with frontmatter stripped

        Load a ``body`` into your context when you need to follow that skill's
        instructions.
        """
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{base_url}/api/v2/marketplace/skills",
                headers=headers_fn(),
                timeout=30,
            )
            r.raise_for_status()
            return r.json()

    @tool()
    async def chat_skills() -> dict:
        """List skills + slash commands invokable in your web chat sandbox.

        Unlike ``skills`` (every marketplace skill you're RBAC-granted, with full
        bodies), this mirrors what the web chat composer's slash menu shows:
        skills bundled into the chat sandbox's workspace template merged with
        your RBAC-filtered marketplace/store plugin skills (marketplace wins name
        clashes) — the same set ``app/chat/runner.py`` installs into a live
        session. Requires cloud chat to be enabled and granted to you.

        Returns ``{"skills": [{"name", "description", "source"}],
        "commands": [{"name", "description"}]}``. ``commands`` is currently
        always empty — no slash command is backend-recognized yet.
        """
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{base_url}/api/chat/skills",
                headers=headers_fn(),
                timeout=30,
            )
            r.raise_for_status()
            return r.json()

    @tool()
    async def stack_browse(resource_type: Literal["data_package", "memory_domain"]) -> dict:
        """List resources you could add to your stack (RBAC-granted candidates).

        Unlike ``catalog`` (which lists tables already in your stack), this is the
        discovery surface: every data package or memory domain your groups are
        granted, each annotated with an ``in_stack`` flag so you can tell what is
        already subscribed and what is still available to add.

        Args:
            resource_type: ``data_package`` or ``memory_domain``.

        Returns ``{"items": [{"id", "name", "description", "requirement",
        "in_stack", ...}]}``. Subscribe to an available item with
        ``stack_subscribe``.
        """
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{base_url}/api/stack/browse",
                headers=headers_fn(),
                params={"type": resource_type},
                timeout=30,
            )
            r.raise_for_status()
            return r.json()

    @tool()
    async def stack_subscribe(
        resource_type: Literal["data_package", "memory_domain"], resource_id: str
    ) -> dict:
        """Subscribe to an available data package or memory domain.

        Adds the resource to your persistent stack — the same effect as clicking
        "Add to stack" in the web UI; it applies to all future sessions. Use
        ``stack_browse`` first to find the ``resource_id`` of an available
        (``in_stack: false``) item.

        Args:
            resource_type: ``data_package`` or ``memory_domain``.
            resource_id:   The resource id from ``stack_browse``.

        Returns ``{"subscribed": true, "next_step": "..."}`` — ``next_step`` tells
        you what to run so the new resource becomes usable in this conversation.
        """
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{base_url}/api/stack/subscribe",
                json={"resource_type": resource_type, "resource_id": resource_id},
                headers=headers_fn(),
                timeout=30,
            )
            r.raise_for_status()
            body = r.json()
        # Post-subscribe hint — both supported types land as local tables pulled
        # by ``agnes pull`` (data packages → parquet, memory domains → synced
        # knowledge). Tell the model what to run so the resource is usable now.
        if isinstance(body, dict):
            body["next_step"] = "Run `agnes pull` to download the new tables."
        return body

    @tool()
    async def stack_unsubscribe(
        resource_type: Literal["data_package", "memory_domain"], resource_id: str
    ) -> dict:
        """Unsubscribe from a data package or memory domain in your stack.

        Removes a previously-subscribed resource. Required resources cannot be
        removed (the server returns an error) — only ``available`` ones you opted
        into. The local copy persists until the next ``agnes pull`` prunes it.

        Args:
            resource_type: ``data_package`` or ``memory_domain``.
            resource_id:   The resource id to unsubscribe from.

        Returns ``{"unsubscribed": true}`` on success.
        """
        async with httpx.AsyncClient() as c:
            r = await c.delete(
                f"{base_url}/api/stack/subscription/{resource_type}/{resource_id}",
                headers=headers_fn(),
                timeout=30,
            )
            r.raise_for_status()
        return {"unsubscribed": True}

    @tool()
    async def store_rate(entity_id: str, vote: Literal[1, -1, 0]) -> dict:
        """Rate a store / marketplace entity thumbs up/down (#398).

        Casts, changes, or clears your single vote on an entity — the same effect
        as the thumbs buttons in the marketplace detail view; one vote per entity
        per user, re-voting replaces the prior value.

        Args:
            entity_id: The store entity id (from ``catalog`` / marketplace browse).
            vote:      ``1`` = thumbs up, ``-1`` = thumbs down, ``0`` = clear your vote.

        Returns ``{"up", "down", "my_vote"}`` — the updated tally for the entity.
        """
        if vote not in (1, -1, 0):
            raise ValueError("vote must be 1, -1, or 0")
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{base_url}/api/store/entities/{entity_id}/rate",
                json={"vote": vote},
                headers=headers_fn(),
                timeout=30,
            )
            r.raise_for_status()
            return r.json()

    @tool()
    async def store_status(entity_id: str) -> dict:
        """Check the review-pipeline status of a flea-market entity you own (owner or admin only).

        After ``store upload`` the guardrail review runs asynchronously; the
        entity stays hidden until it passes. This returns the latest submission's
        status (``pending_llm`` / ``approved`` / ``blocked_llm`` /
        ``review_error`` / ``overridden``) plus an actionable hint. Mirrors
        ``agnes store status <id>``.

        Args:
            entity_id: The store entity id (from the upload response).

        Returns the ``GET /api/store/entities/{id}/status`` payload:
        ``{entity_id, name, type, visibility_status, version_no, submission,
        hint}``.
        """
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{base_url}/api/store/entities/{entity_id}/status",
                headers=headers_fn(),
                timeout=30,
            )
            r.raise_for_status()
            return r.json()

    @tool()
    async def store_publish_markdown(
        name: str,
        skill_md: str,
        type: str = "skill",
        description: str | None = None,
        category: str | None = None,
    ) -> dict:
        """Publish a skill or agent to the store from Markdown content — no ZIP needed.

        The server synthesizes the single-file bundle (``<name>/SKILL.md`` for a
        skill, ``<name>.md`` for an agent) and routes it through the same
        guardrail + review pipeline as a ZIP upload. The result may be held for
        automated review (``visibility_status: pending``) before it appears.
        Mirrors ``POST /api/store/entities/from-markdown`` and
        ``agnes store publish-md``.

        Args:
            name:        Name — lowercase letters, digits, dashes.
            skill_md:    The Markdown content (frontmatter optional; synthesized
                         from ``name``/``description`` when absent).
            type:        ``"skill"`` (default) or ``"agent"``.
            description: One-line *use when …* trigger (goes into frontmatter).
            category:    Optional store category (case-insensitive).

        Returns the created entity — ``{"id", "name", "invocation_name",
        "version", "visibility_status", …}``.
        """
        payload: dict = {"type": type, "name": name, "skill_md": skill_md}
        if description:
            payload["description"] = description
        if category:
            payload["category"] = category
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{base_url}/api/store/entities/from-markdown",
                json=payload,
                headers=headers_fn(),
                timeout=60,
            )
            r.raise_for_status()
            return r.json()

    @tool()
    async def marketplace_search(
        query: str = "",
        type: str = "",
        source: str = "",
        sort: str = "recent",
        limit: int = 24,
    ) -> dict:
        """Search the marketplace — Curated and Flea Market — for installable items.

        Default scope is EVERYWHERE: both the curated marketplaces and the Flea
        Market are searched, and every result carries a ``source`` label
        (``curated`` | ``flea``) plus an ``installed`` flag so you can tell what
        is already in the caller's stack. Results are RBAC-filtered to what the
        caller may access. Mirrors ``GET /api/marketplace/items`` and
        ``agnes marketplace search``.

        Args:
            query:  Search text (empty = browse all).
            type:   Filter: ``skill`` | ``agent`` | ``plugin`` (empty = all).
            source: Restrict to one tab: ``curated`` | ``flea`` (empty = both).
            sort:   ``recent`` (default) | ``most_used`` | ``trending``.
            limit:  Max results per tab (1–100).

        Returns ``{"items": [{"id", "type", "source", "name", "owner",
        "installed", …}], "total"}``. Install an item with ``marketplace_add``;
        inspect one with ``marketplace_detail``.
        """
        tabs = [source] if source else ["curated", "flea"]
        items: list = []
        async with httpx.AsyncClient() as c:
            for tab in tabs:
                params: dict = {"tab": tab, "sort": sort, "page_size": max(1, min(limit, 100))}
                if query:
                    params["q"] = query
                if type:
                    params["type"] = type
                r = await c.get(
                    f"{base_url}/api/marketplace/items",
                    headers=headers_fn(),
                    params=params,
                    timeout=30,
                )
                r.raise_for_status()
                items.extend(r.json().get("items", []))
        return {"items": items, "total": len(items)}

    @tool()
    async def marketplace_detail(item_id: str) -> dict:
        """Show full details for one marketplace item (curated or flea).

        Accepts the same id shapes as ``agnes marketplace detail``: a curated id
        is ``<marketplace_id>/<plugin_name>`` (contains a slash), a Flea Market
        id is the bare entity UUID. Returns the enriched detail — description,
        contents (skills / agents / commands / MCP servers), install state.
        Mirrors ``GET /api/marketplace/curated/{mid}/{plugin}`` /
        ``GET /api/marketplace/flea/{entity_id}/detail``.

        Args:
            item_id: ``<marketplace_id>/<plugin_name>`` or a flea entity UUID —
                     the tab-prefixed forms printed by ``marketplace_search``
                     (``curated-<mid>/<plugin>``, ``flea-<uuid>``) work as-is.
        """
        source, part1, part2 = _split_marketplace_id(item_id)
        if source == "curated":
            path = f"/api/marketplace/curated/{part1}/{part2}"
        else:
            path = f"/api/marketplace/flea/{part1}/detail"
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{base_url}{path}", headers=headers_fn(), timeout=30)
            r.raise_for_status()
            return r.json()

    @tool()
    async def marketplace_add(item_id: str) -> dict:
        """Add a marketplace item (plugin, skill, or agent) to the caller's stack.

        Persistent, like clicking "Add" in the web UI — applies to all future
        sessions. Same id shapes as ``marketplace_detail``. Flea items must have
        passed guardrail review (``approved``) — pending/blocked entities return
        409. Mirrors ``POST /api/store/entities/{id}/install`` (flea) /
        ``POST /api/marketplace/curated/{mid}/{plugin}/install`` (curated) and
        ``agnes marketplace add``.

        Args:
            item_id: ``<marketplace_id>/<plugin_name>`` or a flea entity UUID
                     (tab-prefixed ``marketplace_search`` ids work as-is).

        Returns ``{"installed": true, "next_step": …}`` — the plugin activates
        after the user's next plugin refresh.
        """
        source, part1, part2 = _split_marketplace_id(item_id)
        if source == "curated":
            path = f"/api/marketplace/curated/{part1}/{part2}/install"
        else:
            path = f"/api/store/entities/{part1}/install"
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{base_url}{path}", json={}, headers=headers_fn(), timeout=30)
            r.raise_for_status()
        return {
            "installed": True,
            "next_step": "Run /update-agnes-plugins in Claude Code (or `agnes update`) to activate it.",
        }

    @tool()
    async def marketplace_remove(item_id: str) -> dict:
        """Remove a marketplace item from the caller's stack.

        Inverse of ``marketplace_add``; same id shapes. System plugins pinned by
        an admin cannot be removed (409). Mirrors the DELETE install endpoints
        and ``agnes marketplace remove``.

        Args:
            item_id: ``<marketplace_id>/<plugin_name>`` or a flea entity UUID
                     (tab-prefixed ``marketplace_search`` ids work as-is).
        """
        source, part1, part2 = _split_marketplace_id(item_id)
        if source == "curated":
            path = f"/api/marketplace/curated/{part1}/{part2}/install"
        else:
            path = f"/api/store/entities/{part1}/install"
        async with httpx.AsyncClient() as c:
            r = await c.delete(f"{base_url}{path}", headers=headers_fn(), timeout=30)
            r.raise_for_status()
        return {
            "removed": True,
            "next_step": "Run /update-agnes-plugins in Claude Code (or `agnes update`) to apply it.",
        }

    @tool()
    async def store_update(
        entity_id: str,
        description: str = "",
        category: str = "",
        video_url: str = "",
    ) -> dict:
        """Edit the metadata of an owned Flea Market entity (owner or admin).

        Metadata-only: text fields update in place with no version bump or
        re-review. Binary replacements (new ZIP bundle, photo) have no MCP
        analogue — use ``agnes store update --zip/--photo``. Omitted (empty)
        fields are left untouched. Mirrors ``PUT /api/store/entities/{id}`` and
        ``agnes store update``.

        Args:
            entity_id:   The store entity id (from ``store_publish_markdown``
                         output or ``marketplace_search``).
            description: New description (empty = unchanged).
            category:    New category, case-insensitive (empty = unchanged).
            video_url:   New demo-video URL (empty = unchanged).

        Returns the updated entity ``{"id", "version", …}``.
        """
        data: dict = {}
        if description:
            data["description"] = description
        if category:
            data["category"] = category
        if video_url:
            data["video_url"] = video_url
        if not data:
            return {
                "error": "nothing_to_update",
                "hint": "Pass at least one of description / category / video_url.",
            }
        async with httpx.AsyncClient() as c:
            r = await c.put(
                f"{base_url}/api/store/entities/{entity_id}",
                data=data,
                headers=headers_fn(),
                timeout=30,
            )
            r.raise_for_status()
            return r.json()

    @tool()
    async def store_delete(entity_id: str) -> dict:
        """Delete an owned Flea Market entity (owner or admin).

        Soft-archives the entity by default (reversible): it is hidden from
        browse and refuses new installs, but the bundle stays on disk so users
        who already installed it keep it. Hard delete (drops the bundle and
        removes existing installs) is admin-only via the web/CLI. Mirrors
        ``DELETE /api/store/entities/{id}`` and ``agnes store delete``.

        Args:
            entity_id: The store entity id to delete.
        """
        async with httpx.AsyncClient() as c:
            r = await c.delete(
                f"{base_url}/api/store/entities/{entity_id}",
                headers=headers_fn(),
                timeout=30,
            )
            r.raise_for_status()
        return {"deleted": True, "entity_id": entity_id}

    @tool()
    async def admin_store_lint_findings(include_dismissed: bool = False) -> dict:
        """List advisory skill-lint findings across the store (admin only).

        Advisory craft findings — bloat, weak triggers, likely duplicates — never
        block publication. Mirrors ``GET /api/admin/store/lint-findings`` and
        ``agnes admin store lint-findings``.

        Args:
            include_dismissed: Also include findings an admin has dismissed.

        Returns ``{"findings": [...], "last_run": {...}|null}``.
        """
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{base_url}/api/admin/store/lint-findings",
                params={"include_dismissed": str(include_dismissed).lower()},
                headers=headers_fn(),
                timeout=30,
            )
            r.raise_for_status()
            return r.json()

    @tool()
    async def admin_store_lint_audit(force: bool = False) -> dict:
        """Run a full skill-lint audit over published skills now (admin only).

        Loads the store corpus once and lints each published skill, skipping
        entities whose content is unchanged since their last lint. Guarded by a
        configurable minimum interval unless ``force`` is set. Mirrors
        ``POST /api/admin/store/lint-audit`` and ``agnes admin store lint-audit``.

        Args:
            force: Run even if a recent audit already ran (bypass the guard).

        Returns the run stats, or ``{"skipped": true, ...}`` if the guard fired.
        """
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{base_url}/api/admin/store/lint-audit",
                json={"force": force},
                headers=headers_fn(),
                timeout=300,
            )
            r.raise_for_status()
            return r.json()

    @tool()
    async def admin_store_lint_dismiss(entity_id: str, rule_id: str) -> dict:
        """Dismiss one advisory finding until the entity's content changes (admin only).

        The dismissal is keyed to the finding's current content hash, so it
        auto-resets when the skill is edited. Mirrors
        ``POST /api/admin/store/lint-dismiss`` and ``agnes admin store lint-dismiss``.

        Args:
            entity_id: The store entity id.
            rule_id:   The rule id to dismiss (e.g. ``SL002``).

        Returns ``{"dismissed": true}``.
        """
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{base_url}/api/admin/store/lint-dismiss",
                json={"entity_id": entity_id, "rule_id": rule_id},
                headers=headers_fn(),
                timeout=30,
            )
            r.raise_for_status()
            return r.json()

    @tool()
    async def documentation_api() -> str:
        """Return the curated Agnes REST API reference as Markdown.

        Mirrors the in-app ``/documentation/api`` page and the ``agnes docs api``
        CLI command — three surfaces in lockstep so a public endpoint is reachable
        everywhere it can be looked up. Useful when an agent is composing a
        request against ``/api/*`` and needs to know payload shapes, auth
        requirements, or the inventory of available endpoints without leaving the
        chat.
        """
        md_path = Path(__file__).resolve().parent.parent.parent.parent / "docs" / "api-reference.md"
        try:
            return md_path.read_text(encoding="utf-8")
        except OSError:
            return "# API reference unavailable\n\nThe source markdown file is missing from this deployment."

    @tool()
    async def list_contributed_skills() -> dict:
        """List all plugins in the Agnes Contributed marketplace (admin only).

        Returns name, version, description, and granted group for each plugin
        contributed via the web form, CLI, or ``contribute_skill`` MCP tool.
        Mirrors ``GET /api/admin/contributed-skills`` and ``agnes admin skill list``.

        Requires an admin PAT.
        """
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{base_url}/api/admin/contributed-skills",
                headers=headers_fn(),
                timeout=30,
            )
            r.raise_for_status()
            return r.json()

    @tool()
    async def contribute_skill(skill_md: str, grant_group: str = "Admin") -> dict:
        """Publish a SKILL.md into the Agnes Contributed marketplace (admin only).

        Parses the SKILL.md frontmatter, wraps the skill in a one-skill plugin,
        and grants it to ``grant_group``. Mirrors ``POST /api/admin/contributed-skills``
        and ``agnes admin skill contribute``.
        """
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{base_url}/api/admin/contributed-skills",
                json={"skill_md": skill_md, "grant_group": grant_group},
                headers=headers_fn(),
                timeout=30,
            )
            r.raise_for_status()
            return r.json()

    @tool()
    async def delete_contributed_skill(name: str) -> dict:
        """Remove a contributed skill by plugin name (admin only).

        Mirrors ``DELETE /api/admin/contributed-skills/{name}`` and
        ``agnes admin skill delete``.
        """
        async with httpx.AsyncClient() as c:
            r = await c.delete(
                f"{base_url}/api/admin/contributed-skills/{name}",
                headers=headers_fn(),
                timeout=30,
            )
            r.raise_for_status()
            return {"deleted": name, "status": r.status_code}

    @tool()
    async def admin_config_surface() -> dict:
        """Return the complete per-instance configuration surface (admin only).

        Reads every ``get_*`` resolver in ``app/instance_config.py`` and returns
        their current values alongside which tier supplied each one (env/yaml/default),
        the registered Initial Workspace Template (if any), every registered
        marketplace, and the ``infra_repo_url`` knob.

        Useful for an operator's Claude that needs instance-accurate pointers
        (IWT URL, marketplace URLs, knob values, infra repo) without hardcoding
        anything. Mirrors ``GET /api/admin/config-surface`` and
        ``agnes admin config-surface``.

        Requires an admin PAT.
        """
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{base_url}/api/admin/config-surface",
                headers=headers_fn(),
                timeout=30,
            )
            r.raise_for_status()
            return r.json()

    @tool()
    async def admin_source_connections_list(source_type: str = "") -> dict:
        """List named source connections (multi-project Keboola support).

        Returns all registered source connections. Pass ``source_type="keboola"``
        to filter to Keboola connections only.

        Mirrors ``GET /api/admin/source-connections`` and
        ``agnes admin connection list``.

        Requires an admin PAT.
        """
        async with httpx.AsyncClient() as c:
            params = {"source_type": source_type} if source_type else {}
            r = await c.get(
                f"{base_url}/api/admin/source-connections",
                headers=headers_fn(),
                params=params,
                timeout=30,
            )
            r.raise_for_status()
            return {"connections": r.json()}

    @tool()
    async def admin_knowledge_digests_list() -> dict:
        """List all maintained digests (admin only).

        A maintained digest is an admin-defined markdown document — title +
        standing instructions + a set of source Collections — that the scheduler
        regenerates with an LLM only when its sources' content changes. Access to
        a digest's content is controlled by ``resource_grants`` on the
        ``knowledge_digest`` resource type: a grant is what makes ``agnes pull``
        deliver the digest to a group's members as ``.claude/rules/ka_<slug>.md``.

        Returns ``{"items": [{"id", "slug", "title", "status",
        "status_reason", "generated_at", "output_md" (280-char preview),
        "output_chars"}, ...]}``. Mirrors ``GET /api/admin/knowledge-digests``
        and ``agnes admin digest list``.

        Requires an admin PAT.
        """
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{base_url}/api/admin/knowledge-digests",
                headers=headers_fn(),
                timeout=30,
            )
            r.raise_for_status()
            return r.json()

    @tool()
    async def admin_knowledge_digest_get(digest_id: str) -> dict:
        """Show one maintained digest's full detail (admin only).

        Includes the full ``output_md`` (the list tool only ships a preview) and
        the staleness fields: a digest whose sources changed but whose last
        regeneration failed is ``status: "stale"`` with a ``status_reason`` —
        the previous markdown is kept and still distributed, never silently.

        Args:
            digest_id: The digest id (from ``admin_knowledge_digests_list``).

        Mirrors ``GET /api/admin/knowledge-digests/{id}`` and
        ``agnes admin digest show``. Requires an admin PAT.
        """
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{base_url}/api/admin/knowledge-digests/{digest_id}",
                headers=headers_fn(),
                timeout=30,
            )
            r.raise_for_status()
            return r.json()

    @tool()
    async def admin_knowledge_digest_create(
        slug: str,
        title: str,
        instructions: str,
        source_corpus_ids: list[str] | None = None,
    ) -> dict:
        """Create a new maintained digest (admin only).

        The digest starts ``status: "pending"`` — no markdown is generated until
        the next scheduler pass fingerprints the source Collections and runs the
        LLM regeneration. Granting a group access (``agnes admin grant create
        <group> knowledge_digest <digest_id>``) is what makes ``agnes pull``
        deliver it to that group's members as ``.claude/rules/ka_<slug>.md``.

        Args:
            slug:              URL-safe stable id — becomes the filename
                                ``ka_<slug>.md`` on every analyst laptop.
                                Immutable after create.
            title:              Display title.
            instructions:       Standing instructions for the LLM regeneration
                                 pass (what the digest should cover / how).
            source_corpus_ids:  Ids of the source Collections to fingerprint and
                                 summarize. Defaults to none.

        Mirrors ``POST /api/admin/knowledge-digests`` and
        ``agnes admin digest create``. Requires an admin PAT.
        """
        payload = {
            "slug": slug,
            "title": title,
            "instructions": instructions,
            "source_corpus_ids": source_corpus_ids or [],
        }
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{base_url}/api/admin/knowledge-digests",
                json=payload,
                headers=headers_fn(),
                timeout=30,
            )
            r.raise_for_status()
            return r.json()

    @tool()
    async def admin_knowledge_digest_update(
        digest_id: str,
        title: str | None = None,
        instructions: str | None = None,
        source_corpus_ids: list[str] | None = None,
    ) -> dict:
        """Update a maintained digest's metadata (admin only).

        Only the supplied fields change; the slug is immutable (it's already a
        filename on analyst laptops). Editing ``instructions`` or
        ``source_corpus_ids`` flips the digest's content fingerprint, so the next
        scheduler pass regenerates it even if the source Collections themselves
        haven't changed.

        Args:
            digest_id:          The digest id to update.
            title:              New display title, if changing.
            instructions:       New standing instructions, if changing.
            source_corpus_ids:  New full list of source Collection ids, if
                                changing (replaces the previous list).

        Mirrors ``PUT /api/admin/knowledge-digests/{id}`` and
        ``agnes admin digest edit``. Requires an admin PAT.
        """
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if instructions is not None:
            payload["instructions"] = instructions
        if source_corpus_ids is not None:
            payload["source_corpus_ids"] = source_corpus_ids
        async with httpx.AsyncClient() as c:
            r = await c.put(
                f"{base_url}/api/admin/knowledge-digests/{digest_id}",
                json=payload,
                headers=headers_fn(),
                timeout=30,
            )
            r.raise_for_status()
            return r.json()

    @tool()
    async def admin_knowledge_digest_delete(digest_id: str) -> dict:
        """Delete a maintained digest (admin only).

        Also removes any dangling ``resource_grants`` rows for the digest, so no
        group retains a grant pointing at a now-nonexistent resource. Analyst
        laptops prune the corresponding ``ka_<slug>.md`` on their next
        ``agnes pull``.

        Args:
            digest_id: The digest id to delete.

        Mirrors ``DELETE /api/admin/knowledge-digests/{id}`` and
        ``agnes admin digest delete``. Requires an admin PAT.
        """
        async with httpx.AsyncClient() as c:
            r = await c.delete(
                f"{base_url}/api/admin/knowledge-digests/{digest_id}",
                headers=headers_fn(),
                timeout=30,
            )
            r.raise_for_status()
        return {"deleted": digest_id}

    @tool()
    async def my_secret_test(source_id: str) -> dict:
        """Verify your own stored credential for a per_user MCP source.

        Runs a live connectivity check against the upstream under YOUR
        credential (not the shared one). Returns ``{ok, tool_count, message}``.
        If you are not connected (or another 4xx condition applies — not
        granted, rate-limited, ...), ``ok`` is ``False`` and ``message``
        carries the server's remedy text (e.g. where to add your token).

        Args:
            source_id: The MCP source id (``src_*``).

        Mirrors ``POST /api/mcp/sources/{id}/my-secret/test`` and
        ``agnes mcp my-secret test``.
        """
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{base_url}/api/mcp/sources/{source_id}/my-secret/test",
                headers=headers_fn(),
                timeout=30,
            )
            # 4xx bodies carry the connect remedy (e.g. the not-connected 403's
            # `detail` — see mcp_user_secrets.py) — raise_for_status() would
            # discard it and surface only a generic "403 Forbidden" to the
            # model, defeating the "tells you where to add your token" promise
            # above. Only genuine 5xx/transport errors still raise.
            if 400 <= r.status_code < 500:
                try:
                    detail = r.json().get("detail", r.text)
                except ValueError:
                    detail = r.text
                return {"ok": False, "tool_count": None, "message": detail}
            r.raise_for_status()
            return r.json()

    @tool()
    async def admin_jobs_list(status: str = "", kind: str = "", limit: int = 50) -> dict:
        """List jobs on the wave-2B durable job queue (admin only).

        Jobs are the worker-runtime's unit of work (data-refresh, jira-refresh,
        marketplaces-sync, session-collector, corporate-memory, and any other
        kind registered in ``JOB_KINDS``). Use this to check whether an
        enqueued job has started, finished, or is retrying after a failure.

        Args:
            status: Filter by status: "queued" | "running" | "done" | "failed".
                    Empty (default) returns all statuses.
            kind:   Filter by job kind (e.g. "data-refresh"). Empty (default)
                    returns all kinds.
            limit:  Max rows to return, most recent first (default 50).

        Returns ``{"jobs": [{"id", "kind", "status", "priority", "attempts",
        "max_attempts", "payload", "created_at", "started_at", "finished_at",
        "error", ...}, ...]}``. Mirrors ``GET /api/jobs`` and
        ``agnes admin jobs list``.

        Requires an admin PAT.
        """
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        if kind:
            params["kind"] = kind
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{base_url}/api/jobs", headers=headers_fn(), params=params, timeout=30)
            r.raise_for_status()
            return r.json()

    @tool()
    async def admin_job_get(job_id: str) -> dict:
        """Show one job's full detail, incl. payload and error (admin only).

        Args:
            job_id: The job id (from ``admin_jobs_list`` or the id returned by
                    ``admin_job_enqueue``).

        Mirrors ``GET /api/jobs/{job_id}`` and ``agnes admin jobs show``.
        Requires an admin PAT. 404 if the job doesn't exist.
        """
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{base_url}/api/jobs/{job_id}", headers=headers_fn(), timeout=30)
            r.raise_for_status()
            return r.json()

    @tool()
    async def admin_job_enqueue(kind: str, payload: dict | None = None, idempotency_key: str = "") -> dict:
        """Enqueue a job on the wave-2B worker runtime (admin only).

        ``kind`` must already be registered in the server's ``JOB_KINDS``
        registry (populated at startup by ``register_all_kinds()``) — an
        unrecognized kind 400s with the list of currently-registered kinds.

        Args:
            kind:            Registered job kind (e.g. "data-refresh",
                             "marketplaces-sync", "session-collector",
                             "corporate-memory", "jira-refresh").
            payload:         Job-specific payload dict. Defaults to empty.
            idempotency_key: Dedup key — if a queued/running job already has
                             this key, that job is returned unchanged instead
                             of enqueuing a duplicate. Empty (default) disables
                             dedup.

        Mirrors ``POST /api/jobs`` and ``agnes admin jobs enqueue``. Requires
        an admin PAT.
        """
        body: dict[str, Any] = {"kind": kind, "payload": payload or {}}
        if idempotency_key:
            body["idempotency_key"] = idempotency_key
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{base_url}/api/jobs", json=body, headers=headers_fn(), timeout=30)
            r.raise_for_status()
            return r.json()

    @tool()
    async def admin_analytics_migrate(to: Literal["ducklake", "legacy"]) -> dict:
        """Migrate the analytics query surface between backends (admin only).

        Validates prerequisites (``to="ducklake"`` only: the DuckLake DuckDB
        extension is loadable and the catalog is reachable, auto-repairing a
        missing catalog database on an existing Postgres volume where the
        init-script never ran) and enqueues an ``analytics-migrate`` job that
        rebuilds the named target backend from the on-disk extracts tree —
        no re-extract from the source system, in either direction.

        This call never flips ``analytics.backend`` in config — it is read
        once at boot, not hot-reloaded. Once the returned job completes
        (poll with ``admin_job_get``), set ``analytics.backend`` in
        ``instance.yaml`` (or ``AGNES_ANALYTICS_BACKEND`` env) on every role
        process and restart to actually switch query serving over.

        Args:
            to: Target backend — "ducklake" or "legacy" (rollback).

        Returns ``{status, to, job_id, message}`` on success. Mirrors
        ``POST /api/admin/analytics/migrate`` and
        ``agnes admin analytics migrate --to <target>``. Requires an admin
        PAT. Raises on a 400 (unmet prerequisites — see the error body for
        the full problem list) or a 409 (a migration is already running).
        """
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{base_url}/api/admin/analytics/migrate",
                json={"to": to},
                headers=headers_fn(),
                timeout=30,
            )
            r.raise_for_status()
            return r.json()

    @tool()
    async def agent_list() -> dict:
        """List your own agent profiles.

        Every user has an implicit default agent (all-mode, unbounded scope)
        plus any named agents they've created. Requires an interactive
        session credential — the underlying endpoint
        (``require_session_token``) rejects a PAT of any flavor (plain PAT
        or agent PAT), so this tool only succeeds over the streamable-HTTP
        OAuth transport, which forwards a real Agnes session JWT; over the
        SSE transport (PAT-authenticated) it fails with a 403
        ``"This endpoint requires an interactive session, not a PAT"``.

        Returns ``{"data": [...], "has_more": false, "next_cursor": null}``.
        Each entry has ``id``, ``slug``, ``name``, ``description``,
        ``model`` (null = server default, no model policy), ``token_budget_monthly``
        (null = unbounded), the four scope-mode fields
        (``plugins_mode``/``connections_mode``/``tables_mode``/
        ``memory_mode``, each ``"all"`` or ``"selected"``),
        ``memory_write_mode``, ``is_default``, ``created_at``. Mirrors
        ``GET /api/v1/agents`` and ``agnes agent list``.
        """
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{base_url}/api/v1/agents", headers=headers_fn(), timeout=30)
            r.raise_for_status()
            return r.json()

    @tool()
    async def agent_ask(slug: str, prompt: str, timeout_s: int = 120) -> dict:
        """One-shot synchronous request/response over one of your agents.

        Sync-only: this tool never sets ``background: true`` and does not
        poll ``GET /api/v1/jobs/{id}`` for you. If the run outlasts
        ``timeout_s`` the server degrades to a background job and replies
        with a ``job_id`` instead of an answer — the underlying run keeps
        going server-side, only the wait was bounded. Waiting out a
        background run (foreground polling to completion) has no MCP tool
        by design — an MCP tool call blocking on a poll loop is a poor fit
        for a chat turn; use ``agnes agent ask`` (CLI) or
        ``POST /api/v1/agents/{slug}/responses`` directly for that.

        Args:
            slug:      Agent slug (from ``agent_list``).
            prompt:    The input to send to the agent.
            timeout_s: Max seconds to wait for a synchronous answer
                       (default 120, clamped to [1, 600] server-side).

        Returns the ``200`` body — ``{"answer", "session_id", "response_id",
        "usage", "agent_config_hash", "request_id"}`` — or, on a ``202``
        degrade, ``{"job_id": ...}`` with no ``answer`` yet. Mirrors
        ``POST /api/v1/agents/{slug}/responses`` and ``agnes agent ask``.
        """
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{base_url}/api/v1/agents/{slug}/responses",
                json={"input": prompt, "timeout_s": timeout_s},
                headers=headers_fn(),
                timeout=timeout_s + 10,
            )
            r.raise_for_status()
            return r.json()

    @tool()
    async def agent_usage(slug: str, period: str = "") -> dict:
        """Show one of your agents' monthly token usage against its budget.

        Args:
            slug:   Agent slug (from ``agent_list``).
            period: Month to report, ``YYYY-MM``. Empty (default) reports
                    the current UTC month.

        Returns ``{period, agent_slug, input_tokens, output_tokens,
        cache_read_tokens, cache_creation_tokens, total_tokens,
        budget_limit, budget_remaining}`` — the usage-shaped fields mirror
        Anthropic's own usage object; ``total_tokens`` excludes
        ``cache_read_tokens`` (informational only, not counted against
        budget), so ``budget_remaining`` lines up with when a call against
        this agent would actually start 429ing with ``budget_exhausted``.
        ``budget_limit``/``budget_remaining`` are ``null`` for an agent
        with no configured budget. Mirrors
        ``GET /api/v1/agents/{slug}/usage`` and ``agnes agent usage``.
        """
        params: dict[str, Any] = {"period": period} if period else {}
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{base_url}/api/v1/agents/{slug}/usage",
                headers=headers_fn(),
                params=params,
                timeout=30,
            )
            r.raise_for_status()
            return r.json()

    @tool()
    async def data_apps_list(kind: Literal["", "hosted", "linked"] = "") -> dict:
        """List data apps you can see (RBAC-filtered).

        Visible to any authenticated user: apps you own, apps a group you're
        in has a ``resource_grants`` row for, or (Admin) all apps. Each entry
        has a ``kind`` — ``hosted`` (an app Agnes runs, with a runtime
        ``state``) or ``linked`` (an externally-hosted app, e.g. on Keboola,
        whose ``url`` opens the remote app directly). Returns a list of app
        summaries — ``slug``, ``name``, ``kind``, ``state``, ``url``,
        ``effective_description``, and metadata; secrets are never included.

        Args:
            kind: Optional filter — ``"hosted"`` or ``"linked"``; empty
                  (default) lists both.

        Mirrors ``GET /api/data-apps[?kind=]`` and ``agnes app list [--linked]``.
        """
        params = {"kind": kind} if kind else None
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{base_url}/api/data-apps", headers=headers_fn(), params=params, timeout=30)
            r.raise_for_status()
            return r.json()

    @tool()
    async def data_app_get(slug: str) -> dict:
        """Show one hosted data app's detail.

        Any authenticated user with view access to the app (owner, Admin, or
        a group granted access via ``resource_grants``) may call this.

        Args:
            slug: The app's slug (from ``data_apps_list``).

        Mirrors ``GET /api/data-apps/{slug}`` and ``agnes app show``.
        """
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{base_url}/api/data-apps/{slug}", headers=headers_fn(), timeout=30)
            r.raise_for_status()
            return r.json()

    @tool()
    async def data_app_deploy(slug: str, sha: str = "", mode: Literal["", "dev"] = "") -> dict:
        """Deploy (or redeploy) a hosted data app — app owner or Admin only.

        Fast-forwards the app's ``agnes-live`` ref (to ``sha`` if given,
        otherwise the tracked branch's latest), mints a fresh service token,
        and hands the build off to the runner sidecar. ``mode="dev"`` deploys
        a draft app on its pinned iteration branch instead — a draft has no
        ``agnes-live`` ref to fast-forward, so ``sha`` is ignored in that mode.

        Args:
            slug: The app's slug (a prod app's slug, or a draft's own slug
                  when ``mode="dev"``).
            sha:  Optional commit sha to deploy. Empty (default) fast-forwards
                  to the tracked branch's latest commit. Ignored for draft
                  deploys.
            mode: ``"dev"`` deploys a draft's branch; empty (default) deploys
                  prod. A draft app rejects anything but ``mode="dev"``.

        Returns ``{"state": "running", "deployed_sha": "..."}``. Mirrors
        ``POST /api/data-apps/{slug}/deploy`` and ``agnes app deploy``.
        """
        payload: dict = {}
        if sha:
            payload["sha"] = sha
        if mode:
            payload["mode"] = mode
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{base_url}/api/data-apps/{slug}/deploy",
                json=payload,
                headers=headers_fn(),
                timeout=60,
            )
            r.raise_for_status()
            return r.json()

    @tool()
    async def data_app_create_draft(slug: str, branch: str = "init") -> dict:
        """Create a draft of a prod data app on an iteration branch — app owner or Admin only.

        The draft shares the prod app's git repo (no second repo, no copy):
        it is a registry sibling row pinned to ``branch`` on the parent's
        repo, deployable with ``data_app_deploy(draft_slug, mode="dev")``.
        Drafts are hidden from ``data_apps_list`` — reach them via the
        parent's ``drafts`` field in ``data_app_get``.

        Args:
            slug:   The PROD app's slug (must not itself be a draft).
            branch: Iteration branch name (default ``"init"``).

        Returns ``{"id", "slug", "branch", "git_clone_url"}`` — the draft's
        slug and a git push credential embedded in the clone URL. Mirrors
        ``POST /api/data-apps/{slug}/drafts`` and ``agnes app draft create``.
        """
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{base_url}/api/data-apps/{slug}/drafts",
                headers=headers_fn(),
                json={"branch": branch},
                timeout=60,
            )
            r.raise_for_status()
            return r.json()

    @tool()
    async def data_app_delete_draft(slug: str, draft_slug: str) -> dict:
        """Tear down a draft of a prod data app — app owner or Admin only.

        Stops the draft's container, revokes its service token, deletes the
        iteration branch on the parent's repo, and removes the draft's
        registry row.

        Args:
            slug:       The PROD app's slug (the draft's parent).
            draft_slug: The draft's own slug (from ``data_app_create_draft``
                        or the parent's ``drafts`` field).

        Returns ``{"status": "deleted"}``. Mirrors
        ``DELETE /api/data-apps/{slug}/drafts/{draft_slug}`` and
        ``agnes app draft delete``.
        """
        async with httpx.AsyncClient() as c:
            r = await c.request(
                "DELETE",
                f"{base_url}/api/data-apps/{slug}/drafts/{draft_slug}",
                headers=headers_fn(),
                timeout=60,
            )
            r.raise_for_status()
            return {"status": "deleted"}

    @tool()
    async def data_app_git_credential(slug: str) -> dict:
        """Mint a fresh git push credential for a data app — app owner or Admin only.

        Args:
            slug: The app's slug (a prod app; drafts push through the same
                  parent-repo credential minted here or at draft-create time).

        Returns ``{"git_clone_url": "..."}`` with an embedded, time-scoped
        push credential. Mirrors ``POST /api/data-apps/{slug}/git-credential``
        and ``agnes app git-credential``.
        """
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{base_url}/api/data-apps/{slug}/git-credential",
                headers=headers_fn(),
                timeout=30,
            )
            r.raise_for_status()
            return r.json()

    @tool()
    async def data_app_logs(slug: str, tail: int = 200) -> dict:
        """Show the last N lines of runner logs for a hosted data app — app owner or Admin only.

        Args:
            slug: The app's slug.
            tail: Number of trailing log lines to return (default 200).

        Returns ``{"logs": "..."}``. Mirrors ``GET /api/data-apps/{slug}/logs``
        and ``agnes app logs``.
        """
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{base_url}/api/data-apps/{slug}/logs",
                headers=headers_fn(),
                params={"tail": tail},
                timeout=30,
            )
            r.raise_for_status()
            return r.json()

    @tool()
    async def data_app_set_description(slug: str, description: str) -> dict:
        """Set the admin description override on a managed (linked) data app.

        Linked apps are org resources whose ``description`` the ingest sync
        refreshes; this pins a human-authored description the sync won't clobber.
        Owner/Admin only; managed rows only (a 409 ``not_managed`` comes back for
        a hosted app — edit those via the normal update flow).

        Args:
            slug:        The app's slug.
            description: The description to pin (empty string clears it).

        Returns the updated app dict. Mirrors ``PATCH /api/data-apps/{slug}`` and
        ``agnes app set-description``.
        """
        async with httpx.AsyncClient() as c:
            r = await c.patch(
                f"{base_url}/api/data-apps/{slug}",
                json={"description": description},
                headers=headers_fn(),
                timeout=30,
            )
            r.raise_for_status()
            return r.json()

    def _data_apps_disabled_payload() -> dict:
        return {
            "error": "data_apps_disabled",
            "message": "Data apps are disabled on this instance.",
        }

    def _is_data_apps_disabled_response(r: httpx.Response) -> bool:
        if r.status_code != 404:
            return False
        try:
            return r.json().get("detail") == "data_apps_disabled"
        except Exception:
            return False

    @tool()
    async def agnes_data_app_preview(slug: str, url: str = "") -> dict:
        """Open or refresh the in-chat split-pane preview of a hosted data app.

        Chat-surface-only (spec §7/§9) — no REST/CLI analogue; the runner
        forwards this tool's return value verbatim into a render directive
        the web chat frontend switches on.

        Call this TWICE per preview cycle: first with an empty ``url`` (the
        default) the moment a scaffold/dev deploy is kicked off — this opens
        a placeholder pane immediately (spec §7 mandate), before the app is
        actually reachable. Once the dev deploy is healthy (poll
        ``data_app_get`` in short steps), call again with the real ``url``
        (typically ``/apps/<slug>/``) to swap the pane to the live app —
        this second call mints a short-TTL scoped preview grant
        (``POST /api/data-apps/{slug}/preview-grant``) so the iframe loads
        without a cross-origin login.

        Args:
            slug: The (draft or prod) app's slug.
            url:  Empty (default) for the placeholder call; the app's URL
                  (e.g. ``/apps/<slug>/``) to swap to the live pane.

        Returns ``{"render": "data_app_preview", "slug", "url"}`` — ``url`` is
        ``null`` on the placeholder call. The live-URL call mints the scoped
        preview cookie server-side (installed via the grant endpoint's
        ``Set-Cookie`` header; the web chat re-fetches it same-origin), but the
        token value is deliberately NOT returned — a tool result is archived in
        the session transcript, and this is a live bearer credential. Returns a
        friendly ``data_apps_disabled`` payload (not an error) if data apps are
        disabled on this instance.
        """
        if not url:
            return {"render": "data_app_preview", "slug": slug, "url": None}
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{base_url}/api/data-apps/{slug}/preview-grant",
                headers=headers_fn(),
                timeout=30,
            )
            if _is_data_apps_disabled_response(r):
                return _data_apps_disabled_payload()
            r.raise_for_status()
        # The POST above validates view access (403 -> raises) and installs the
        # scoped cookie via its Set-Cookie header. We intentionally do NOT read
        # or surface the cookie value: the render directive the web chat needs
        # carries only slug + url, and the frontend lands the HttpOnly cookie
        # itself via a same-origin re-fetch of the grant endpoint.
        return {"render": "data_app_preview", "slug": slug, "url": url}

    @tool()
    async def agnes_data_app_refresh(slug: str) -> dict:
        """Force-reload the in-chat preview pane for a hosted data app.

        Chat-surface-only (spec §9) — no REST/CLI analogue; a pure render
        directive with no server round-trip. Call after pushing a fresh
        commit to a draft's dev deploy so the iframe picks up the change
        without the user manually reloading.

        Args:
            slug: The app's slug the currently-open pane is showing.

        Returns ``{"render": "data_app_preview_refresh", "slug"}``.
        """
        return {"render": "data_app_preview_refresh", "slug": slug}

    @tool()
    async def agnes_data_app_close(slug: str) -> dict:
        """Tear down the in-chat preview pane for a hosted data app.

        Chat-surface-only (spec §9) — no REST/CLI analogue; a pure render
        directive with no server round-trip. Call this BEFORE
        ``data_app_delete_draft`` when abandoning or promoting a draft (the
        extras skill's ordering rule) — closing the pane first avoids the
        iframe pointing at a container that's about to disappear.

        Args:
            slug: The app's slug the currently-open pane is showing.

        Returns ``{"render": "data_app_preview_close", "slug"}``.
        """
        return {"render": "data_app_preview_close", "slug": slug}

    @tool()
    async def agnes_data_app_credentials(slug: str) -> dict:
        """Show the shareable URL for a hosted data app — the TERMINAL
        render of a reply (spec §7): never follow this tool's result with
        more chat text.

        Chat-surface-only (spec §9) — no REST/CLI analogue.

        Args:
            slug: The app's slug.

        Returns ``{"render": "data_app_credentials", "slug", "url",
        "password"}``. ``password`` is always ``null`` today — the
        control-plane detail endpoint this calls never returns the
        encrypted secrets blob (by design), so there is no shared
        basic-auth password to surface yet; the chat reply should hint at
        granting a group access via ``/admin/access`` instead of sharing a
        password. Returns a friendly ``data_apps_disabled`` payload (not an
        error) if data apps are disabled on this instance.
        """
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{base_url}/api/data-apps/{slug}", headers=headers_fn(), timeout=30)
            if _is_data_apps_disabled_response(r):
                return _data_apps_disabled_payload()
            r.raise_for_status()
            detail = r.json()
        return {
            "render": "data_app_credentials",
            "slug": slug,
            "url": detail.get("url"),
            "password": None,
        }

    @tool()
    async def tool_docs(tool_name: str) -> dict:
        """Return the full reference documentation (docstring) for one registered MCP tool — arguments, return shape, and usage tips beyond the short description shown in the tool list."""
        doc = TOOL_DOCS.get(tool_name)
        if doc is None:
            known = ", ".join(sorted(TOOL_DOCS))
            raise ValueError(f"Unknown tool {tool_name!r}. Valid tool names: {known}")
        return {"tool": tool_name, "docs": doc}

    return list(FOUNDATION_TOOL_NAMES)
