"""CLI tests for `agnes chat` — the V1c streaming terminal thin client
(Task 6) over the V1b agent-session API, plus a regression check that the
new `_ChatGroup` dispatch trick didn't break the pre-existing `agnes chat
skills` subcommand.

Mocks the HTTP layer the same way ``tests/test_cli_agent.py`` does: patch
``cli.commands.chat.api_{get,post,delete,post_sse}`` directly and invoke via
Typer's CliRunner. ``api_post_sse`` is mocked as a plain generator/iterator
over canned AG-UI event dicts (see ``app/api/agent_sse.py`` for the real
vocabulary) — no real HTTP/SSE parsing is exercised here, that's
``cli.client.api_post_sse``'s own concern (not under test in this file).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

import cli.commands.chat as chat_mod
from cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    yield tmp_path


def _resp(status_code=200, json_data=None, text=""):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data if json_data is not None else {}
    r.text = text
    return r


EVENTS_HI = [
    {"type": "RUN_STARTED"},
    {"type": "TEXT_MESSAGE_CONTENT", "delta": "Hello"},
    {"type": "TEXT_MESSAGE_CONTENT", "delta": " world"},
    {"type": "TEXT_MESSAGE_END", "content": "Hello world"},
    {"type": "RUN_FINISHED"},
]

EVENTS_ERROR = [
    {"type": "TEXT_MESSAGE_CONTENT", "delta": "partial answer"},
    {"type": "RUN_ERROR", "message": "tool exploded", "code": "tool_failed"},
]


class TestOnce:
    def test_once_prints_assembled_answer_and_exits_zero(self):
        with (
            patch("cli.commands.chat.api_post", return_value=_resp(201, {"session_id": "sess-1"})) as mock_post,
            patch("cli.commands.chat.api_post_sse", return_value=iter(EVENTS_HI)) as mock_sse,
            patch("cli.commands.chat.api_delete", return_value=_resp(204)) as mock_delete,
        ):
            result = runner.invoke(app, ["chat", "myagent", "--once", "hi"])

        assert result.exit_code == 0
        assert "Hello world" in result.output

        # Session created against the right agent slug.
        assert mock_post.call_args_list[0].args[0] == "/api/v1/agents/myagent/sessions"
        # Turn sent to the right session with the right body.
        assert mock_sse.call_args.args[0] == "/api/v1/sessions/sess-1/messages"
        assert mock_sse.call_args.kwargs["json"] == {"input": "hi"}
        # /exit-equivalent cleanup: --once best-effort deletes the session too.
        assert mock_delete.call_args.args[0] == "/api/v1/sessions/sess-1"

    def test_run_error_event_exits_nonzero_with_rendered_message(self):
        with (
            patch("cli.commands.chat.api_post", return_value=_resp(201, {"session_id": "sess-1"})),
            patch("cli.commands.chat.api_post_sse", return_value=iter(EVENTS_ERROR)),
            patch("cli.commands.chat.api_delete", return_value=_resp(204)),
        ):
            result = runner.invoke(app, ["chat", "myagent", "--once", "boom"])

        assert result.exit_code == 1
        assert "tool exploded" in result.output
        # The partial answer that streamed before the error is still shown.
        assert "partial answer" in result.output

    def test_once_json_dumps_full_event_list(self):
        with (
            patch("cli.commands.chat.api_post", return_value=_resp(201, {"session_id": "sess-1"})),
            patch("cli.commands.chat.api_post_sse", return_value=iter(EVENTS_HI)),
            patch("cli.commands.chat.api_delete", return_value=_resp(204)),
        ):
            result = runner.invoke(app, ["chat", "myagent", "--once", "hi", "--json"])

        assert result.exit_code == 0
        dumped = json.loads(result.output)
        assert dumped == EVENTS_HI

    def test_json_without_once_is_rejected(self):
        result = runner.invoke(app, ["chat", "myagent", "--json"])
        assert result.exit_code == 2
        assert "--once" in result.output

    def test_session_create_404_gives_helpful_error(self):
        with patch(
            "cli.commands.chat.api_post",
            return_value=_resp(404, {"detail": {"code": "agent_not_found"}}),
        ):
            result = runner.invoke(app, ["chat", "no-such-agent", "--once", "hi"])

        assert result.exit_code == 1
        assert "agnes agent list" in result.output


class TestInteractive:
    def test_stdin_lines_then_exit(self):
        with (
            patch("cli.commands.chat.api_post", return_value=_resp(201, {"session_id": "sess-1"})) as mock_post,
            patch("cli.commands.chat.api_post_sse", return_value=iter(EVENTS_HI)) as mock_sse,
            patch("cli.commands.chat.api_delete", return_value=_resp(204)) as mock_delete,
        ):
            result = runner.invoke(app, ["chat", "myagent"], input="hello\n/exit\n")

        assert result.exit_code == 0
        assert "Hello world" in result.output
        assert mock_post.call_args_list[0].args[0] == "/api/v1/agents/myagent/sessions"
        mock_sse.assert_called_once()
        assert mock_sse.call_args.args[0] == "/api/v1/sessions/sess-1/messages"
        assert mock_sse.call_args.kwargs["json"] == {"input": "hello"}
        # /exit best-effort deletes the session.
        assert mock_delete.call_args.args[0] == "/api/v1/sessions/sess-1"

    def test_eof_also_quits_and_cleans_up(self):
        with (
            patch("cli.commands.chat.api_post", return_value=_resp(201, {"session_id": "sess-1"})),
            patch("cli.commands.chat.api_post_sse", return_value=iter(EVENTS_HI)),
            patch("cli.commands.chat.api_delete", return_value=_resp(204)) as mock_delete,
        ):
            # No trailing /exit — the input just runs out (EOF).
            result = runner.invoke(app, ["chat", "myagent"], input="hello\n")

        assert result.exit_code == 0
        assert mock_delete.called

    def test_blank_lines_and_empty_input_are_skipped(self):
        with (
            patch("cli.commands.chat.api_post", return_value=_resp(201, {"session_id": "sess-1"})),
            patch("cli.commands.chat.api_post_sse", return_value=iter(EVENTS_HI)) as mock_sse,
            patch("cli.commands.chat.api_delete", return_value=_resp(204)),
        ):
            result = runner.invoke(app, ["chat", "myagent"], input="\n   \nhello\n/exit\n")

        assert result.exit_code == 0
        # Only the one real message reaches the server.
        mock_sse.assert_called_once()


class TestCtrlC:
    """C7: Ctrl-C mid-stream must stop consuming, best-effort cancel, and
    return cleanly — never propagate and never leave a half-read stream.
    Exercised directly against ``_send_turn`` (unit level) since that's
    the exact seam the SIGINT lands in — simulating a `KeyboardInterrupt`
    partway through consuming the mocked event generator is the cleanest
    way to pin this down without fighting real OS signal delivery in a
    test process.
    """

    def test_keyboard_interrupt_mid_stream_cancels_and_returns_cleanly(self):
        cancel_calls = []

        def fake_sse(path, json=None):
            yield {"type": "TEXT_MESSAGE_CONTENT", "delta": "partial"}
            raise KeyboardInterrupt()

        def fake_post(path, **kwargs):
            cancel_calls.append(path)
            return _resp(202, {})

        with (
            patch("cli.commands.chat.api_post_sse", side_effect=fake_sse),
            patch("cli.commands.chat.api_post", side_effect=fake_post),
        ):
            # No exception should escape — this is the "never a wedged
            # terminal" requirement: a caller (the REPL loop) can call this
            # and keep going.
            result = chat_mod._send_turn("sess-1", "hi", live_render=False)

        assert result.cancelled is True
        assert result.answer == "partial"
        assert cancel_calls == ["/api/v1/sessions/sess-1/cancel"]

    def test_ctrl_c_in_repl_loop_does_not_crash_the_process(self):
        """End-to-end: a turn that raises KeyboardInterrupt mid-stream inside
        the interactive loop must be caught, cancel posted, and the REPL
        must keep running (reach the next prompt and honor /exit) rather
        than letting the interrupt escape to Click's top-level handler."""

        call_count = {"n": 0}

        def fake_sse(path, json=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                yield {"type": "TEXT_MESSAGE_CONTENT", "delta": "partial"}
                raise KeyboardInterrupt()
            yield from EVENTS_HI

        with (
            patch("cli.commands.chat.api_post", return_value=_resp(201, {"session_id": "sess-1"})) as mock_post,
            patch("cli.commands.chat.api_post_sse", side_effect=fake_sse),
            patch("cli.commands.chat.api_delete", return_value=_resp(204)),
        ):
            result = runner.invoke(app, ["chat", "myagent"], input="first\nsecond\n/exit\n")

        assert result.exit_code == 0
        assert "Cancelled" in result.output
        assert "Hello world" in result.output
        # One session-create call, then the two turns (cancel POST is a
        # separate call to the same mocked api_post, so >= 3 calls total).
        assert mock_post.call_args_list[0].args[0] == "/api/v1/agents/myagent/sessions"


class TestSkillsRegression:
    """`_ChatGroup.parse_args` redirects any unrecognized first token to
    the hidden `_repl` command — this locks down that a REGISTERED
    subcommand name (`skills`) still dispatches normally instead of being
    swallowed as an agent slug."""

    def test_chat_skills_still_dispatches_to_the_catalog_command(self):
        with patch(
            "cli.commands.chat.api_get",
            return_value=_resp(200, {"skills": [], "commands": []}),
        ) as mock_get:
            result = runner.invoke(app, ["chat", "skills"])

        assert result.exit_code == 0
        assert mock_get.call_args.args[0] == "/api/chat/skills"
        assert "No skills or commands available." in result.output
