"""Tests for `agnes admin grant create --requirement available|required`.

The two-step server contract (POST creates 'available', PUT flips to
'required') is intentional — the POST endpoint doesn't accept the field
directly per the v49 API freeze (Phase 5). When the (group,
resource_type, resource_id) tuple already exists, POST returns 409 and
the CLI falls back to a list+match-and-PUT path so the flag has the
same effect whether the grant is new or existing.
"""

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


# A group lookup result so _resolve_group_id() returns the id without
# trying alternate sources.
_GROUP_RESP = _resp(200, [{"id": "g1", "name": "sales"}])


class TestGrantCreate:
    def test_default_creates_available(self):
        """No --requirement → just one POST, no PUT."""
        with patch(
            "cli.commands.admin.api_get",
            return_value=_GROUP_RESP,
        ), patch(
            "cli.commands.admin.api_post",
            return_value=_resp(201, {"id": "grant_1"}),
        ) as post, patch(
            "cli.commands.admin.api_put"
        ) as put:
            result = runner.invoke(
                app,
                ["admin", "grant", "create", "sales", "data_package", "pkg_a"],
            )
        assert result.exit_code == 0
        post.assert_called_once()
        put.assert_not_called()

    def test_explicit_available_no_put(self):
        with patch(
            "cli.commands.admin.api_get",
            return_value=_GROUP_RESP,
        ), patch(
            "cli.commands.admin.api_post",
            return_value=_resp(201, {"id": "grant_1"}),
        ), patch(
            "cli.commands.admin.api_put"
        ) as put:
            result = runner.invoke(
                app,
                [
                    "admin", "grant", "create", "sales",
                    "data_package", "pkg_a",
                    "--requirement", "available",
                ],
            )
        assert result.exit_code == 0
        put.assert_not_called()

    def test_required_triggers_post_then_put(self):
        """New grant + --requirement required → POST then PUT."""
        with patch(
            "cli.commands.admin.api_get",
            return_value=_GROUP_RESP,
        ), patch(
            "cli.commands.admin.api_post",
            return_value=_resp(201, {"id": "grant_42"}),
        ) as post, patch(
            "cli.commands.admin.api_put",
            return_value=_resp(200, {"id": "grant_42", "requirement": "required"}),
        ) as put:
            result = runner.invoke(
                app,
                [
                    "admin", "grant", "create", "sales",
                    "data_package", "pkg_a",
                    "--requirement", "required",
                ],
            )
        assert result.exit_code == 0
        post.assert_called_once()
        put.assert_called_once()
        assert "grant_42" in put.call_args.args[0]
        assert put.call_args.kwargs["json"] == {"requirement": "required"}
        assert "required" in result.output

    def test_invalid_requirement_value(self):
        result = runner.invoke(
            app,
            [
                "admin", "grant", "create", "sales",
                "data_package", "pkg_a",
                "--requirement", "mandatory",
            ],
        )
        assert result.exit_code == 2
        assert "must be" in result.output.lower()

    def test_existing_grant_flipped_via_put(self):
        """POST returns 409 → list+match → PUT to flip requirement."""
        existing = {
            "id": "grant_77",
            "resource_id": "pkg_a",
            "requirement": "available",
        }
        get_calls = [
            _GROUP_RESP,                        # _resolve_group_id
            _resp(200, [existing]),             # listing after 409
        ]
        with patch(
            "cli.commands.admin.api_get", side_effect=get_calls
        ), patch(
            "cli.commands.admin.api_post",
            return_value=_resp(409, {"detail": "Grant already exists"}),
        ), patch(
            "cli.commands.admin.api_put",
            return_value=_resp(200, {"id": "grant_77"}),
        ) as put:
            result = runner.invoke(
                app,
                [
                    "admin", "grant", "create", "sales",
                    "data_package", "pkg_a",
                    "--requirement", "required",
                ],
            )
        assert result.exit_code == 0
        put.assert_called_once()
        assert "grant_77" in put.call_args.args[0]
        assert put.call_args.kwargs["json"] == {"requirement": "required"}

    def test_existing_grant_already_at_desired_level_skips_put(self):
        """409 + already at desired requirement → no PUT, success message."""
        existing = {
            "id": "grant_77",
            "resource_id": "pkg_a",
            "requirement": "required",
        }
        get_calls = [
            _GROUP_RESP,
            _resp(200, [existing]),
        ]
        with patch(
            "cli.commands.admin.api_get", side_effect=get_calls
        ), patch(
            "cli.commands.admin.api_post",
            return_value=_resp(409, {"detail": "Grant already exists"}),
        ), patch(
            "cli.commands.admin.api_put"
        ) as put:
            result = runner.invoke(
                app,
                [
                    "admin", "grant", "create", "sales",
                    "data_package", "pkg_a",
                    "--requirement", "required",
                ],
            )
        assert result.exit_code == 0
        put.assert_not_called()
        assert "already exists" in result.output.lower()


class TestGrantListRequirement:
    def test_list_includes_requirement_column(self):
        rows = [
            {
                "id": "grant_1",
                "group_name": "sales",
                "resource_type": "data_package",
                "resource_id": "pkg_a",
                "requirement": "required",
                "assigned_by": "admin",
            }
        ]
        with patch(
            "cli.commands.admin.api_get", return_value=_resp(200, rows)
        ):
            result = runner.invoke(app, ["admin", "grant", "list"])
        assert result.exit_code == 0
        assert "REQUIREMENT" in result.output
        assert "required" in result.output
