"""Tests for the AG-UI SSE event vocabulary mapper (app/api/agent_sse.py).

Pure module: no I/O, no manager. Maps internal chat frame dicts (as emitted
by app/chat/manager.py's runner/broadcast pipeline) to AG-UI event dicts,
and serializes SSE records.
"""

import ast
import json
import pathlib


from app.api.agent_sse import SSE_TERMINAL_TYPES, frame_to_agui, sse_bytes


class TestFrameToAgui:
    def test_ready_maps_to_run_started(self):
        assert frame_to_agui({"type": "ready"}) == {"type": "RUN_STARTED"}

    def test_token_maps_to_text_message_content(self):
        # CRITICAL: the real runner token frame carries `text`, NOT
        # `content` (app/chat/runner.py: `_emit({"type": "token",
        # "text": piece})`) — same class of field-name trap the tool_call
        # case below calls out. Reading `content` here yielded
        # `delta: None` on every streamed token, which is why
        # `agnes chat <slug> --once` printed "(no answer)" while the
        # answer itself arrived intact in the trailing TEXT_MESSAGE_END.
        assert frame_to_agui({"type": "token", "text": "he"}) == {
            "type": "TEXT_MESSAGE_CONTENT",
            "delta": "he",
        }

    def test_token_without_text_maps_to_none_delta(self):
        assert frame_to_agui({"type": "token"}) == {
            "type": "TEXT_MESSAGE_CONTENT",
            "delta": None,
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


# ---------------------------------------------------------------------------
# Static guard: the field names the mapper READS must be field names some
# real emitter WRITES.
#
# Every test above this line feeds `frame_to_agui` a hand-written frame, so
# the suite stayed green while the mapper read `token["content"]` and the
# runner emitted `token["text"]` — the fixtures encoded the bug. A unit test
# cannot catch a field-name drift when the same author writes both sides, so
# this reads the ACTUAL emitters out of `app/chat/` and diffs the two.
# ---------------------------------------------------------------------------

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHAT_PKG = _REPO_ROOT / "app" / "chat"
_MAPPER_SRC = _REPO_ROOT / "app" / "api" / "agent_sse.py"


def _frame_get_keys(node: ast.AST) -> set[str]:
    """Every ``frame.get("<literal>")`` key read anywhere under ``node``."""
    keys: set[str] = set()
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "get"
            and isinstance(sub.func.value, ast.Name)
            and sub.func.value.id == "frame"
            and sub.args
            and isinstance(sub.args[0], ast.Constant)
            and isinstance(sub.args[0].value, str)
        ):
            keys.add(sub.args[0].value)
    return keys


def _mapper_reads() -> dict[str, set[str]]:
    """Derive ``{frame_type: {fields read}}`` from `frame_to_agui`'s own AST.

    Reads the branch structure (``if ftype == "<type>": ...``) rather than a
    hand-kept table, so the check cannot be satisfied by updating a fixture:
    changing which field the mapper reads changes what this returns.
    """
    tree = ast.parse(_MAPPER_SRC.read_text(encoding="utf-8"), filename=str(_MAPPER_SRC))
    fn = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "frame_to_agui")
    reads: dict[str, set[str]] = {}
    for stmt in ast.walk(fn):
        if not isinstance(stmt, ast.If) or not isinstance(stmt.test, ast.Compare):
            continue
        test = stmt.test
        if not (len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)):
            continue
        right = test.comparators[0]
        if not (isinstance(right, ast.Constant) and isinstance(right.value, str)):
            continue
        for branch_stmt in stmt.body:
            reads.setdefault(right.value, set()).update(_frame_get_keys(branch_stmt))
    return reads


def _emitted_frame_fields() -> dict[str, set[str]]:
    """Collect ``{frame_type: {field names}}`` from `app/chat/` source.

    Walks every dict literal carrying a constant ``"type"`` key and unions
    its literal string keys. Frames assembled dynamically (``d["type"] =
    ...``) are invisible here — acceptable, since every frame the mapper
    handles is written as a literal today, and a type that stops appearing
    fails the ``frame_type in emitted`` assertion rather than passing
    silently.
    """
    emitted: dict[str, set[str]] = {}
    for path in sorted(_CHAT_PKG.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            literal_keys = {
                key.value: value
                for key, value in zip(node.keys, node.values)
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            frame_type = literal_keys.get("type")
            if isinstance(frame_type, ast.Constant) and isinstance(frame_type.value, str):
                emitted.setdefault(frame_type.value, set()).update(literal_keys)
    return emitted


class TestMapperFieldsMatchRealEmitters:
    def test_the_guard_actually_sees_both_sides(self):
        # A drift check that reads nothing asserts nothing. Pin that both
        # AST extractors found the frame types this mapper is built around,
        # so a parser that silently stops matching (a refactor to a dispatch
        # dict, a renamed parameter) fails loudly instead of passing empty.
        reads = _mapper_reads()
        emitted = _emitted_frame_fields()
        assert {"token", "assistant_message", "tool_call", "tool_result", "error"} <= set(reads)
        assert reads["token"], "extractor found no field read for the token branch"
        assert {"token", "assistant_message", "tool_call", "tool_result"} <= set(emitted)

    def test_every_field_the_mapper_reads_is_written_by_an_emitter(self):
        reads = _mapper_reads()
        emitted = _emitted_frame_fields()
        problems = []
        for frame_type, fields in sorted(reads.items()):
            if not fields:
                continue  # data-free branch (`ready`, `done`, `cancelled`)
            if frame_type not in emitted:
                problems.append(
                    f"{frame_type!r}: mapper reads {sorted(fields)} but no such frame is emitted anywhere in app/chat/"
                )
                continue
            missing = fields - emitted[frame_type]
            if missing:
                problems.append(
                    f"{frame_type!r}: mapper reads {sorted(missing)}, emitters write "
                    f"{sorted(emitted[frame_type])} — the mapped value is None on the wire"
                )
        assert not problems, "frame field-name drift between emitter and mapper:\n" + "\n".join(problems)

    def test_token_branch_reads_the_field_the_runner_writes(self):
        # The specific regression: mapper read `content`, runner writes
        # `text`. Pinned on both sides so neither can drift back alone.
        assert _mapper_reads()["token"] == {"text"}
        emitted_token = _emitted_frame_fields()["token"]
        assert "text" in emitted_token
        assert "content" not in emitted_token
