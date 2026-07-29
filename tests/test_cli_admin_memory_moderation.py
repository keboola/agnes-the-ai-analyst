"""CLI tests for `agnes admin memory approve/reject/revoke/require/unrequire`.

Lifecycle moderation deliberately bypasses the generic edit/bulk-edit paths
(the API allowlist excludes ``status``) and rides the governance batch
endpoint ``POST /api/memory/admin/batch`` (per-item ``mark-unmandatory`` for
``unrequire``, which has no batch analogue).
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "config"))
    # Same suppression as the parity harness: keep the auto-update probe from
    # ever mixing output into `result.output`, which several tests parse as
    # JSON (Devin Review on #1091).
    monkeypatch.setenv("AGNES_NO_UPDATE_CHECK", "1")
    (tmp_path / "config").mkdir()
    yield tmp_path


def _resp(status_code=200, json_data=None, text=""):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data if json_data is not None else {}
    r.text = text
    return r


class TestApprove:
    def test_approve_single(self):
        with patch(
            "cli.commands.memory_admin.api_post",
            return_value=_resp(200, {"success": ["item_1"], "not_found": []}),
        ) as m:
            result = runner.invoke(app, ["admin", "memory", "approve", "item_1"])
        assert result.exit_code == 0
        assert m.call_args.args[0] == "/api/memory/admin/batch"
        assert m.call_args.kwargs["json"] == {"item_ids": ["item_1"], "action": "approve"}
        assert "approve: item_1" in result.output

    def test_approve_multiple_json(self):
        payload = {"success": ["a", "b"], "not_found": []}
        with patch(
            "cli.commands.memory_admin.api_post",
            return_value=_resp(200, payload),
        ) as m:
            result = runner.invoke(app, ["admin", "memory", "approve", "a", "b", "--json"])
        assert result.exit_code == 0
        assert m.call_args.kwargs["json"]["item_ids"] == ["a", "b"]
        assert json.loads(result.output) == payload

    def test_approve_not_found_exits_nonzero(self):
        with patch(
            "cli.commands.memory_admin.api_post",
            return_value=_resp(200, {"success": ["a"], "not_found": ["ghost"]}),
        ):
            result = runner.invoke(app, ["admin", "memory", "approve", "a", "ghost"])
        assert result.exit_code == 1
        assert "ghost" in result.output
        # "Not found" must hint the next step (command-ux playbook); approve
        # acts on the pending queue, so the hint points there.
        assert "agnes admin memory tree --status pending" in result.output

    def test_approve_server_error(self):
        with patch(
            "cli.commands.memory_admin.api_post",
            return_value=_resp(403, {"detail": "Admin role required"}),
        ):
            result = runner.invoke(app, ["admin", "memory", "approve", "item_1"])
        assert result.exit_code == 1
        assert "Admin role required" in result.output


class TestReject:
    def test_reject_with_reason(self):
        with patch(
            "cli.commands.memory_admin.api_post",
            return_value=_resp(200, {"success": ["item_1"], "not_found": []}),
        ) as m:
            result = runner.invoke(
                app,
                ["admin", "memory", "reject", "item_1", "--reason", "duplicate of item_0"],
            )
        assert result.exit_code == 0
        assert m.call_args.kwargs["json"] == {
            "item_ids": ["item_1"],
            "action": "reject",
            "reason": "duplicate of item_0",
        }
        assert "reject: item_1" in result.output


class TestRevoke:
    def test_revoke_not_found_hint_is_not_pending_scoped(self):
        # revoke acts on already-approved items — a ``--status pending``
        # lookup would never surface the id (Devin review, PR #1091).
        with patch(
            "cli.commands.memory_admin.api_post",
            return_value=_resp(200, {"success": [], "not_found": ["ghost"]}),
        ):
            result = runner.invoke(app, ["admin", "memory", "revoke", "ghost"])
        assert result.exit_code == 1
        assert "agnes admin memory tree" in result.output
        assert "--status pending" not in result.output

    def test_revoke(self):
        with patch(
            "cli.commands.memory_admin.api_post",
            return_value=_resp(200, {"success": ["item_1"], "not_found": []}),
        ) as m:
            result = runner.invoke(app, ["admin", "memory", "revoke", "item_1", "--reason", "stale"])
        assert result.exit_code == 0
        assert m.call_args.kwargs["json"]["action"] == "revoke"
        assert m.call_args.kwargs["json"]["reason"] == "stale"


class TestRequire:
    def test_require_maps_to_mandate_action(self):
        with patch(
            "cli.commands.memory_admin.api_post",
            return_value=_resp(200, {"success": ["item_1"], "not_found": []}),
        ) as m:
            result = runner.invoke(
                app,
                ["admin", "memory", "require", "item_1", "--audience", "sales"],
            )
        assert result.exit_code == 0
        # Batch endpoint spells the action ``mandate``; CLI output uses the
        # v49 "require" vocabulary.
        assert m.call_args.kwargs["json"]["action"] == "mandate"
        assert m.call_args.kwargs["json"]["audience"] == "sales"
        assert "require: item_1" in result.output
        assert "mandate:" not in result.output


class TestUnrequire:
    def test_unrequire_per_item(self):
        with patch(
            "cli.commands.memory_admin.api_post",
            return_value=_resp(200, {"id": "item_1", "is_required": False}),
        ) as m:
            result = runner.invoke(app, ["admin", "memory", "unrequire", "item_1", "item_2"])
        assert result.exit_code == 0
        called_paths = [c.args[0] for c in m.call_args_list]
        assert called_paths == [
            "/api/memory/items/item_1/mark-unmandatory",
            "/api/memory/items/item_2/mark-unmandatory",
        ]
        assert "unrequire: item_1" in result.output

    def test_unrequire_not_found(self):
        with patch(
            "cli.commands.memory_admin.api_post",
            side_effect=[
                _resp(200, {"id": "item_1", "is_required": False}),
                _resp(404, {"detail": "Knowledge item not found"}),
            ],
        ):
            result = runner.invoke(app, ["admin", "memory", "unrequire", "item_1", "ghost"])
        assert result.exit_code == 1
        assert "ghost" in result.output

    def test_unrequire_error_still_reports_prior_successes(self):
        """A mid-loop server error must not swallow already-applied demotions.

        The per-item fan-out has no rollback: items that returned 200 before
        the failing one are demoted server-side. The command must print them
        (plus a warning) before exiting non-zero (Devin Review on #1091).
        """
        with patch(
            "cli.commands.memory_admin.api_post",
            side_effect=[
                _resp(200, {"id": "item_1", "is_required": False}),
                _resp(500, {"detail": "boom"}, text="boom"),
            ],
        ):
            result = runner.invoke(app, ["admin", "memory", "unrequire", "item_1", "item_2"])
        assert result.exit_code == 1
        assert "unrequire: item_1" in result.output
        assert "applied before the error" in result.output

    def test_unrequire_error_with_json_keeps_stdout_parseable(self):
        """--json consumers must get the partial results dict, not prose."""
        with patch(
            "cli.commands.memory_admin.api_post",
            side_effect=[
                _resp(200, {"id": "item_1", "is_required": False}),
                _resp(404, {"detail": "Knowledge item not found"}),
                _resp(500, {"detail": "boom"}, text="boom"),
            ],
        ):
            result = runner.invoke(
                app,
                ["admin", "memory", "unrequire", "item_1", "ghost", "item_3", "--json"],
            )
        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload["success"] == ["item_1"]
        assert payload["not_found"] == ["ghost"]
