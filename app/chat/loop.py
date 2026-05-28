"""Anthropic tool-use loop for the chat agent.

The loop streams a single user turn into:

  - 0..N ``tool_use`` blocks (assistant) → handler invocations →
    ``tool_result`` blocks (user)
  - 1 terminal assistant ``text`` block once the model stops calling
    tools.

Events are yielded as plain dicts. ``app/api/chat.py`` converts them
to SSE frames; tests consume them directly.

Streaming model: we call ``messages.stream`` per turn and accumulate the
final assistant message (text + tool_use blocks). The Anthropic SDK
exposes ``stream.text_stream`` for token-level deltas; we forward each
delta as a ``token`` event so the UI sees the answer materialize in
real time, then re-emit the full block for persistence.

We use a hard cap on tool-use iterations (``MAX_TOOL_ITERATIONS``) so
a runaway tool-loop can never burn an unbounded number of model calls.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional

import duckdb

from .prompts import SYSTEM_PROMPT
from .tools import TOOL_DEFINITIONS, dispatch

logger = logging.getLogger(__name__)


MAX_TOOL_ITERATIONS = 12
DEFAULT_MAX_TOKENS = 4096


@dataclass
class ChatTurnConfig:
    model: str
    max_tokens: int = DEFAULT_MAX_TOKENS
    system: str = SYSTEM_PROMPT


def _format_tool_use_summary(block: Any) -> dict[str, Any]:
    """Compact dict describing one tool_use block for persistence."""
    return {
        "tool": getattr(block, "name", ""),
        "id": getattr(block, "id", ""),
        "args": getattr(block, "input", {}) or {},
    }


async def run_turn(
    *,
    client: Any,
    config: ChatTurnConfig,
    history: list[dict[str, Any]],
    user_message: str,
    user: dict,
    conn: duckdb.DuckDBPyConnection,
) -> AsyncIterator[dict[str, Any]]:
    """Run one user turn end-to-end.

    Yields events:
      {"type": "token", "text": "..."}                                  per assistant token delta
      {"type": "tool_call", "tool": "...", "args": {...}}               about to invoke a tool
      {"type": "tool_result", "tool": "...", "result": {...}, "ok": bool}  tool returned
      {"type": "assistant_message", "content": "...", "tool_calls": [...], "usage": {...}}  per assistant turn
      {"type": "done"}                                                  final turn complete
      {"type": "error", "error": "..."}                                 fatal — loop aborted

    The caller is responsible for persisting the user message + every
    emitted ``assistant_message`` and ``tool_result`` block.
    """
    # Start from caller-provided history + the new user message. We don't
    # mutate ``history`` in place; the caller may want to keep its own copy.
    messages: list[dict[str, Any]] = list(history) + [
        {"role": "user", "content": user_message},
    ]

    for iteration in range(MAX_TOOL_ITERATIONS):
        try:
            async with client.messages.stream(
                model=config.model,
                max_tokens=config.max_tokens,
                system=config.system,
                tools=TOOL_DEFINITIONS,
                messages=messages,
            ) as stream:
                async for token in stream.text_stream:
                    if token:
                        yield {"type": "token", "text": token}
                final_message = await stream.get_final_message()
        except Exception as exc:
            logger.exception("chat: model call failed")
            yield {"type": "error", "error": f"model call failed: {exc}"}
            return

        # Persist the assistant turn (text + tool_use blocks).
        text_parts: list[str] = []
        tool_use_blocks: list[Any] = []
        for block in final_message.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(getattr(block, "text", ""))
            elif btype == "tool_use":
                tool_use_blocks.append(block)

        usage = {
            "input_tokens": getattr(final_message.usage, "input_tokens", None),
            "output_tokens": getattr(final_message.usage, "output_tokens", None),
        }

        yield {
            "type": "assistant_message",
            "content": "".join(text_parts),
            "tool_calls": [_format_tool_use_summary(b) for b in tool_use_blocks],
            "usage": usage,
            "stop_reason": getattr(final_message, "stop_reason", None),
        }

        # Append the assistant turn to messages for the next loop iteration.
        messages.append(
            {"role": "assistant", "content": _content_for_replay(final_message.content)}
        )

        if not tool_use_blocks:
            # Model produced a terminal answer — we're done.
            yield {"type": "done"}
            return

        # Run every tool_use in this turn (Anthropic packs many into one
        # assistant turn for batchable work) and feed them all back as a
        # single user turn containing one ``tool_result`` per id.
        tool_result_blocks: list[dict[str, Any]] = []
        for block in tool_use_blocks:
            tool_name = getattr(block, "name", "")
            tool_id = getattr(block, "id", "")
            tool_args = getattr(block, "input", {}) or {}
            yield {"type": "tool_call", "tool": tool_name, "args": tool_args}
            result = await dispatch(tool_name, tool_args, user, conn)
            yield {
                "type": "tool_result",
                "tool": tool_name,
                "ok": result.ok,
                "result": result.data,
            }
            tool_result_blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": _stringify_tool_result(result.data),
                    "is_error": not result.ok,
                }
            )
        messages.append({"role": "user", "content": tool_result_blocks})

    yield {
        "type": "error",
        "error": (
            f"reached MAX_TOOL_ITERATIONS={MAX_TOOL_ITERATIONS} without a "
            "terminal answer — the loop has been stopped to avoid runaway "
            "cost; rephrase the question or split it into smaller asks"
        ),
    }


def _content_for_replay(blocks: list[Any]) -> list[dict[str, Any]]:
    """Convert SDK content blocks into the dict shape the API expects when
    replaying as message history."""
    out: list[dict[str, Any]] = []
    for b in blocks:
        btype = getattr(b, "type", None)
        if btype == "text":
            out.append({"type": "text", "text": getattr(b, "text", "")})
        elif btype == "tool_use":
            out.append(
                {
                    "type": "tool_use",
                    "id": getattr(b, "id", ""),
                    "name": getattr(b, "name", ""),
                    "input": getattr(b, "input", {}) or {},
                }
            )
    return out


def _stringify_tool_result(data: dict[str, Any]) -> str:
    """Serialize a tool result for the ``tool_result.content`` field.

    Anthropic accepts either str or a list of content blocks. For
    simplicity (and because the LLM is good at reading JSON), we always
    emit a JSON-encoded string."""
    import json
    try:
        return json.dumps(data, ensure_ascii=False, default=str)
    except Exception:
        return repr(data)
