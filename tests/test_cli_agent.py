"""CLI tests for `agnes agent` — profile CRUD, scope, token minting, ask (Task 11).

Mocks the HTTP layer the same way ``tests/test_cli_admin_digest.py`` does:
patch ``cli.commands.agent.api_{get,post,put,delete}`` with a MagicMock
response, invoke via Typer's CliRunner, assert method/path/payload +
rendering (table and ``--json``).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

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


_AGENT_ROW = {
    "id": "ag_1",
    "slug": "research",
    "name": "Research",
    "description": None,
    "model": "claude-x",
    "token_budget_monthly": 1000,
    "plugins_mode": "selected",
    "connections_mode": "selected",
    "tables_mode": "selected",
    "memory_mode": "selected",
    "memory_write_mode": "off",
    "is_default": False,
    "created_at": "2026-07-01",
    "updated_at": "2026-07-01",
}


class TestList:
    def test_list_text(self):
        with patch(
            "cli.commands.agent.api_get",
            return_value=_resp(200, {"data": [_AGENT_ROW], "has_more": False, "next_cursor": None}),
        ) as m:
            result = runner.invoke(app, ["agent", "list"])
        assert result.exit_code == 0
        assert m.call_args.args[0] == "/api/v1/agents"
        assert "research" in result.output
        assert "Research" in result.output

    def test_list_json(self):
        with patch(
            "cli.commands.agent.api_get",
            return_value=_resp(200, {"data": [_AGENT_ROW], "has_more": False, "next_cursor": None}),
        ):
            result = runner.invoke(app, ["agent", "list", "--json"])
        data = json.loads(result.output)
        assert data[0]["slug"] == "research"

    def test_list_empty_hints_create(self):
        with patch(
            "cli.commands.agent.api_get",
            return_value=_resp(200, {"data": [], "has_more": False, "next_cursor": None}),
        ):
            result = runner.invoke(app, ["agent", "list"])
        assert result.exit_code == 0
        assert "agnes agent create" in result.output


class TestCreate:
    def test_create_success(self):
        created = dict(_AGENT_ROW)
        with patch(
            "cli.commands.agent.api_post",
            return_value=_resp(201, created),
        ) as m:
            result = runner.invoke(
                app,
                ["agent", "create", "Research", "--slug", "research", "--model", "claude-x", "--budget", "1000"],
            )
        assert result.exit_code == 0
        assert m.call_args.args[0] == "/api/v1/agents"
        assert m.call_args.kwargs["json"] == {
            "name": "Research",
            "slug": "research",
            "model": "claude-x",
            "token_budget_monthly": 1000,
        }
        assert "ag_1" in result.output
        assert "research" in result.output

    def test_create_with_prompt_file(self, tmp_path):
        f = tmp_path / "prompt.txt"
        f.write_text("You are a research assistant.\n", encoding="utf-8")
        with patch(
            "cli.commands.agent.api_post",
            return_value=_resp(201, dict(_AGENT_ROW)),
        ) as m:
            result = runner.invoke(
                app,
                ["agent", "create", "Research", "--slug", "research", "--prompt-file", str(f)],
            )
        assert result.exit_code == 0
        assert m.call_args.kwargs["json"]["system_prompt"] == "You are a research assistant."

    def test_create_slug_taken_renders_detail_code(self):
        with patch(
            "cli.commands.agent.api_post",
            return_value=_resp(409, {"detail": {"code": "slug_taken", "message": "slug 'research' is already in use"}}),
        ):
            result = runner.invoke(app, ["agent", "create", "Research", "--slug", "research"])
        assert result.exit_code == 1
        assert "slug_taken" in result.output
        assert "already in use" in result.output


class TestShow:
    def test_show_found(self):
        with patch(
            "cli.commands.agent.api_get",
            return_value=_resp(200, {"data": [_AGENT_ROW], "has_more": False, "next_cursor": None}),
        ):
            result = runner.invoke(app, ["agent", "show", "research"])
        assert result.exit_code == 0
        assert "ag_1" in result.output
        assert "claude-x" in result.output

    def test_show_json(self):
        with patch(
            "cli.commands.agent.api_get",
            return_value=_resp(200, {"data": [_AGENT_ROW], "has_more": False, "next_cursor": None}),
        ):
            result = runner.invoke(app, ["agent", "show", "research", "--json"])
        data = json.loads(result.output)
        assert data["id"] == "ag_1"

    def test_show_not_found_hints_list(self):
        with patch(
            "cli.commands.agent.api_get",
            return_value=_resp(200, {"data": [], "has_more": False, "next_cursor": None}),
        ):
            result = runner.invoke(app, ["agent", "show", "nope"])
        assert result.exit_code == 1
        assert "agnes agent list" in result.output


class TestScopeSet:
    def test_scope_set_sends_put_with_items(self):
        with (
            patch(
                "cli.commands.agent.api_get",
                return_value=_resp(200, {"data": [_AGENT_ROW], "has_more": False, "next_cursor": None}),
            ),
            patch(
                "cli.commands.agent.api_put",
                return_value=_resp(200, {"items": [{"item_type": "plugin", "item_id": "p1"}]}),
            ) as m,
        ):
            result = runner.invoke(
                app,
                [
                    "agent",
                    "scope",
                    "set",
                    "research",
                    "--plugin",
                    "p1",
                    "--table",
                    "t1",
                    "--connection",
                    "c1",
                    "--memory-domain",
                    "d1",
                ],
            )
        assert result.exit_code == 0
        assert m.call_args.args[0] == "/api/v1/agents/ag_1/scope"
        items = m.call_args.kwargs["json"]["items"]
        assert {"item_type": "plugin", "item_id": "p1"} in items
        assert {"item_type": "table", "item_id": "t1"} in items
        assert {"item_type": "connection", "item_id": "c1"} in items
        assert {"item_type": "memory_domain", "item_id": "d1"} in items

    def test_scope_set_requires_at_least_one_item(self):
        result = runner.invoke(app, ["agent", "scope", "set", "research"])
        assert result.exit_code == 2


class TestToken:
    def test_token_prints_secret_once_with_warning(self):
        with (
            patch(
                "cli.commands.agent.api_get",
                return_value=_resp(200, {"data": [_AGENT_ROW], "has_more": False, "next_cursor": None}),
            ),
            patch(
                "cli.commands.agent.api_post",
                return_value=_resp(
                    200,
                    {
                        "id": "tok_1",
                        "name": "ci",
                        "prefix": "abcd1234",
                        "agent_id": "ag_1",
                        "token": "agent-pat-secret-value",
                        "expires_at": "2026-10-01",
                        "created_at": "2026-07-01",
                    },
                ),
            ) as m,
        ):
            result = runner.invoke(app, ["agent", "token", "research", "--name", "ci"])
        assert result.exit_code == 0
        assert m.call_args.args[0] == "/api/v1/agents/ag_1/tokens"
        assert m.call_args.kwargs["json"] == {"name": "ci", "expires_in_days": 90}
        assert "agent-pat-secret-value" in result.output
        assert "ONCE" in result.output

    def test_token_expires_days_zero_means_never(self):
        with (
            patch(
                "cli.commands.agent.api_get",
                return_value=_resp(200, {"data": [_AGENT_ROW], "has_more": False, "next_cursor": None}),
            ),
            patch(
                "cli.commands.agent.api_post",
                return_value=_resp(
                    200,
                    {"id": "tok_1", "name": "ci", "prefix": "x", "agent_id": "ag_1", "token": "t", "expires_at": None},
                ),
            ) as m,
        ):
            result = runner.invoke(app, ["agent", "token", "research", "--name", "ci", "--expires-days", "0"])
        assert result.exit_code == 0
        assert m.call_args.kwargs["json"] == {"name": "ci", "expires_in_days": None}

    def test_token_not_selected_mode_renders_detail_code(self):
        with (
            patch(
                "cli.commands.agent.api_get",
                return_value=_resp(200, {"data": [_AGENT_ROW], "has_more": False, "next_cursor": None}),
            ),
            patch(
                "cli.commands.agent.api_post",
                return_value=_resp(
                    403,
                    {
                        "detail": {
                            "code": "agent_not_selected_mode",
                            "message": "agent PATs require all four scope modes to be 'selected'",
                        }
                    },
                ),
            ),
        ):
            result = runner.invoke(app, ["agent", "token", "research", "--name", "ci"])
        assert result.exit_code == 1
        assert "agent_not_selected_mode" in result.output


class TestDelete:
    def test_delete_requires_confirm_without_yes(self):
        with (
            patch(
                "cli.commands.agent.api_get",
                return_value=_resp(200, {"data": [_AGENT_ROW], "has_more": False, "next_cursor": None}),
            ),
            patch("cli.commands.agent.api_delete") as m,
        ):
            result = runner.invoke(app, ["agent", "delete", "research"], input="n\n")
        m.assert_not_called()
        assert result.exit_code != 0

    def test_delete_with_yes_calls_delete(self):
        with (
            patch(
                "cli.commands.agent.api_get",
                return_value=_resp(200, {"data": [_AGENT_ROW], "has_more": False, "next_cursor": None}),
            ),
            patch("cli.commands.agent.api_delete", return_value=_resp(204)) as m,
        ):
            result = runner.invoke(app, ["agent", "delete", "research", "--yes"])
        assert result.exit_code == 0
        assert m.call_args.args[0] == "/api/v1/agents/ag_1"


class TestAsk:
    def test_ask_sync_answer(self):
        with patch(
            "cli.commands.agent.api_post",
            return_value=_resp(
                200,
                {
                    "answer": "The answer is 42.",
                    "session_id": "sess_1",
                    "response_id": "resp_1",
                    "usage": {},
                    "agent_config_hash": "abc",
                    "request_id": "req_1",
                },
            ),
        ) as m:
            result = runner.invoke(app, ["agent", "ask", "research", "what is the answer?"])
        assert result.exit_code == 0
        assert m.call_args.args[0] == "/api/v1/agents/research/responses"
        assert m.call_args.kwargs["json"]["input"] == "what is the answer?"
        assert "The answer is 42." in result.output

    def test_ask_sync_answer_json(self):
        with patch(
            "cli.commands.agent.api_post",
            return_value=_resp(200, {"answer": "42", "session_id": "s1"}),
        ):
            result = runner.invoke(app, ["agent", "ask", "research", "q", "--json"])
        data = json.loads(result.output)
        assert data["answer"] == "42"

    def test_ask_background_polls_until_completed(self):
        job_queued = {"id": "job_1", "status": "queued", "result": None, "error": None}
        job_done = {"id": "job_1", "status": "completed", "result": {"answer": "final answer"}, "error": None}
        with (
            patch(
                "cli.commands.agent.api_post",
                return_value=_resp(202, {"job_id": "job_1"}),
            ),
            patch(
                "cli.commands.agent.api_get",
                side_effect=[_resp(200, job_queued), _resp(200, job_done)],
            ),
            patch("cli.commands.agent.time.sleep"),
        ):
            result = runner.invoke(app, ["agent", "ask", "research", "q"])
        assert result.exit_code == 0
        assert "final answer" in result.output

    def test_ask_background_job_failed(self):
        job_failed = {
            "id": "job_1",
            "status": "failed",
            "result": None,
            "error": {"code": "concurrency_cap", "message": "too many"},
        }
        with (
            patch("cli.commands.agent.api_post", return_value=_resp(202, {"job_id": "job_1"})),
            patch("cli.commands.agent.api_get", return_value=_resp(200, job_failed)),
            patch("cli.commands.agent.time.sleep"),
        ):
            result = runner.invoke(app, ["agent", "ask", "research", "q"])
        assert result.exit_code == 1
        assert "concurrency_cap" in result.output

    def test_ask_poll_bounded_by_timeout(self):
        """Never-terminal job must not loop forever — bounded by --timeout."""
        job_running = {"id": "job_1", "status": "in_progress", "result": None, "error": None}
        with (
            patch("cli.commands.agent.api_post", return_value=_resp(202, {"job_id": "job_1"})),
            patch("cli.commands.agent.api_get", return_value=_resp(200, job_running)) as m_get,
            patch("cli.commands.agent.time.sleep"),
        ):
            result = runner.invoke(app, ["agent", "ask", "research", "q", "--timeout", "4"])
        assert result.exit_code == 1
        # bounded: a handful of polls, not unbounded
        assert m_get.call_count <= 10
        assert "Timed out" in result.output

    def test_ask_error_renders_detail_code(self):
        with patch(
            "cli.commands.agent.api_post",
            return_value=_resp(404, {"detail": {"code": "agent_not_found"}}),
        ):
            result = runner.invoke(app, ["agent", "ask", "nope", "q"])
        assert result.exit_code == 1
        assert "agent_not_found" in result.output
