"""In-subprocess entrypoint. Runs claude-agent-sdk inside the chat sandbox.

Stdin: JSON lines, one per frame. Inbound types: user_msg, cancel.
Stdout: JSON lines. Outbound types: runner_ready, token, tool_call,
        tool_result, assistant_message, error, done.

Env (set by ChatManager via SubprocessProvider):
- AGNES_SESSION_ID, AGNES_USER_EMAIL, AGNES_API, AGNES_TOKEN
- AGNES_DAILY_BUDGET_USD, AGNES_PER_TOOL_CALL_SECONDS
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path


def _emit(frame: dict) -> None:
    sys.stdout.write(json.dumps(frame) + "\n")
    sys.stdout.flush()


async def _stdin_lines() -> "asyncio.Queue[dict]":
    queue: asyncio.Queue[dict] = asyncio.Queue()

    async def reader() -> None:
        loop = asyncio.get_running_loop()
        reader_obj = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader_obj)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)
        while True:
            line = await reader_obj.readline()
            if not line:
                await queue.put({"type": "_eof"})
                return
            try:
                await queue.put(json.loads(line))
            except json.JSONDecodeError:
                continue

    asyncio.create_task(reader())
    return queue


async def _fake_agent_loop(
    queue: "asyncio.Queue[dict]",
    *,
    per_tool_seconds: float = 90.0,
) -> None:
    """Used by tests via AGNES_RUNNER_FAKE_AGENT=1. Echoes user_msg back.

    Special messages:
    - ``__slow_tool__`` — simulates a tool call that exceeds the per-tool
      wall-clock cap. Emits ``tool_call`` then, after timeout, emits a
      synthetic ``tool_result: {timeout: true}``.
    """
    while True:
        frame = await queue.get()
        if frame.get("type") == "_eof":
            return
        if frame.get("type") == "user_msg":
            text = frame.get("text", "")
            if text == "__slow_tool__":
                _emit({"type": "tool_call", "tool": "run_query", "args": {"sql": "..."}})
                try:
                    await asyncio.wait_for(
                        asyncio.sleep(per_tool_seconds + 5),
                        timeout=per_tool_seconds,
                    )
                except asyncio.TimeoutError:
                    _emit({
                        "type": "tool_result",
                        "tool": "run_query",
                        "result": {"timeout": True},
                    })
                continue
            _emit({
                "type": "assistant_message",
                "content": f"echo: {text}",
                "tokens_in": 1,
                "tokens_out": 1,
                "model": "fake",
            })


async def _real_agent_loop(queue: "asyncio.Queue[dict]", workdir: Path) -> None:
    """Real claude-agent-sdk-backed loop.

    Per-tool wall-clock cap (Phase 12.2): the fake-agent path enforces
    AGNES_PER_TOOL_CALL_SECONDS via asyncio.wait_for in _fake_agent_loop.
    For the real SDK path, tool dispatch is handled inside ClaudeSDKClient
    (agnes receives tool_call/tool_result frames, not raw coroutines), so
    per-tool wrapping is not straightforward at this boundary. A simpler
    wall-clock timeout is applied at the whole-turn level: if
    receive_response() takes longer than per_tool_seconds * max_tools_per_turn,
    the connection is interrupted. Full per-tool granularity requires either
    an SDK API that exposes individual tool dispatch coroutines, or an
    out-of-process watchdog. TODO(Phase 12.2): revisit when claude-agent-sdk
    exposes a per-tool hook or run_tool() coroutine.

    Uses ClaudeSDKClient for persistent-session bidirectional communication:
    - connect() once with the first user_msg
    - query() for each subsequent user_msg
    - receive_response() (async-iter) to consume each turn's messages
    - interrupt() for cancel frames

    Message type mapping (SDK → outbound JSON frames):
    - AssistantMessage with TextBlock content → token frames + assistant_message at turn end
    - AssistantMessage with ToolUseBlock content → tool_call frame
    - AssistantMessage with ToolResultBlock content → tool_result frame
    - ResultMessage → assistant_message frame (turn end, carries usage/model)
    """
    from claude_agent_sdk import (  # type: ignore[import-untyped]
        AssistantMessage,
        ClaudeSDKClient,
        ResultMessage,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
    )

    async with ClaudeSDKClient() as client:
        # Flag to track whether we've called connect() yet
        connected = False

        while True:
            frame = await queue.get()
            t = frame.get("type")

            if t == "_eof":
                return

            if t == "cancel":
                client.interrupt()
                continue

            if t != "user_msg":
                continue

            text = frame.get("text", "")

            # First message: connect; subsequent messages: query
            if not connected:
                await client.connect(text)
                connected = True
            else:
                await client.query(text)

            # Consume the response for this turn
            collected_text: list[str] = []
            tokens_in = 0
            tokens_out = 0
            model = ""

            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            _emit({"type": "token", "text": block.text})
                            collected_text.append(block.text)
                        elif isinstance(block, ToolUseBlock):
                            _emit({
                                "type": "tool_call",
                                "tool": block.name,
                                "args": block.input,
                            })
                        elif isinstance(block, ToolResultBlock):
                            result = block.content
                            if isinstance(result, list):
                                result = " ".join(
                                    item.get("text", "") if isinstance(item, dict) else str(item)
                                    for item in result
                                )
                            _emit({
                                "type": "tool_result",
                                "tool": block.tool_use_id,
                                "result": result,
                            })
                    model = msg.model
                    if msg.usage:
                        tokens_in += msg.usage.get("input_tokens", 0)
                        tokens_out += msg.usage.get("output_tokens", 0)

                elif isinstance(msg, ResultMessage):
                    if msg.usage:
                        tokens_in = msg.usage.get("input_tokens", tokens_in)
                        tokens_out = msg.usage.get("output_tokens", tokens_out)
                    # ResultMessage signals turn end; receive_response() stops after it
                    _emit({
                        "type": "assistant_message",
                        "content": "".join(collected_text),
                        "tokens_in": tokens_in,
                        "tokens_out": tokens_out,
                        "model": model,
                    })


async def amain() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    args = parser.parse_args()

    workdir = Path(os.environ.get("AGNES_WORKDIR", os.getcwd()))

    _emit({"type": "runner_ready"})
    queue = await _stdin_lines()

    per_tool = float(os.environ.get("AGNES_PER_TOOL_CALL_SECONDS", "90"))
    if os.environ.get("AGNES_RUNNER_FAKE_AGENT") == "1":
        await _fake_agent_loop(queue, per_tool_seconds=per_tool)
    else:
        try:
            await _real_agent_loop(queue, workdir)
        except Exception as exc:
            _emit({"type": "error", "kind": "runner_exception", "message": str(exc)})
            raise


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
