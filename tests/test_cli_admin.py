"""Tests for da admin subcommands."""

import json
import pytest
from unittest.mock import patch, MagicMock

from typer.testing import CliRunner
from cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("DA_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "config").mkdir()
    (tmp_path / "data").mkdir()
    yield tmp_path


def _resp(status_code=200, json_data=None, text=""):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data if json_data is not None else {}
    r.text = text
    return r


class TestListUsers:
    def test_list_users_text(self):
        users = [
            {"email": "alice@x.com", "role": "admin", "id": "aaa00001"},
            {"email": "bob@x.com", "role": "analyst", "id": "bbb00002"},
        ]
        with patch("cli.commands.admin.api_get", return_value=_resp(200, users)):
            result = runner.invoke(app, ["admin", "list-users"])
        assert result.exit_code == 0
        assert "alice@x.com" in result.output
        assert "bob@x.com" in result.output

    def test_list_users_json(self):
        users = [{"email": "alice@x.com", "role": "admin", "id": "aaa00001"}]
        with patch("cli.commands.admin.api_get", return_value=_resp(200, users)):
            result = runner.invoke(app, ["admin", "list-users", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data[0]["email"] == "alice@x.com"

    def test_list_users_api_error(self):
        with patch("cli.commands.admin.api_get", return_value=_resp(500, {"detail": "Server error"}, "Server error")):
            result = runner.invoke(app, ["admin", "list-users"])
        assert result.exit_code == 1


class TestAddUser:
    def test_add_user_success(self):
        created = {"email": "newuser@x.com", "id": "uid-1", "role": "analyst"}
        with patch("cli.commands.admin.api_post", return_value=_resp(201, created)):
            result = runner.invoke(app, ["admin", "add-user", "newuser@x.com", "--role", "analyst"])
        assert result.exit_code == 0
        assert "newuser@x.com" in result.output

    def test_add_user_failure(self):
        with patch("cli.commands.admin.api_post", return_value=_resp(400, {"detail": "Already exists"})):
            result = runner.invoke(app, ["admin", "add-user", "dup@x.com"])
        assert result.exit_code == 1


class TestRemoveUser:
    def test_remove_user_success(self):
        with patch("cli.commands.admin.api_delete", return_value=_resp(204)):
            result = runner.invoke(app, ["admin", "remove-user", "uid-1"])
        assert result.exit_code == 0
        assert "removed" in result.output.lower()

    def test_remove_user_not_found(self):
        with patch("cli.commands.admin.api_delete", return_value=_resp(404, text="Not found")):
            result = runner.invoke(app, ["admin", "remove-user", "nonexistent"])
        assert result.exit_code == 1


class TestRegisterTable:
    def test_register_table_success(self):
        with patch("cli.commands.admin.api_post", return_value=_resp(201, {"id": "t1", "name": "orders"})):
            result = runner.invoke(app, [
                "admin", "register-table", "orders",
                "--source-type", "keboola",
                "--bucket", "in.c-crm",
                "--query-mode", "local",
            ])
        assert result.exit_code == 0
        assert "Registered: orders" in result.output

    def test_register_table_already_exists(self):
        with patch("cli.commands.admin.api_post", return_value=_resp(409, {"detail": "exists"})):
            result = runner.invoke(app, ["admin", "register-table", "orders"])
        assert result.exit_code == 0
        assert "Already exists" in result.output

    def test_register_table_failure(self):
        with patch("cli.commands.admin.api_post", return_value=_resp(500, {"detail": "error"})):
            result = runner.invoke(app, ["admin", "register-table", "bad_table"])
        assert result.exit_code == 1


class TestListTables:
    def test_list_tables_text(self):
        payload = {
            "count": 2,
            "tables": [
                {"name": "orders", "source_type": "keboola", "query_mode": "local", "bucket": "in.c-crm"},
                {"name": "customers", "source_type": "keboola", "query_mode": "local", "bucket": "in.c-crm"},
            ],
        }
        with patch("cli.commands.admin.api_get", return_value=_resp(200, payload)):
            result = runner.invoke(app, ["admin", "list-tables"])
        assert result.exit_code == 0
        assert "Registered tables: 2" in result.output
        assert "orders" in result.output

    def test_list_tables_json(self):
        payload = {"count": 1, "tables": [{"name": "orders"}]}
        with patch("cli.commands.admin.api_get", return_value=_resp(200, payload)):
            result = runner.invoke(app, ["admin", "list-tables", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["count"] == 1


class TestMetadataShow:
    def test_metadata_show_columns(self):
        payload = {
            "columns": [
                {"column_name": "id", "basetype": "INTEGER", "confidence": "high", "description": "PK"},
                {"column_name": "name", "basetype": "VARCHAR", "confidence": "high", "description": ""},
            ]
        }
        with patch("cli.commands.admin.api_get", return_value=_resp(200, payload)):
            result = runner.invoke(app, ["admin", "metadata-show", "orders"])
        assert result.exit_code == 0
        assert "id" in result.output
        assert "name" in result.output

    def test_metadata_show_json(self):
        payload = {"columns": [{"column_name": "id", "basetype": "INTEGER"}]}
        with patch("cli.commands.admin.api_get", return_value=_resp(200, payload)):
            result = runner.invoke(app, ["admin", "metadata-show", "orders", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "columns" in data

    def test_metadata_show_not_found(self):
        with patch("cli.commands.admin.api_get", return_value=_resp(404, {"detail": "Not found"})):
            result = runner.invoke(app, ["admin", "metadata-show", "nonexistent"])
        assert result.exit_code == 1


def test_admin_set_role_invokes_patch(monkeypatch):
    """`da admin set-role` sends PATCH to /api/users/{id} with role."""
    import httpx
    from cli.commands.admin import admin_app
    from typer.testing import CliRunner

    captured = {}

    def fake_patch(path, json=None, **kwargs):
        captured["path"] = path
        captured["json"] = json
        return httpx.Response(200, json={
            "id": "abc", "email": "x@y.z", "name": "X",
            "role": json.get("role") if json else "viewer",
            "active": True, "created_at": "", "deactivated_at": None,
        })

    from cli import client as cli_client
    monkeypatch.setattr(cli_client, "api_patch", fake_patch, raising=False)
    # patch admin.api_patch too since admin.py imports names
    from cli.commands import admin as admin_mod
    monkeypatch.setattr(admin_mod, "api_patch", fake_patch, raising=False)

    runner = CliRunner()
    result = runner.invoke(admin_app, ["set-role", "abc", "analyst"])
    assert result.exit_code == 0
    assert captured["path"] == "/api/users/abc"
    assert captured["json"] == {"role": "analyst"}


class TestMetadataApply:
    def test_metadata_apply_dry_run(self, tmp_path):
        proposal = {
            "tables": {
                "orders": {
                    "columns": {
                        "id": {"basetype": "INTEGER", "description": "Primary key"},
                    }
                }
            }
        }
        proposal_file = tmp_path / "proposal.json"
        proposal_file.write_text(json.dumps(proposal))
        result = runner.invoke(app, ["admin", "metadata-apply", str(proposal_file), "--dry-run"])
        assert result.exit_code == 0
        assert "DRY RUN" in result.output
        assert "orders.id" in result.output

    def test_metadata_apply_file_not_found(self):
        result = runner.invoke(app, ["admin", "metadata-apply", "/nonexistent/proposal.json"])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()
