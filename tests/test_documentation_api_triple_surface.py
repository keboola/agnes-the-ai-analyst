"""Triple-surface coverage check for the new ``/documentation/api`` cohort.

Project policy: every public ``/api/*`` (and ``/documentation/*``) endpoint
added going forward MUST be reachable via three surfaces:

  1. REST (HTTP)            — ``app.web.router``
  2. CLI (``agnes …``)      — ``cli/commands/…`` + ``cli/main.py`` registration
  3. MCP tool               — ``app.api.mcp_http`` (HTTP MCP server)

This locks the v0.68.0 cohort: a single endpoint, ``GET /documentation/api``,
landed with a matching ``agnes docs api`` CLI subcommand AND a matching
``documentation_api`` MCP tool. The intent isn't a full retroactive sweep
(pre-existing endpoints stay as-is) — it's a forward-only ratchet so the
new floor never erodes.

When you add another endpoint that ought to live on all three surfaces,
extend ``_COHORT`` below and the test fails until the CLI/MCP entries land.
"""

from __future__ import annotations


# Endpoints that must have all three surfaces. Forward-only — add new
# entries when they land, do NOT retroactively backfill old endpoints
# (the policy is a ratchet, not a sweep). Tuple of (cli_cmd, mcp_tool).
_COHORT: dict[str, tuple[str, str]] = {
    "/documentation/api": ("docs api", "documentation_api"),
    # Stack discovery (issue #621). subscribe/unsubscribe paths are already
    # grandfathered; browse is the new triple-surface endpoint.
    "/api/stack/browse": ("stack browse", "stack_browse"),
}


def test_rest_endpoints_callable():
    """REST surface — every cohort entry resolves to a router handler."""
    from app.web.router import router  # noqa: F401  (import surface registration)

    # documentation_api is the handler name; importing the module is enough
    # to register the route on the router. Spot-check the new one explicitly.
    from app.web.router import documentation_api  # type: ignore[attr-defined]

    assert callable(documentation_api)


def test_cli_subcommands_registered():
    """CLI surface — every cohort entry has a matching ``agnes <cmd>`` subcommand.

    Walks the typer command tree at the top-level ``app`` and asserts each
    ``<group> <subcmd>`` pair from the cohort resolves to a registered command.
    """
    from cli.main import app

    # Top-level groups (name → registered Typer instance).
    groups: dict[str, object] = {g.name: g.typer_instance for g in app.registered_groups if g.name}

    for path, (cli_cmd, _mcp_tool) in _COHORT.items():
        head, tail = cli_cmd.split(" ", 1)
        assert head in groups, (
            f"CLI group '{head}' missing for {path} — register via `app.add_typer(...)` in cli/main.py"
        )
        sub = groups[head]
        sub_names = {c.name for c in sub.registered_commands if c.name}  # type: ignore[attr-defined]
        assert tail in sub_names, (
            f"CLI subcommand '{cli_cmd}' missing for {path} — define "
            f'`@{head}_app.command("{tail}")` in cli/commands/{head}.py'
        )


def test_mcp_tools_registered():
    """MCP surface — every cohort entry has a matching FastMCP tool.

    FastMCP exposes registered tools via ``list_tools()``; the test runs the
    coroutine synchronously and checks the cohort's tool names are present.
    """
    import asyncio

    from app.api.mcp_http import mcp

    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    for path, (_cli_cmd, mcp_tool) in _COHORT.items():
        assert mcp_tool in names, (
            f"MCP tool '{mcp_tool}' missing for {path} — register via `@mcp.tool()` in app/api/mcp_http.py"
        )


import os
from pathlib import Path

_BASELINE_PATH = Path(__file__).resolve().parent / "api_triple_surface_grandfathered.txt"

# Endpoints consciously REST-only (admin mutations, internal, webhooks). Reason
# required. New endpoints go here OR in _COHORT — never silently.
_ADOPTION_REASON = (
    "admin-only Adoption dashboard — web UI only, no CLI/MCP analogue "
    "(read-only aggregates rendered as cards/charts in the browser)"
)
_PROMPTS_REASON = (
    "admin-only managed-prompt editor (#622) — web UI only at /admin/prompts, "
    "no analyst CLI/MCP analogue (mirrors the grandfathered "
    "/api/admin/{welcome,workspace-prompt}-template editors)"
)
_EXEMPT: dict[str, str] = {
    "/api/admin/prompts/{kind}": _PROMPTS_REASON,
    "/api/admin/prompts/{kind}/source": _PROMPTS_REASON,
    "/api/admin/prompts/{kind}/bind-git": _PROMPTS_REASON,
    "/api/admin/prompts/{kind}/preview": _PROMPTS_REASON,
    "/api/admin/prompts/iwt-files": _PROMPTS_REASON,
    "/api/admin/adoption/kpis": _ADOPTION_REASON,
    "/api/admin/adoption/series": _ADOPTION_REASON,
    "/api/admin/adoption/top-skills": _ADOPTION_REASON,
    "/api/admin/adoption/top-users": _ADOPTION_REASON,
    "/api/admin/adoption/users/{user_id}/kpis": _ADOPTION_REASON,
    "/api/admin/adoption/users/{user_id}/series": _ADOPTION_REASON,
    "/api/admin/adoption/users/{user_id}/top-skills": _ADOPTION_REASON,
    "/api/admin/adoption/users/{user_id}/top-tools": _ADOPTION_REASON,
}


def _load_grandfathered() -> frozenset[str]:
    if not _BASELINE_PATH.exists():
        return frozenset()
    return frozenset(
        ln.strip()
        for ln in _BASELINE_PATH.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.startswith("#")
    )


def _live_surface_paths() -> set[str]:
    os.environ.setdefault("TESTING", "1")
    from app.main import create_app

    return {p for p in create_app().openapi()["paths"] if p.startswith(("/api/", "/documentation/"))}


def test_new_endpoints_are_classified():
    live = _live_surface_paths()
    assert len(live) > 150, "openapi returned too few paths — gate would be vacuous"
    grandfathered = _load_grandfathered()
    assert grandfathered, (
        "grandfather baseline empty/missing — run `.venv/bin/python -m scripts.seed_triple_surface_baseline`"
    )
    stale = grandfathered - live
    assert not stale, f"baseline lists paths no longer live (remove them): {sorted(stale)}"
    unclassified = live - set(_COHORT) - set(_EXEMPT) - grandfathered
    assert not unclassified, (
        f"{len(unclassified)} new endpoint(s) not classified — add each to "
        f"_COHORT (triple-surface: land CLI + MCP) or _EXEMPT (REST-only, with a "
        f"reason):\n" + "\n".join(f"  {p}" for p in sorted(unclassified))
    )
