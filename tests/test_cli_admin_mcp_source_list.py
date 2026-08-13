"""`agnes admin mcp source list` surfaces the url-policy report (#1216 part 1).

The API already computes ``url_policy_verdict`` per row (DNS-free, admin-only
— see ``tests/test_admin_mcp_url_policy_report.py``); this pins that the CLI
table doesn't drop it on the floor, since the table is a thin projection of
whatever the API returns and a column silently omitted here is invisible to
an operator who lives on the CLI rather than the admin UI.
"""

from __future__ import annotations

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


def _resp(status_code=200, json_data=None):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data if json_data is not None else {}
    return r


def test_list_shows_would_refuse_for_a_flagged_row():
    rows = [
        {
            "id": "src_legacy",
            "name": "src_legacy",
            "transport": "http",
            "url": "http://169.254.169.254/mcp",
            "auth_method": None,
            "auth_secret_env": None,
            "url_policy_verdict": {"verdict": "would_refuse", "reasons": ["address_in_blocked_range: 169.254.169.254"]},
        },
        {
            "id": "src_clean",
            "name": "src_clean",
            "transport": "http",
            "url": "https://mcp.vendor.example/mcp",
            "auth_method": None,
            "auth_secret_env": None,
            "url_policy_verdict": {"verdict": "ok", "reasons": []},
        },
    ]
    with patch("cli.commands.admin_mcp.api_get", return_value=_resp(200, rows)):
        result = runner.invoke(app, ["admin", "mcp", "source", "list"])
    assert result.exit_code == 0, result.output
    assert "would_refuse" in result.output
    assert "ok" in result.output


def test_list_leaves_a_stdio_row_blank_not_missing():
    rows = [
        {
            "id": "src_stdio",
            "name": "src_stdio",
            "transport": "stdio",
            "command": "/bin/thing",
            "url_policy_verdict": None,
        }
    ]
    with patch("cli.commands.admin_mcp.api_get", return_value=_resp(200, rows)):
        result = runner.invoke(app, ["admin", "mcp", "source", "list"])
    assert result.exit_code == 0, result.output
    assert "src_stdio" in result.output
