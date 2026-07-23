"""CLI tests for `agnes app ...` (hosted data apps, Task 10).

Follows the `tests/test_glossary_cli.py` idiom: patch the module-level
`api_get`/`api_post`/`api_delete` names inside `cli.commands.data_apps`
(Typer captured them at import time) with a MagicMock response, then invoke
through `typer.testing.CliRunner`.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from cli.main import app

runner = CliRunner()


def _mock_response(status_code, json_body, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    resp.text = text or str(json_body)
    return resp


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_human_readable():
    fake = _mock_response(
        200,
        [
            {"slug": "sapp", "name": "S App", "state": "running", "url": "/apps/sapp/"},
            {"slug": "bapp", "name": "B App", "state": "stopped", "url": "/apps/bapp/"},
        ],
    )
    with patch("cli.commands.data_apps.api_get", return_value=fake) as mock_get:
        result = runner.invoke(app, ["app", "list"])
    assert result.exit_code == 0, result.output
    assert "sapp" in result.stdout
    assert "running" in result.stdout
    mock_get.assert_called_once()
    assert mock_get.call_args.args[0] == "/api/data-apps"


def test_list_json():
    fake = _mock_response(200, [{"slug": "sapp", "name": "S", "state": "running", "url": "/apps/sapp/"}])
    with patch("cli.commands.data_apps.api_get", return_value=fake):
        result = runner.invoke(app, ["app", "list", "--json"])
    assert result.exit_code == 0
    assert '"slug": "sapp"' in result.stdout


def test_list_empty_hints_create():
    fake = _mock_response(200, [])
    with patch("cli.commands.data_apps.api_get", return_value=fake):
        result = runner.invoke(app, ["app", "list"])
    assert result.exit_code == 0
    assert "agnes app create" in result.stdout


def test_list_respects_limit():
    fake = _mock_response(
        200,
        [{"slug": f"a{i}", "name": f"A{i}", "state": "running", "url": ""} for i in range(5)],
    )
    with patch("cli.commands.data_apps.api_get", return_value=fake):
        result = runner.invoke(app, ["app", "list", "--limit", "2", "--json"])
    assert result.exit_code == 0
    import json as json_lib

    body = json_lib.loads(result.stdout)
    assert len(body) == 2


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def test_show_found():
    fake = _mock_response(
        200, {"slug": "sapp", "name": "S App", "state": "running", "url": "/apps/sapp/", "description": "desc"}
    )
    with patch("cli.commands.data_apps.api_get", return_value=fake) as mock_get:
        result = runner.invoke(app, ["app", "show", "sapp"])
    assert result.exit_code == 0
    assert "S App" in result.stdout
    assert "running" in result.stdout
    assert mock_get.call_args.args[0] == "/api/data-apps/sapp"


def test_show_json():
    fake = _mock_response(200, {"slug": "sapp", "name": "S", "state": "running", "url": "/apps/sapp/"})
    with patch("cli.commands.data_apps.api_get", return_value=fake):
        result = runner.invoke(app, ["app", "show", "sapp", "--json"])
    assert result.exit_code == 0
    assert '"state": "running"' in result.stdout


def test_show_not_found_hints_list():
    fake = _mock_response(404, {"detail": "data_app_not_found"})
    with patch("cli.commands.data_apps.api_get", return_value=fake):
        result = runner.invoke(app, ["app", "show", "nope"])
    assert result.exit_code == 1
    output = result.output + str(result.stderr_bytes or b"")
    assert "not found" in output.lower()
    assert "agnes app list" in output


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_create_internal_default():
    fake = _mock_response(201, {"id": "id1", "slug": "myapp", "git_url": "https://server/data-apps.git/myapp"})
    with patch("cli.commands.data_apps.api_post", return_value=fake) as mock_post:
        result = runner.invoke(app, ["app", "create", "myapp", "My App"])
    assert result.exit_code == 0, result.output
    assert "myapp" in result.stdout
    assert "data-apps.git/myapp" in result.stdout
    assert mock_post.call_args.args[0] == "/api/data-apps"
    payload = mock_post.call_args.kwargs["json"]
    assert payload["slug"] == "myapp"
    assert payload["name"] == "My App"
    assert "repo_mode" not in payload  # let the server default to internal


def test_create_external_repo_mode():
    fake = _mock_response(201, {"id": "id1", "slug": "ext1", "git_url": "https://server/data-apps.git/ext1"})
    with patch("cli.commands.data_apps.api_post", return_value=fake) as mock_post:
        result = runner.invoke(
            app,
            [
                "app",
                "create",
                "ext1",
                "External App",
                "--repo-url",
                "https://github.com/acme/app.git",
                "--repo-branch",
                "release",
            ],
        )
    assert result.exit_code == 0, result.output
    payload = mock_post.call_args.kwargs["json"]
    assert payload["repo_mode"] == "external"
    assert payload["repo_url"] == "https://github.com/acme/app.git"
    assert payload["repo_branch"] == "release"


def test_create_quota_exceeded_friendly_message():
    fake = _mock_response(403, {"detail": "app_quota_exceeded"})
    with patch("cli.commands.data_apps.api_post", return_value=fake):
        result = runner.invoke(app, ["app", "create", "toomany", "Too Many"])
    assert result.exit_code == 1
    output = result.output + str(result.stderr_bytes or b"")
    assert "quota" in output.lower()
    assert "app_quota_exceeded" not in output  # mapped to a human message, not the raw code


def test_create_unknown_error_falls_back_to_raw_detail():
    fake = _mock_response(400, {"detail": "some_unmapped_detail"})
    with patch("cli.commands.data_apps.api_post", return_value=fake):
        result = runner.invoke(app, ["app", "create", "x", "X"])
    assert result.exit_code == 1
    output = result.output + str(result.stderr_bytes or b"")
    assert "some_unmapped_detail" in output


# ---------------------------------------------------------------------------
# deploy
# ---------------------------------------------------------------------------


def test_deploy_default():
    fake = _mock_response(200, {"state": "running", "deployed_sha": "abc123"})
    with patch("cli.commands.data_apps.api_post", return_value=fake) as mock_post:
        result = runner.invoke(app, ["app", "deploy", "sapp"])
    assert result.exit_code == 0, result.output
    assert "running" in result.stdout
    assert "abc123" in result.stdout
    assert mock_post.call_args.args[0] == "/api/data-apps/sapp/deploy"
    assert mock_post.call_args.kwargs["json"] == {}


def test_deploy_with_sha():
    fake = _mock_response(200, {"state": "running", "deployed_sha": "deadbeef"})
    with patch("cli.commands.data_apps.api_post", return_value=fake) as mock_post:
        result = runner.invoke(app, ["app", "deploy", "sapp", "--sha", "deadbeef"])
    assert result.exit_code == 0
    assert mock_post.call_args.kwargs["json"] == {"sha": "deadbeef"}


def test_deploy_empty_repo_friendly_message():
    fake = _mock_response(409, {"detail": "deploy_empty_repo"})
    with patch("cli.commands.data_apps.api_post", return_value=fake):
        result = runner.invoke(app, ["app", "deploy", "sapp"])
    assert result.exit_code == 1
    output = result.output + str(result.stderr_bytes or b"")
    assert "no commits" in output.lower() or "empty" in output.lower()


def test_deploy_not_found():
    fake = _mock_response(404, {"detail": "data_app_not_found"})
    with patch("cli.commands.data_apps.api_post", return_value=fake):
        result = runner.invoke(app, ["app", "deploy", "nope"])
    assert result.exit_code == 1
    output = result.output + str(result.stderr_bytes or b"")
    assert "agnes app list" in output


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------


def test_logs_default_tail():
    fake = _mock_response(200, {"logs": "line 1\nline 2\n"})
    with patch("cli.commands.data_apps.api_get", return_value=fake) as mock_get:
        result = runner.invoke(app, ["app", "logs", "sapp"])
    assert result.exit_code == 0
    assert "line 1" in result.stdout
    assert mock_get.call_args.args[0] == "/api/data-apps/sapp/logs"
    assert mock_get.call_args.kwargs["params"]["tail"] == 200


def test_logs_custom_tail():
    fake = _mock_response(200, {"logs": "x\n"})
    with patch("cli.commands.data_apps.api_get", return_value=fake) as mock_get:
        result = runner.invoke(app, ["app", "logs", "sapp", "--tail", "50"])
    assert result.exit_code == 0
    assert mock_get.call_args.kwargs["params"]["tail"] == 50


def test_logs_runner_unavailable():
    fake = _mock_response(502, {"detail": "runner_unavailable"})
    with patch("cli.commands.data_apps.api_get", return_value=fake):
        result = runner.invoke(app, ["app", "logs", "sapp"])
    assert result.exit_code == 1
    output = result.output + str(result.stderr_bytes or b"")
    assert "unavailable" in output.lower()


# ---------------------------------------------------------------------------
# open
# ---------------------------------------------------------------------------


def test_open_prints_url_only():
    fake = _mock_response(200, {"slug": "sapp", "name": "S", "state": "running", "url": "https://sapp.example.com/"})
    with patch("cli.commands.data_apps.api_get", return_value=fake):
        result = runner.invoke(app, ["app", "open", "sapp"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "https://sapp.example.com/"


def test_open_not_found():
    fake = _mock_response(404, {"detail": "data_app_not_found"})
    with patch("cli.commands.data_apps.api_get", return_value=fake):
        result = runner.invoke(app, ["app", "open", "nope"])
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


def test_stop_happy_path():
    fake = _mock_response(200, {"state": "stopped"})
    with patch("cli.commands.data_apps.api_post", return_value=fake) as mock_post:
        result = runner.invoke(app, ["app", "stop", "sapp"])
    assert result.exit_code == 0
    assert "stopped" in result.stdout
    assert mock_post.call_args.args[0] == "/api/data-apps/sapp/stop"


def test_stop_not_found():
    fake = _mock_response(404, {"detail": "data_app_not_found"})
    with patch("cli.commands.data_apps.api_post", return_value=fake):
        result = runner.invoke(app, ["app", "stop", "nope"])
    assert result.exit_code == 1
    output = result.output + str(result.stderr_bytes or b"")
    assert "agnes app list" in output


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete_with_yes_flag_skips_confirmation():
    fake = _mock_response(204, None)
    with patch("cli.commands.data_apps.api_delete", return_value=fake) as mock_delete:
        result = runner.invoke(app, ["app", "delete", "sapp", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Deleted" in result.stdout
    mock_delete.assert_called_once()
    assert mock_delete.call_args.args[0] == "/api/data-apps/sapp"


def test_delete_confirm_accept():
    fake = _mock_response(204, None)
    with patch("cli.commands.data_apps.api_delete", return_value=fake) as mock_delete:
        result = runner.invoke(app, ["app", "delete", "sapp"], input="y\n")
    assert result.exit_code == 0, result.output
    mock_delete.assert_called_once()


def test_delete_confirm_abort():
    with patch("cli.commands.data_apps.api_delete") as mock_delete:
        result = runner.invoke(app, ["app", "delete", "sapp"], input="n\n")
    assert result.exit_code != 0
    mock_delete.assert_not_called()


def test_delete_not_found():
    fake = _mock_response(404, {"detail": "data_app_not_found"})
    with patch("cli.commands.data_apps.api_delete", return_value=fake):
        result = runner.invoke(app, ["app", "delete", "nope", "--yes"])
    assert result.exit_code == 1
