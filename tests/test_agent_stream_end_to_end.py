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
from app.api.agent_sse import frame_to_agui, sse_bytes


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


def _wire_bytes(frames) -> bytes:
    """Serialize frames the way `app/api/agent_sessions.py::_event_stream` does."""
    out = b""
    for seq, frame in enumerate(frames, start=1):
        event = frame_to_agui(frame)
        if event is None:
            continue
        out += sse_bytes(event, f"sess-e2e:{seq}")
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


def test_an_error_frame_surfaces_as_a_turn_error(monkeypatch):
    from cli.commands.chat import _send_turn

    frames = [
        {"type": "ready"},
        {"type": "error", "kind": "turn_idle_timeout", "message": "no agent activity for 300s"},
    ]
    _install_wire(monkeypatch, _wire_bytes(frames))
    result = _send_turn("sess-e2e", "hang", live_render=False)
    assert result.error == "no agent activity for 300s"
