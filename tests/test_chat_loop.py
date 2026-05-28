"""Multi-turn tool-use loop tests using a fake Anthropic client.

The fake mimics the SDK's ``messages.stream(...)`` async context manager,
yielding canned token deltas and a final message with the requested
content blocks. We use this to drive the loop deterministically without
hitting the real API.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any

import duckdb
import pytest

from src.db import _ensure_schema
from app.chat.loop import ChatTurnConfig, run_turn, MAX_TOOL_ITERATIONS


# --------------------------------------------------------------------------- #
# Fake Anthropic client
# --------------------------------------------------------------------------- #


@dataclass
class _FakeBlock:
    type: str
    text: str = ""
    name: str = ""
    id: str = ""
    input: dict = field(default_factory=dict)


@dataclass
class _FakeUsage:
    input_tokens: int = 5
    output_tokens: int = 3


@dataclass
class _FakeMessage:
    content: list
    usage: _FakeUsage = field(default_factory=_FakeUsage)
    stop_reason: str = "end_turn"


class _FakeStreamContext:
    """Async context manager mimicking client.messages.stream(...)."""

    def __init__(self, text_chunks: list[str], final_message: _FakeMessage):
        self._text_chunks = text_chunks
        self._final_message = final_message

    async def __aenter__(self):
        async def _gen():
            for chunk in self._text_chunks:
                yield chunk
        self.text_stream = _gen()
        return self

    async def __aexit__(self, *exc):
        return None

    async def get_final_message(self):
        return self._final_message


class _FakeMessages:
    def __init__(self, scripted_turns: list[tuple[list[str], _FakeMessage]]):
        # Each call to .stream(...) consumes the next scripted turn.
        self._turns = list(scripted_turns)
        self.calls = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        if not self._turns:
            raise RuntimeError("scripted turns exhausted")
        chunks, msg = self._turns.pop(0)
        return _FakeStreamContext(chunks, msg)


class _FakeClient:
    def __init__(self, scripted_turns: list[tuple[list[str], _FakeMessage]]):
        self.messages = _FakeMessages(scripted_turns)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def conn():
    c = duckdb.connect(":memory:")
    _ensure_schema(c)
    return c


@pytest.fixture
def user():
    return {"id": "alice", "email": "alice@example.com"}


def _collect(coro_gen):
    async def _run():
        out = []
        async for ev in coro_gen:
            out.append(ev)
        return out
    return asyncio.run(_run())


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


class TestSingleTurn:
    def test_terminal_text_only(self, conn, user):
        """No tool calls — the model returns plain text. The loop should
        emit token deltas + one assistant_message + done."""
        client = _FakeClient([(
            ["Hello", " there"],
            _FakeMessage(content=[_FakeBlock(type="text", text="Hello there")]),
        )])
        events = _collect(run_turn(
            client=client,
            config=ChatTurnConfig(model="test-model"),
            history=[],
            user_message="hi",
            user=user,
            conn=conn,
        ))
        types = [e["type"] for e in events]
        assert types == ["token", "token", "assistant_message", "done"]
        assert events[-2]["content"] == "Hello there"
        assert events[-2]["usage"]["input_tokens"] == 5
        assert events[-2]["usage"]["output_tokens"] == 3


class TestToolUseLoop:
    def test_tool_call_then_answer(self, conn, user):
        """Turn 1: model calls list_catalog. Turn 2: model answers with text."""
        turn1 = (
            [],
            _FakeMessage(
                content=[
                    _FakeBlock(
                        type="tool_use",
                        name="list_catalog",
                        id="tu_1",
                        input={},
                    ),
                ],
                stop_reason="tool_use",
            ),
        )
        turn2 = (
            ["You have", " no tables"],
            _FakeMessage(content=[_FakeBlock(type="text", text="You have no tables")]),
        )
        client = _FakeClient([turn1, turn2])
        events = _collect(run_turn(
            client=client,
            config=ChatTurnConfig(model="test-model"),
            history=[],
            user_message="what tables?",
            user=user,
            conn=conn,
        ))
        types = [e["type"] for e in events]
        # Expected order: assistant_message (turn1 with tool_use) → tool_call →
        # tool_result → tokens (turn2) → assistant_message (turn2) → done.
        assert types == [
            "assistant_message",  # turn1 with tool_use
            "tool_call",
            "tool_result",
            "token", "token",
            "assistant_message",  # terminal
            "done",
        ]
        # The tool_call payload was list_catalog.
        tool_call_ev = next(e for e in events if e["type"] == "tool_call")
        assert tool_call_ev["tool"] == "list_catalog"
        # The tool_result was ok (returns empty list of tables on a fresh DB).
        tool_result_ev = next(e for e in events if e["type"] == "tool_result")
        assert tool_result_ev["ok"]
        assert "tables" in tool_result_ev["result"]

    def test_tool_call_with_error_passes_message_to_model(self, conn, user):
        """A failing tool returns ok=False but the loop continues — the
        next assistant turn sees the error in its tool_result block."""
        turn1 = (
            [],
            _FakeMessage(
                content=[_FakeBlock(
                    type="tool_use",
                    name="get_schema",
                    id="tu_1",
                    input={"table_id": "nonexistent"},
                )],
                stop_reason="tool_use",
            ),
        )
        turn2 = (
            ["Sorry"],
            _FakeMessage(content=[_FakeBlock(type="text", text="Sorry")]),
        )
        client = _FakeClient([turn1, turn2])
        events = _collect(run_turn(
            client=client,
            config=ChatTurnConfig(model="test-model"),
            history=[],
            user_message="schema?",
            user=user,
            conn=conn,
        ))
        tool_result_ev = next(e for e in events if e["type"] == "tool_result")
        assert not tool_result_ev["ok"]
        # Order of checks: access first (doesn't leak existence), then
        # registry. Either way the loop should see a non-empty error.
        assert tool_result_ev["result"]["error"]


class TestRunawayProtection:
    def test_max_iterations_triggers_error(self, conn, user):
        """If the model never stops calling tools, the loop bails after
        MAX_TOOL_ITERATIONS with an error event."""
        # Every turn is a tool_use → 12 turns produced, then the loop bails.
        forever_tool = _FakeMessage(
            content=[_FakeBlock(
                type="tool_use", name="list_catalog", id="tu_1", input={},
            )],
            stop_reason="tool_use",
        )
        client = _FakeClient([([] , forever_tool)] * (MAX_TOOL_ITERATIONS + 1))
        events = _collect(run_turn(
            client=client,
            config=ChatTurnConfig(model="test-model"),
            history=[],
            user_message="loop",
            user=user,
            conn=conn,
        ))
        types = [e["type"] for e in events]
        assert types.count("assistant_message") == MAX_TOOL_ITERATIONS
        assert types[-1] == "error"
        assert "MAX_TOOL_ITERATIONS" in events[-1]["error"]


class TestModelFailure:
    def test_model_call_exception_yields_error_event(self, conn, user):
        class _BoomClient:
            class messages:
                @staticmethod
                def stream(**kwargs):
                    raise RuntimeError("API down")
        events = _collect(run_turn(
            client=_BoomClient(),
            config=ChatTurnConfig(model="test-model"),
            history=[],
            user_message="hi",
            user=user,
            conn=conn,
        ))
        assert events[-1]["type"] == "error"
        assert "API down" in events[-1]["error"]
