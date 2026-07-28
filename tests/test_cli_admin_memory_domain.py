"""CLI tests for `agnes admin memory-domain` subcommands."""

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


class TestList:
    def test_list_text(self):
        rows = [
            {"id": "d_1", "slug": "playbook", "name": "Playbook", "description": "Sales"},
        ]
        with patch(
            "cli.commands.admin_memory_domain.api_get",
            return_value=_resp(200, rows),
        ):
            result = runner.invoke(app, ["admin", "memory-domain", "list"])
        assert result.exit_code == 0
        assert "Playbook" in result.output
        assert "Memory Domains: 1" in result.output

    def test_list_json(self):
        rows = [{"id": "d_1", "slug": "x", "name": "X", "description": None}]
        with patch(
            "cli.commands.admin_memory_domain.api_get",
            return_value=_resp(200, rows),
        ):
            result = runner.invoke(
                app, ["admin", "memory-domain", "list", "--json"]
            )
        data = json.loads(result.output)
        assert data[0]["slug"] == "x"


class TestCreate:
    def test_create_success(self):
        with patch(
            "cli.commands.admin_memory_domain.api_post",
            return_value=_resp(201, {"id": "d_new"}),
        ) as m:
            result = runner.invoke(
                app,
                [
                    "admin", "memory-domain", "create",
                    "--name", "Playbook",
                    "--slug", "playbook",
                    "--description", "d",
                    "--icon", "🎯",
                    "--color", "#dcfce7",
                ],
            )
        assert result.exit_code == 0
        assert m.call_args.kwargs["json"] == {
            "name": "Playbook",
            "slug": "playbook",
            "description": "d",
            "icon": "🎯",
            "color": "#dcfce7",
        }
        assert "d_new" in result.output


class TestEdit:
    def test_edit_resolves_slug(self):
        get_responses = [
            _resp(404, {"detail": "memory_domain_not_found"}),
            _resp(200, [{"id": "d_42", "slug": "playbook", "name": "Playbook"}]),
        ]
        with patch(
            "cli.commands.admin_memory_domain.api_get", side_effect=get_responses
        ), patch(
            "cli.commands.admin_memory_domain.api_put", return_value=_resp(200, {})
        ) as m_put:
            result = runner.invoke(
                app,
                ["admin", "memory-domain", "edit", "playbook", "--name", "New"],
            )
        assert result.exit_code == 0
        assert "d_42" in m_put.call_args.args[0]


class TestDelete:
    def test_delete_with_yes(self):
        with patch(
            "cli.commands.admin_memory_domain.api_get",
            return_value=_resp(200, {"id": "d_1", "slug": "s", "name": "n"}),
        ), patch(
            "cli.commands.admin_memory_domain.api_delete",
            return_value=_resp(204),
        ) as m:
            result = runner.invoke(
                app, ["admin", "memory-domain", "delete", "d_1", "--yes"]
            )
        assert result.exit_code == 0
        m.assert_called_once()


class TestAddRemoveItem:
    def test_add_item(self):
        with patch(
            "cli.commands.admin_memory_domain.api_get",
            return_value=_resp(200, {"id": "d_1", "slug": "s", "name": "n"}),
        ), patch(
            "cli.commands.admin_memory_domain.api_post",
            return_value=_resp(200, {"added": True}),
        ) as m:
            result = runner.invoke(
                app,
                ["admin", "memory-domain", "add-item", "d_1", "item_99"],
            )
        assert result.exit_code == 0
        assert m.call_args.args[0] == "/api/admin/memory-domains/d_1/items"
        assert m.call_args.kwargs["json"] == {"item_id": "item_99"}

    def test_remove_item_with_yes(self):
        with patch(
            "cli.commands.admin_memory_domain.api_get",
            return_value=_resp(200, {"id": "d_1", "slug": "s", "name": "n"}),
        ), patch(
            "cli.commands.admin_memory_domain.api_delete",
            return_value=_resp(204),
        ) as m:
            result = runner.invoke(
                app,
                ["admin", "memory-domain", "remove-item",
                 "d_1", "item_99", "--yes"],
            )
        assert result.exit_code == 0
        assert "item_99" in m.call_args.args[0]
