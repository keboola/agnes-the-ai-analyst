"""Shared helpers for the Agnes MCP servers (HTTP foundation + CLI stdio).

Two concerns, both about keeping MCP tool traffic inside a model's context
budget:

- ``summarize_docstring`` / ``progressive_tool`` — ship only the first
  docstring paragraph as the wire description on ``tools/list``; the full
  docstring stays available on demand via each server's ``tool_docs`` tool.
- ``ensure_output_size`` — hard cap on serialized tool output; over the cap
  the tool raises with actionable narrowing guidance instead of returning a
  payload that would flood the model's context.
"""

from __future__ import annotations

import inspect
import json
import os
from typing import Any, Callable, MutableMapping

DEFAULT_MAX_OUTPUT_CHARS = 100_000
MAX_OUTPUT_CHARS_ENV = "AGNES_MCP_MAX_OUTPUT_CHARS"

QUERY_NARROW_HINT = "select specific columns, add a WHERE filter, lower `limit`, or aggregate server-side"


class MCPOutputTooLarge(ValueError):
    """Serialized tool output exceeded the configured cap."""


def max_output_chars() -> int:
    """Resolve the output cap: env override, else default. ``0`` disables."""
    raw = os.environ.get(MAX_OUTPUT_CHARS_ENV, "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return DEFAULT_MAX_OUTPUT_CHARS


def ensure_output_size(
    payload: Any,
    tool_name: str,
    *,
    hint: str = QUERY_NARROW_HINT,
    cap: int | None = None,
) -> Any:
    """Return ``payload`` unless its serialized size exceeds the cap.

    Raises :class:`MCPOutputTooLarge` with an actionable message instead of
    returning an oversized payload — no partial data, so an agent never
    computes over a silently-incomplete result.
    """
    effective = max_output_chars() if cap is None else cap
    if effective <= 0:
        return payload
    size = len(json.dumps(payload, default=str))
    if size > effective:
        raise MCPOutputTooLarge(
            f"{tool_name} response is ~{size:,} chars, over the {effective:,}-char "
            f"output cap. Narrow the request: {hint}."
        )
    return payload


def summarize_docstring(doc: str | None) -> tuple[str, bool]:
    """Return ``(first paragraph as one line, has_more_content)``."""
    cleaned = inspect.cleandoc(doc or "").strip()
    if not cleaned:
        return "", False
    first, _sep, rest = cleaned.partition("\n\n")
    summary = " ".join(line.strip() for line in first.splitlines())
    return summary, bool(rest.strip())


def progressive_tool(mcp: Any, docs_registry: MutableMapping[str, str]) -> Callable[[], Callable[[Callable], Callable]]:
    """Drop-in replacement factory for ``@mcp.tool()``.

    ``tool = progressive_tool(mcp, registry)`` then ``@tool()`` registers the
    function with only its first docstring paragraph as the wire description
    (plus a ``tool_docs`` pointer when the docstring has more), and stores the
    full docstring in ``docs_registry`` for the on-demand ``tool_docs`` tool.
    The decorated function is returned unchanged, matching ``FastMCP.tool``.
    """

    def tool(read_only: bool = False) -> Callable[[Callable], Callable]:
        """``read_only=True`` advertises the standard MCP ``readOnlyHint``
        annotation on the tool.

        It is a **hint to callers, not an authorization decision** — RBAC is
        enforced by the endpoint the tool calls either way. What it buys is
        that an agent harness can auto-approve the call instead of stopping to
        ask: a client with no annotation has to treat every tool as a possible
        mutation, which on a surface with no approval UI means the call waits
        out the approval timeout and the tool is effectively unusable.

        Mark a tool only when it cannot mutate anything. Over-marking a write
        tool silently removes a human gate; under-marking only costs a prompt.
        """

        def decorate(fn: Callable) -> Callable:
            full = inspect.cleandoc(fn.__doc__ or "").strip()
            summary, has_more = summarize_docstring(full)
            description = summary or fn.__name__
            if has_more:
                description += f" Full contract: tool_docs('{fn.__name__}')."
            docs_registry[fn.__name__] = full
            if read_only:
                from mcp.types import ToolAnnotations

                return mcp.tool(
                    description=description,
                    annotations=ToolAnnotations(readOnlyHint=True),
                )(fn)
            return mcp.tool(description=description)(fn)

        return decorate

    return tool
