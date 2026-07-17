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
from typing import Any, Callable

import httpx
from mcp.server.fastmcp import FastMCP

FOUNDATION_TOOL_NAMES: tuple[str, ...] = (
    "server_info",
    "catalog",
    "collections_list",
    "collection_get",
    "collections_search",
    "knowledge_search",
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
    "admin_store_lint_findings",
    "admin_store_lint_audit",
    "admin_store_lint_dismiss",
    "documentation_api",
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
)


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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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
        return {"schema": rs.json(), "sample": rm.json()}

    @mcp.tool()
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
            return r.json()

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
    async def stack_browse(resource_type: str) -> dict:
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

    @mcp.tool()
    async def stack_subscribe(resource_type: str, resource_id: str) -> dict:
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

    @mcp.tool()
    async def stack_unsubscribe(resource_type: str, resource_id: str) -> dict:
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

    @mcp.tool()
    async def store_rate(entity_id: str, vote: int) -> dict:
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

    @mcp.tool()
    async def store_status(entity_id: str) -> dict:
        """Check the review-pipeline status of a flea-market entity you own.

        After ``store upload`` the guardrail review runs asynchronously; the
        entity stays hidden until it passes. This returns the latest submission's
        status (``pending_llm`` / ``approved`` / ``blocked_llm`` /
        ``review_error`` / ``overridden``) plus an actionable hint. Owner or
        admin only. Mirrors ``agnes store status <id>``.

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

    @mcp.tool()
    async def store_publish_markdown(
        name: str,
        skill_md: str,
        description: str | None = None,
        category: str | None = None,
    ) -> dict:
        """Publish a skill to the store from Markdown content — no ZIP needed.

        The server synthesizes the SKILL.md folder and routes it through the same
        guardrail + review pipeline as a ZIP upload. The result may be held for
        automated review (``visibility_status: pending``) before it appears.
        Mirrors ``POST /api/store/entities/from-markdown`` and
        ``agnes store publish-md``.

        Args:
            name:        Skill name — lowercase letters, digits, dashes.
            skill_md:    The SKILL.md content (frontmatter optional; synthesized
                         from ``name``/``description`` when absent).
            description: One-line *use when …* trigger (goes into frontmatter).
            category:    Optional store category (case-insensitive).

        Returns the created entity — ``{"id", "name", "invocation_name",
        "version", "visibility_status", …}``.
        """
        payload: dict = {"type": "skill", "name": name, "skill_md": skill_md}
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
    async def my_secret_test(source_id: str) -> dict:
        """Verify your own stored credential for a per_user MCP source.

        Runs a live connectivity check against the upstream under YOUR
        credential (not the shared one). Returns ``{ok, tool_count, message}``.
        If you are not connected, this returns a 403 whose message tells you
        where to add your token.

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
            r.raise_for_status()
            return r.json()

    return list(FOUNDATION_TOOL_NAMES)
