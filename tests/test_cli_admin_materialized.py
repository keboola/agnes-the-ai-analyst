"""`da admin register-table --query-mode materialized --query @file.sql` works."""
from typer.testing import CliRunner
from unittest.mock import patch, MagicMock

from cli.main import app


def _fake_201(json_body=None):
    resp = MagicMock()
    resp.status_code = 201
    resp.json = lambda: {"id": "x", "name": "x", "status": "registered"}
    return resp


def test_register_materialized_with_inline_query(monkeypatch):
    captured = {}

    def fake_post(path, json):
        captured["path"] = path
        captured["json"] = json
        return _fake_201()

    monkeypatch.setattr("cli.commands.admin.api_post", fake_post)

    runner = CliRunner()
    result = runner.invoke(app, [
        "admin", "register-table", "orders_90d",
        "--source-type", "bigquery",
        "--query-mode", "materialized",
        "--query", "SELECT date FROM bq.\"prj.ds.orders\"",
        "--schedule", "every 6h",
    ])

    assert result.exit_code == 0, result.stdout
    assert captured["json"]["query_mode"] == "materialized"
    assert captured["json"]["source_query"] == 'SELECT date FROM bq."prj.ds.orders"'
    assert captured["json"]["sync_schedule"] == "every 6h"


def test_register_materialized_reads_query_from_file(tmp_path, monkeypatch):
    sql_file = tmp_path / "orders.sql"
    sql_file.write_text("SELECT date, SUM(revenue) FROM bq.\"prj.ds.orders\" GROUP BY 1\n")

    captured = {}

    def fake_post(path, json):
        captured["json"] = json
        return _fake_201()

    monkeypatch.setattr("cli.commands.admin.api_post", fake_post)

    runner = CliRunner()
    result = runner.invoke(app, [
        "admin", "register-table", "orders_90d",
        "--source-type", "bigquery",
        "--query-mode", "materialized",
        "--query", f"@{sql_file}",
        "--schedule", "daily 03:00",
    ])

    assert result.exit_code == 0, result.stdout
    assert "SELECT date, SUM(revenue)" in captured["json"]["source_query"]
    # File contents trimmed (no trailing newline noise sent over the wire)
    assert not captured["json"]["source_query"].endswith("\n")


def test_register_materialized_without_query_fails(monkeypatch):
    """--query-mode materialized without --query is a client-side error,
    no API call made."""
    called = {"count": 0}

    def fake_post(*args, **kwargs):
        called["count"] += 1
        return _fake_201()

    monkeypatch.setattr("cli.commands.admin.api_post", fake_post)

    runner = CliRunner()
    result = runner.invoke(app, [
        "admin", "register-table", "orders_90d",
        "--source-type", "bigquery",
        "--query-mode", "materialized",
    ])

    assert result.exit_code != 0
    assert called["count"] == 0
    # Error message names the missing flag
    assert "--query" in (result.stdout + result.stderr)


def test_register_local_mode_does_not_send_source_query(monkeypatch):
    """Default local mode shouldn't send source_query=None — keep the JSON
    payload clean (server-side validator forbids source_query in local mode)."""
    captured = {}

    def fake_post(path, json):
        captured["json"] = json
        return _fake_201()

    monkeypatch.setattr("cli.commands.admin.api_post", fake_post)

    runner = CliRunner()
    result = runner.invoke(app, [
        "admin", "register-table", "kbc_orders",
        "--source-type", "keboola",
        "--bucket", "in.c-crm",
    ])

    assert result.exit_code == 0
    assert "source_query" not in captured["json"] or captured["json"].get("source_query") in (None, "")
    assert "sync_schedule" not in captured["json"] or captured["json"].get("sync_schedule") in (None, "")


def test_register_query_at_path_missing_file_fails(monkeypatch):
    """@file.sql where the file doesn't exist surfaces a clear error."""
    monkeypatch.setattr("cli.commands.admin.api_post", lambda *a, **kw: _fake_201())
    runner = CliRunner()
    result = runner.invoke(app, [
        "admin", "register-table", "x",
        "--source-type", "bigquery",
        "--query-mode", "materialized",
        "--query", "@/tmp/definitely-does-not-exist-9b4f7e2c.sql",
    ])
    assert result.exit_code != 0
