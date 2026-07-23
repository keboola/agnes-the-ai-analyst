"""Tests for the AG-UI SSE event vocabulary mapper (app/api/agent_sse.py).

Pure module: no I/O, no manager. Maps internal chat frame dicts (as emitted
by app/chat/manager.py's runner/broadcast pipeline) to AG-UI event dicts,
and serializes SSE records.
"""

import json

from app.api.agent_sse import SSE_TERMINAL_TYPES, frame_to_agui, sse_bytes


class TestFrameToAgui:
    def test_ready_maps_to_run_started(self):
        assert frame_to_agui({"type": "ready"}) == {"type": "RUN_STARTED"}

    def test_token_maps_to_text_message_content(self):
        assert frame_to_agui({"type": "token", "content": "he"}) == {
            "type": "TEXT_MESSAGE_CONTENT",
            "delta": "he",
        }

    def test_assistant_message_maps_to_text_message_end(self):
        assert frame_to_agui({"type": "assistant_message", "content": "hello world"}) == {
            "type": "TEXT_MESSAGE_END",
            "content": "hello world",
        }

    def test_tool_call_maps_to_tool_call_start_with_tool_and_args_fields(self):
        # CRITICAL: the real runner tool-call frame carries `tool`/`args`,
        # NOT `name`/`input` (verified against app/chat/manager.py's
        # _pump_subprocess_to_ws audit block, ~line 1735-1736).
        frame = {"type": "tool_call", "tool": "bash", "args": {"command": "ls"}}
        assert frame_to_agui(frame) == {
            "type": "TOOL_CALL_START",
            "name": "bash",
            "args": {"command": "ls"},
        }

    def test_tool_call_missing_fields_maps_to_none_values(self):
        assert frame_to_agui({"type": "tool_call"}) == {
            "type": "TOOL_CALL_START",
            "name": None,
            "args": None,
        }

    def test_tool_result_maps_to_tool_call_end(self):
        frame = {"type": "tool_result", "result": "ok"}
        assert frame_to_agui(frame) == {"type": "TOOL_CALL_END", "result": "ok"}

    def test_done_maps_to_run_finished(self):
        assert frame_to_agui({"type": "done"}) == {"type": "RUN_FINISHED"}

    def test_error_maps_to_run_error_with_message(self):
        frame = {"type": "error", "message": "boom"}
        assert frame_to_agui(frame) == {"type": "RUN_ERROR", "message": "boom"}

    def test_cancelled_maps_to_run_error_with_cancelled_code(self):
        assert frame_to_agui({"type": "cancelled"}) == {
            "type": "RUN_ERROR",
            "message": "cancelled",
            "code": "cancelled",
        }

    def test_session_renamed_dropped(self):
        assert frame_to_agui({"type": "session_renamed", "title": "x"}) is None

    def test_unknown_frame_dropped(self):
        assert frame_to_agui({"type": "some_future_frame_type"}) is None

    def test_missing_type_dropped(self):
        assert frame_to_agui({}) is None


class TestSseBytes:
    def test_sse_bytes_with_id(self):
        out = sse_bytes({"type": "RUN_FINISHED"}, "c1:5").decode()
        assert out.startswith("id: c1:5\n")
        assert "event: RUN_FINISHED\n" in out
        assert out.endswith("\n\n")
        data_line = [line for line in out.splitlines() if line.startswith("data: ")][0]
        assert json.loads(data_line[len("data: ") :])["type"] == "RUN_FINISHED"

    def test_sse_bytes_without_id_omits_id_line(self):
        out = sse_bytes({"type": "RUN_STARTED"}, None).decode()
        assert "id:" not in out
        assert out.startswith("event: RUN_STARTED\n")
        assert out.endswith("\n\n")

    def test_sse_bytes_data_round_trips_full_event(self):
        event = {"type": "TEXT_MESSAGE_CONTENT", "delta": "hi"}
        out = sse_bytes(event, "c1:1").decode()
        data_line = [line for line in out.splitlines() if line.startswith("data: ")][0]
        assert json.loads(data_line[len("data: ") :]) == event

    def test_sse_bytes_returns_bytes_utf8(self):
        out = sse_bytes({"type": "TEXT_MESSAGE_CONTENT", "delta": "héllo"}, None)
        assert isinstance(out, bytes)
        assert "héllo" in out.decode("utf-8")


class TestSseTerminalTypes:
    def test_terminal_types_contents(self):
        assert SSE_TERMINAL_TYPES == {"RUN_FINISHED", "RUN_ERROR"}

    def test_run_started_not_terminal(self):
        assert "RUN_STARTED" not in SSE_TERMINAL_TYPES

    def test_tool_call_start_not_terminal(self):
        assert "TOOL_CALL_START" not in SSE_TERMINAL_TYPES
