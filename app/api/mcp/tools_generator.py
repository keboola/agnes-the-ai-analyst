"""Generate outbound MCP tools from ``tool_registry``.

For each passthrough-mode tool registered in ``tool_registry``, register a
FastMCP tool on the given server that forwards the call to the upstream MCP
source. The exposed name + JSON input schema from the registry are surfaced
verbatim to AI clients, so the upstream tool's contract reaches Claude
Desktop / Cursor / Cline unchanged.

Materialize-mode tools are NOT registered here — they live as DuckDB tables
in ``analytics.duckdb`` and are reachable via the existing generic ``query``
and ``catalog`` tools from @mf's foundation.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from connectors.mcp.client import call_tool_async
from src.repositories import mcp_sources_repo, tool_registry_repo
from src.repositories.tool_registry import PASSTHROUGH

logger = logging.getLogger(__name__)


_JSON_TYPE_TO_PY = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "array": "list",
    "object": "dict",
}


_PY_IDENT_RE = __import__("re").compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_ident(name: str) -> bool:
    """Reject anything that isn't a safe Python identifier — keeps exec() safe."""
    # Reserved words would shadow Python builtins (def, class, lambda, ...).
    return bool(_PY_IDENT_RE.match(name)) and not __import__("keyword").iskeyword(name)


def _make_passthrough_callable(
    source: Dict[str, Any],
    original_name: str,
    input_schema: Optional[Dict[str, Any]],
    caller_id_fn: Optional[Callable[[], Optional[str]]] = None,
) -> Callable[..., Any]:
    """Build an async callable forwarding to the upstream MCP tool.

    FastMCP infers its validation Pydantic model from the function signature,
    so a single ``**kwargs`` wrapper makes every call fail validation. We
    materialize a real signature from ``input_schema.properties`` via exec()
    — required props are positional, optional get ``= None`` defaults.

    Property names from the upstream schema are validated against a strict
    identifier regex before being inserted into the synthesized source; any
    non-identifier prop name falls back to ``**kwargs`` for that tool. This
    keeps the exec() call free of upstream-controlled syntax.
    """
    props: Dict[str, Dict[str, Any]] = (input_schema or {}).get("properties") or {}
    required = set((input_schema or {}).get("required") or [])

    safe_props = [name for name in props if _safe_ident(name)]
    fallback_kwargs = len(safe_props) != len(props)  # any unsafe key → use **kwargs

    # NOTE: only the genuine non-identifier case takes the **kwargs wrapper.
    # An EMPTY schema (no props) must fall through to the synthesized path
    # below, which emits a valid parameterless ``async def _passthrough():``.
    # Routing empty schemas here instead makes FastMCP render a *required*
    # ``kwargs`` field, so the only valid (empty) call 422s.
    if fallback_kwargs:

        async def _passthrough(**kwargs: Any) -> str:
            caller_user_id = caller_id_fn() if caller_id_fn else None
            result = await call_tool_async(source, original_name, arguments=kwargs, caller_user_id=caller_user_id)
            if result.is_error:
                raise RuntimeError(f"upstream tool {original_name} returned error: {result.text[:300]}")
            return result.text

        _passthrough.__name__ = original_name
        _passthrough.__doc__ = f"Passthrough to upstream MCP tool {original_name!r}."
        return _passthrough

    # Required params must precede optional ones in the synthesized
    # signature — Python rejects `def f(opt=None, req):` as
    # SyntaxError ("non-default argument follows default argument"),
    # and the SyntaxError lands at `exec()` below, OUTSIDE the per-tool
    # try/except that wraps `add_tool` — so a single upstream schema
    # listing an optional property before a required one would crash
    # the entire `register_passthrough_tools` loop and lose every
    # passthrough tool, not just the one with the bad property order.
    # Two-pass build keeps the synthesized callable parseable
    # regardless of upstream property ordering.
    # (Devin Review on #474)
    sig_parts: List[str] = []
    for name in safe_props:
        if name in required:
            sig_parts.append(name)
    for name in safe_props:
        if name not in required:
            sig_parts.append(f"{name}=None")
    sig_str = ", ".join(sig_parts)
    arg_dict_items = ", ".join(f'"{n}": {n}' for n in safe_props)

    src = (
        f"async def _passthrough({sig_str}):\n"
        f"    raw = {{{arg_dict_items}}}\n"
        f"    args = {{k: v for k, v in raw.items() if v is not None}}\n"
        f"    result = await __forward(args)\n"
        f"    if result.is_error:\n"
        f"        raise RuntimeError('upstream returned error: ' + result.text[:300])\n"
        f"    return result.text\n"
    )

    async def _forward(args: Dict[str, Any]):
        caller_user_id = caller_id_fn() if caller_id_fn else None
        return await call_tool_async(source, original_name, arguments=args, caller_user_id=caller_user_id)

    namespace: Dict[str, Any] = {"__forward": _forward}
    exec(src, namespace)  # noqa: S102 - synthesized source only references vetted idents
    fn = namespace["_passthrough"]
    fn.__name__ = original_name
    fn.__doc__ = f"Passthrough to upstream MCP tool {original_name!r}."
    return fn


def register_passthrough_tools(
    mcp_instance: FastMCP,
    caller_id_fn: Optional[Callable[[], Optional[str]]] = None,
) -> List[str]:
    """Register every enabled passthrough tool from ``tool_registry`` on ``mcp_instance``.

    ``caller_id_fn`` resolves the current request's caller user id at tool-call
    time. Each transport (SSE / Streamable-HTTP) passes its own resolver so
    the synthesized closures — registered once at startup — can still forward
    per-request identity into ``call_tool_async``. Without it, a
    ``scope='per_user'`` source would see ``caller_user_id=None`` (the
    caller-less materialize signal) and resolve to the shared credential for
    every authenticated caller. Omit it only where no per-request caller
    exists.

    Returns the list of exposed tool names that were registered (useful for
    logging and tests).
    """
    sources_repo = mcp_sources_repo()
    tools_repo = tool_registry_repo()

    registered: List[str] = []
    for tool in tools_repo.list_by_mode(PASSTHROUGH, enabled_only=True):
        source = sources_repo.get(tool["source_id"])
        if source is None or not source.get("enabled", True):
            logger.warning(
                "passthrough %s skipped — source %s missing/disabled", tool["exposed_name"], tool["source_id"]
            )
            continue

        input_schema = tool.get("input_schema") if isinstance(tool.get("input_schema"), dict) else None
        fn = _make_passthrough_callable(source, tool["original_name"], input_schema, caller_id_fn)
        description = tool.get("description") or f"Passthrough to {source['name']}.{tool['original_name']}"

        try:
            mcp_instance.add_tool(fn, name=tool["exposed_name"], description=description)
        except Exception:
            logger.exception("failed to register passthrough tool %s", tool["exposed_name"])
            continue

        # Surface the upstream's JSON schema verbatim — covers cases where
        # the schema has descriptions/enums/constraints richer than what we
        # can express through plain Python parameter defaults.
        if input_schema:
            registered_tool = mcp_instance._tool_manager.get_tool(tool["exposed_name"])
            if registered_tool is not None:
                try:
                    registered_tool.parameters = input_schema
                except Exception:
                    logger.exception("could not patch parameters for %s", tool["exposed_name"])

        registered.append(tool["exposed_name"])

    if registered:
        logger.info("registered %d passthrough MCP tools: %s", len(registered), ", ".join(registered))
    return registered
