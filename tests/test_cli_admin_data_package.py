"""CLI tests for `agnes admin data-package` subcommands."""

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
            {"id": "pkg_1", "slug": "sales", "name": "Sales", "description": "Orders"},
            {"id": "pkg_2", "slug": "marketing", "name": "Marketing", "description": ""},
        ]
        with patch(
            "cli.commands.admin_data_package.api_get",
            return_value=_resp(200, rows),
        ):
            result = runner.invoke(app, ["admin", "data-package", "list"])
        assert result.exit_code == 0
        assert "Sales" in result.output
        assert "Marketing" in result.output
        assert "Data Packages: 2" in result.output

    def test_list_json(self):
        rows = [{"id": "pkg_1", "slug": "sales", "name": "Sales", "description": None}]
        with patch(
            "cli.commands.admin_data_package.api_get",
            return_value=_resp(200, rows),
        ):
            result = runner.invoke(app, ["admin", "data-package", "list", "--json"])
        data = json.loads(result.output)
        assert data[0]["slug"] == "sales"

    def test_list_search_passes_param(self):
        with patch(
            "cli.commands.admin_data_package.api_get",
            return_value=_resp(200, []),
        ) as m:
            result = runner.invoke(
                app, ["admin", "data-package", "list", "--search", "sales"]
            )
        assert result.exit_code == 0
        assert m.call_args.kwargs["params"] == {"search": "sales"}


class TestCreate:
    def test_create_success(self):
        with patch(
            "cli.commands.admin_data_package.api_post",
            return_value=_resp(201, {"id": "pkg_new"}),
        ) as m:
            result = runner.invoke(
                app,
                [
                    "admin", "data-package", "create",
                    "--name", "Marketing",
                    "--slug", "marketing",
                    "--description", "Bundle desc",
                    "--icon", "📦",
                    "--color", "#fce7f3",
                ],
            )
        assert result.exit_code == 0
        assert m.call_args.kwargs["json"] == {
            "name": "Marketing",
            "slug": "marketing",
            "description": "Bundle desc",
            "icon": "📦",
            "color": "#fce7f3",
        }
        assert "pkg_new" in result.output

    def test_create_slug_conflict(self):
        with patch(
            "cli.commands.admin_data_package.api_post",
            return_value=_resp(409, {"detail": "slug_exists"}),
        ):
            result = runner.invoke(
                app,
                ["admin", "data-package", "create",
                 "--name", "X", "--slug", "dup"],
            )
        assert result.exit_code == 1
        assert "slug_exists" in result.output


class TestEdit:
    def test_edit_updates_name(self):
        # _resolve_pkg_id calls api_get; first call returns 200.
        with patch(
            "cli.commands.admin_data_package.api_get",
            return_value=_resp(200, {"id": "pkg_1", "slug": "s", "name": "old"}),
        ), patch(
            "cli.commands.admin_data_package.api_put",
            return_value=_resp(200, {}),
        ) as m:
            result = runner.invoke(
                app, ["admin", "data-package", "edit", "pkg_1", "--name", "New"]
            )
        assert result.exit_code == 0
        assert m.call_args.kwargs["json"] == {"name": "New"}

    def test_edit_resolves_slug_to_id(self):
        # First GET 404 → second GET lists → matches slug → PUT.
        get_responses = [
            _resp(404, {"detail": "data_package_not_found"}),
            _resp(200, [{"id": "pkg_42", "slug": "sales", "name": "Sales"}]),
        ]
        with patch(
            "cli.commands.admin_data_package.api_get",
            side_effect=get_responses,
        ), patch(
            "cli.commands.admin_data_package.api_put",
            return_value=_resp(200, {}),
        ) as m_put:
            result = runner.invoke(
                app, ["admin", "data-package", "edit", "sales", "--name", "X"]
            )
        assert result.exit_code == 0
        # PUT URL embeds the resolved id, not the slug.
        assert "pkg_42" in m_put.call_args.args[0]

    def test_edit_no_fields_fails(self):
        # Doesn't even hit the API.
        result = runner.invoke(app, ["admin", "data-package", "edit", "pkg_1"])
        assert result.exit_code == 2
        assert "at least one" in result.output.lower()


class TestDelete:
    def test_delete_requires_confirm_without_yes(self):
        # `typer.confirm` reads from stdin; CliRunner provides empty input
        # by default → confirm returns False → Abort.
        with patch(
            "cli.commands.admin_data_package.api_get",
            return_value=_resp(200, {"id": "pkg_1", "slug": "s", "name": "n"}),
        ), patch(
            "cli.commands.admin_data_package.api_delete",
        ) as m:
            result = runner.invoke(
                app, ["admin", "data-package", "delete", "pkg_1"], input="n\n"
            )
        m.assert_not_called()
        assert result.exit_code != 0

    def test_delete_with_yes_calls_delete(self):
        with patch(
            "cli.commands.admin_data_package.api_get",
            return_value=_resp(200, {"id": "pkg_1", "slug": "s", "name": "n"}),
        ), patch(
            "cli.commands.admin_data_package.api_delete",
            return_value=_resp(204),
        ) as m:
            result = runner.invoke(
                app, ["admin", "data-package", "delete", "pkg_1", "--yes"]
            )
        assert result.exit_code == 0
        m.assert_called_once()


class TestAddRemoveTable:
    def test_add_table(self):
        with patch(
            "cli.commands.admin_data_package.api_get",
            return_value=_resp(200, {"id": "pkg_1", "slug": "s", "name": "n"}),
        ), patch(
            "cli.commands.admin_data_package.api_post",
            return_value=_resp(200, {"added": True}),
        ) as m:
            result = runner.invoke(
                app,
                ["admin", "data-package", "add-table", "pkg_1", "tbl_42"],
            )
        assert result.exit_code == 0
        assert m.call_args.args[0] == "/api/admin/data-packages/pkg_1/tables"
        assert m.call_args.kwargs["json"] == {"table_id": "tbl_42"}
        assert "Added table" in result.output

    def test_add_table_already_present(self):
        with patch(
            "cli.commands.admin_data_package.api_get",
            return_value=_resp(200, {"id": "pkg_1", "slug": "s", "name": "n"}),
        ), patch(
            "cli.commands.admin_data_package.api_post",
            return_value=_resp(200, {"added": False}),
        ):
            result = runner.invoke(
                app,
                ["admin", "data-package", "add-table", "pkg_1", "tbl_42"],
            )
        assert result.exit_code == 0
        assert "already in" in result.output

    def test_remove_table_with_yes(self):
        with patch(
            "cli.commands.admin_data_package.api_get",
            return_value=_resp(200, {"id": "pkg_1", "slug": "s", "name": "n"}),
        ), patch(
            "cli.commands.admin_data_package.api_delete",
            return_value=_resp(204),
        ) as m:
            result = runner.invoke(
                app,
                ["admin", "data-package", "remove-table",
                 "pkg_1", "tbl_42", "--yes"],
            )
        assert result.exit_code == 0
        assert "tbl_42" in m.call_args.args[0]
        assert "pkg_1" in m.call_args.args[0]
