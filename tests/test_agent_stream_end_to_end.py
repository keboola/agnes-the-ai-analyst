"""Runner frame -> AG-UI mapper -> SSE bytes -> CLI reader -> printed answer.

Every link in this chain had tests and every one of them passed while
`agnes chat <slug> --once` printed "(no answer)" on a turn that had
answered. The server tests fed the mapper a frame the runner does not
emit; the CLI tests fed the reader events the mapper does not produce.
Both halves were self-consistent and neither touched the other, so a
field-name mismatch at the seam was invisible.

This file starts from the frame shape `app/chat/runner.py` actually emits
and ends at the string `_send_turn` hands the user, crossing every layer
in between with no mock in the middle. It fails on a regression in any of
them.
"""

from __future__ import annotations

import httpx
import pytest

import cli.client as client_mod
from app.api.agent_sse import SSE_TERMINAL_TYPES, frame_to_agui, sse_bytes


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    yield tmp_path


#: Exactly what a text-only turn puts on the bus. `token` carries `text`
#: (`runner.py`: `_emit({"type": "token", "text": piece})`); the trailing
#: `assistant_message` carries `content`. Getting these two mixed up is
#: the bug this file exists for.
RUNNER_FRAMES = [
    {"type": "ready"},
    {"type": "token", "text": "Monthly "},
    {"type": "token", "text": "Recurring "},
    {"type": "token", "text": "Revenue."},
    {"type": "assistant_message", "content": "Monthly Recurring Revenue."},
    {"type": "done"},
]

#: What a turn with NO incremental streaming puts on the bus — verified
#: against `app/chat/runner.py::_fake_agent_loop` (the `AGNES_RUNNER_FAKE_AGENT=1`
#: path every non-`real_llm` chat/e2e test runs against, per
#: `tests/e2e/test_agnes_cli_via_chat.py`'s own module docstring: "Fake-agent's
#: `echo:` reply doesn't exercise that decision"). It answers by emitting a
#: single `assistant_message` frame straight away — no `token` frame ever
#: precedes it, so no `TEXT_MESSAGE_CONTENT` delta ever reaches the CLI. The
#: real runner's own idle-watchdog partial-save path
#: (`_run_turn`, `if partial: _emit({"type": "assistant_message", ...})`)
#: can hit the same shape when a turn times out before its first delta.
NO_DELTA_RUNNER_FRAMES = [
    {"type": "ready"},
    {"type": "assistant_message", "content": "echo: hi", "tokens_in": 1, "tokens_out": 1, "model": "fake"},
    {"type": "done"},
]


#: What the real runner's idle-watchdog puts on the bus when a turn wedges
#: (`app/chat/runner.py` — the `turn_idle_timeout` branch): the partial-save
#: `assistant_message` first, then the `error` frame, then the outer loop's
#: `done`. The order is load-bearing, not incidental — `error` maps to
#: RUN_ERROR, which `_event_stream` treats as terminal, so a partial emitted
#: AFTER it would be dropped server-side and never reach any client.
#: `tests/test_chat_runner.py::test_idle_watchdog_interrupts_a_wedged_turn`
#: pins the emission order at the source.
WATCHDOG_RUNNER_FRAMES = [
    {"type": "ready"},
    {"type": "assistant_message", "content": "Counting tables so far: 12", "model": "fake"},
    {
        "type": "error",
        "kind": "turn_idle_timeout",
        "message": "no agent activity for 300s; interrupting the turn (a tool call is likely stuck)",
    },
    {"type": "done"},
]


def _wire_bytes(frames) -> bytes:
    """Serialize frames the way `app/api/agent_sessions.py::_event_stream` does.

    Including its stop rule: the generator breaks out of the drain loop the
    moment it yields a terminal event (`SSE_TERMINAL_TYPES` — RUN_FINISHED
    or RUN_ERROR), so frames queued behind one never make it onto the wire.
    Serializing them anyway would let this file "prove" a delivery the real
    server cannot perform.
    """
    out = b""
    for seq, frame in enumerate(frames, start=1):
        event = frame_to_agui(frame)
        if event is None:
            continue
        out += sse_bytes(event, f"sess-e2e:{seq}")
        if event["type"] in SSE_TERMINAL_TYPES:
            break
    return out


def _install_wire(monkeypatch, body: bytes) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    def fake_get_client(timeout=30.0):
        return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")

    monkeypatch.setattr(client_mod, "get_client", fake_get_client)


def test_a_streamed_turn_reaches_the_user_as_text(monkeypatch, capsys):
    from cli.commands.chat import _send_turn

    _install_wire(monkeypatch, _wire_bytes(RUNNER_FRAMES))
    result = _send_turn("sess-e2e", "What is MRR?", live_render=True)

    # The whole point: the deltas assembled into the answer.
    assert result.answer == "Monthly Recurring Revenue."
    assert result.error is None
    assert not result.cancelled
    # ...and the user saw it stream, rather than a silent turn.
    assert "Monthly Recurring Revenue." in capsys.readouterr().out


def test_the_once_path_would_not_print_no_answer(monkeypatch):
    """`--once` prints "(no answer)" exactly when `.answer` is falsy."""
    from cli.commands.chat import _send_turn

    _install_wire(monkeypatch, _wire_bytes(RUNNER_FRAMES))
    assert _send_turn("sess-e2e", "What is MRR?", live_render=False).answer


def test_a_turn_with_no_streamed_deltas_still_reaches_the_user_as_text(monkeypatch, capsys):
    """HIGH: a turn that never streams a `token`/`TEXT_MESSAGE_CONTENT` delta
    — the fake-agent `echo:` path, and the real runner's idle-watchdog
    partial-save — still carries the answer on the trailing
    `assistant_message`/`TEXT_MESSAGE_END` frame. `_send_turn` must read it
    from there instead of reporting "(no answer)" on a turn that answered.
    """
    from cli.commands.chat import _send_turn

    _install_wire(monkeypatch, _wire_bytes(NO_DELTA_RUNNER_FRAMES))
    result = _send_turn("sess-e2e", "hi", live_render=True)

    assert result.answer == "echo: hi"
    assert result.error is None
    # The whole point of --once: the answer must actually appear on screen,
    # not just live on `.answer` for an internal check to swallow.
    assert "echo: hi" in capsys.readouterr().out


def test_a_tool_using_turn_still_yields_its_text(monkeypatch):
    """Tool frames travel the same wire and must not eat the answer."""
    from cli.commands.chat import _send_turn

    frames = [
        {"type": "ready"},
        {"type": "tool_call", "tool": "bash", "args": {"command": "agnes catalog"}},
        {"type": "tool_result", "tool_use_id": "t1", "result": "…"},
        {"type": "token", "text": "Answer after a tool."},
        {"type": "done"},
    ]
    _install_wire(monkeypatch, _wire_bytes(frames))
    result = _send_turn("sess-e2e", "look it up", live_render=False)
    assert result.answer == "Answer after a tool."


def test_the_chain_carries_the_tool_name_the_runner_named(monkeypatch):
    """`tool` -> `name` is the same class of rename as `text` -> `delta`."""
    from cli.commands.chat import _send_turn

    frames = [
        {"type": "ready"},
        {"type": "tool_call", "tool": "bash", "args": {"command": "ls"}},
        {"type": "done"},
    ]
    _install_wire(monkeypatch, _wire_bytes(frames))
    result = _send_turn("sess-e2e", "run it", live_render=False)
    starts = [e for e in result.events if e.get("type") == "TOOL_CALL_START"]
    assert [e["name"] for e in starts] == ["bash"]


def test_a_wedged_turns_partial_answer_survives_the_error(monkeypatch, capsys):
    """HIGH: the idle watchdog's partial-save must reach the analyst.

    A turn the watchdog interrupts still produced text, and that text is
    the only thing the analyst gets out of the minutes they waited. It
    rides the wire only if the runner emits it BEFORE the `error` frame:
    RUN_ERROR is terminal, so `_event_stream` closes the response right
    after it (`_wire_bytes` above mirrors that stop rule). Emitted after,
    the partial is dropped server-side and no client-side drain can
    recover it.
    """
    from cli.commands.chat import _send_turn

    _install_wire(monkeypatch, _wire_bytes(WATCHDOG_RUNNER_FRAMES))
    result = _send_turn("sess-e2e", "count tables", live_render=True)

    assert result.answer == "Counting tables so far: 12"
    assert result.error is not None and "no agent activity" in result.error
    assert "Counting tables so far: 12" in capsys.readouterr().out


def test_an_error_frame_surfaces_as_a_turn_error(monkeypatch):
    from cli.commands.chat import _send_turn

    frames = [
        {"type": "ready"},
        {"type": "error", "kind": "turn_idle_timeout", "message": "no agent activity for 300s"},
    ]
    _install_wire(monkeypatch, _wire_bytes(frames))
    result = _send_turn("sess-e2e", "hang", live_render=False)
    assert result.error == "no agent activity for 300s"
