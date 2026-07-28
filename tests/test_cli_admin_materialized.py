"""`agnes admin register-table --query-mode materialized --query @file.sql`
sends source_query in the payload; existing local/remote paths still work
unchanged."""
from typer.testing import CliRunner
from unittest.mock import MagicMock

from cli.main import app


def _fake_resp(status_code, body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json = lambda: body or {"id": "x", "name": "x", "status": "registered"}
    return resp


def test_register_materialized_with_inline_query(monkeypatch):
    captured = {}

    def fake_post(path, json):
        captured["path"] = path
        captured["json"] = json
        return _fake_resp(201)

    monkeypatch.setattr("cli.commands.admin.api_post", fake_post)

    runner = CliRunner()
    result = runner.invoke(app, [
        "admin", "register-table", "orders_90d",
        "--source-type", "bigquery",
        "--query-mode", "materialized",
        "--bucket", "fin",
        "--query", "SELECT date FROM `prj.ds.orders`",
        "--sync-schedule", "every 6h",
    ])

    assert result.exit_code == 0, result.stdout
    assert captured["path"] == "/api/admin/register-table"
    assert captured["json"]["query_mode"] == "materialized"
    assert captured["json"]["source_query"] == "SELECT date FROM `prj.ds.orders`"
    assert captured["json"]["sync_schedule"] == "every 6h"


def test_register_materialized_reads_query_from_file(tmp_path, monkeypatch):
    sql_file = tmp_path / "orders.sql"
    sql_file.write_text(
        "SELECT date, SUM(revenue) FROM `prj.ds.orders` GROUP BY 1\n"
    )

    captured = {}

    def fake_post(path, json):
        captured["json"] = json
        return _fake_resp(201)

    monkeypatch.setattr("cli.commands.admin.api_post", fake_post)

    runner = CliRunner()
    result = runner.invoke(app, [
        "admin", "register-table", "orders_90d",
        "--source-type", "bigquery",
        "--query-mode", "materialized",
        "--bucket", "fin",
        "--query", f"@{sql_file}",
        "--sync-schedule", "daily 03:00",
    ])

    assert result.exit_code == 0, result.stdout
    assert "SELECT date, SUM(revenue)" in captured["json"]["source_query"]
    assert not captured["json"]["source_query"].endswith("\n")


def test_register_materialized_without_query_fails(monkeypatch):
    """--query-mode materialized without --query is a client-side error,
    no API call made."""
    called = {"count": 0}

    def fake_post(*args, **kwargs):
        called["count"] += 1
        return _fake_resp(201)

    monkeypatch.setattr("cli.commands.admin.api_post", fake_post)

    runner = CliRunner()
    result = runner.invoke(app, [
        "admin", "register-table", "orders_90d",
        "--source-type", "bigquery",
        "--query-mode", "materialized",
    ])

    assert result.exit_code != 0
    assert called["count"] == 0
    combined = result.stdout + (result.stderr or "")
    assert "--query" in combined


def test_register_local_mode_does_not_send_source_query(monkeypatch):
    """Default local mode shouldn't send source_query — server-side
    validator forbids it on local."""
    captured = {}

    def fake_post(path, json):
        captured["json"] = json
        return _fake_resp(201)

    monkeypatch.setattr("cli.commands.admin.api_post", fake_post)

    runner = CliRunner()
    result = runner.invoke(app, [
        "admin", "register-table", "kbc_orders",
        "--source-type", "keboola",
        "--bucket", "in.c-crm",
    ])

    assert result.exit_code == 0
    assert "source_query" not in captured["json"]
    assert "sync_schedule" not in captured["json"]


def test_register_query_at_path_missing_file_fails(monkeypatch):
    """@file.sql where the file doesn't exist surfaces a clear error."""
    monkeypatch.setattr(
        "cli.commands.admin.api_post", lambda *a, **kw: _fake_resp(201),
    )
    runner = CliRunner()
    result = runner.invoke(app, [
        "admin", "register-table", "x",
        "--source-type", "bigquery",
        "--query-mode", "materialized",
        "--query", "@/tmp/definitely-does-not-exist-9b4f7e2c.sql",
    ])
    assert result.exit_code != 0


def test_register_remote_path_unchanged(monkeypatch):
    """The pre-existing --bucket / --source-table / --query-mode remote
    flow still works without --query."""
    captured = {}

    def fake_post(path, json):
        captured["json"] = json
        return _fake_resp(200)

    monkeypatch.setattr("cli.commands.admin.api_post", fake_post)

    runner = CliRunner()
    result = runner.invoke(app, [
        "admin", "register-table", "live_orders",
        "--source-type", "bigquery",
        "--bucket", "analytics",
        "--source-table", "orders",
        "--query-mode", "remote",
    ])

    assert result.exit_code == 0
    assert captured["json"]["query_mode"] == "remote"
    assert "source_query" not in captured["json"]
    assert captured["json"]["bucket"] == "analytics"
    assert captured["json"]["source_table"] == "orders"


def test_register_materialized_without_bucket_fails_with_clear_error(monkeypatch):
    """`--query-mode materialized` without `--bucket` is a client-side
    error. Pre-fix the CLI sent `bucket=""` to the server; registration
    succeeded but `agnes schema <name>` later 400ed with "unsafe BQ
    identifier in registry" because the schema endpoint built
    `bq.\"\".\"<src>\"` from the empty bucket. Catching this at register
    time gives operators a clear pointer at the right knob instead of
    accept-then-fail-later UX."""
    called = {"count": 0}

    def fake_post(*args, **kwargs):
        called["count"] += 1
        return _fake_resp(201)

    monkeypatch.setattr("cli.commands.admin.api_post", fake_post)

    runner = CliRunner()
    result = runner.invoke(app, [
        "admin", "register-table", "category_summary",
        "--source-type", "bigquery",
        "--query-mode", "materialized",
        "--query", "SELECT 1",
        # No --bucket on purpose.
    ])

    assert result.exit_code != 0
    # API never called — fail fast on client side.
    assert called["count"] == 0
    combined = result.stdout + (result.stderr or "")
    assert "--bucket" in combined
    # The error must explain WHY it's required, not just say "missing".
    assert "schema" in combined.lower() or "identifier" in combined.lower()


def test_register_table_emits_first_sync_and_grant_hints(monkeypatch):
    """After a successful register-table for a materialized row, the CLI
    output must point operators at:
    (a) `agnes setup first-sync` — registration adds a registry row but
        does NOT trigger a parquet build, and `agnes pull` then reports
        "Updated 0 tables (1 total)" until the next scheduler tick.
    (b) `agnes admin grant create <group> table <id>` — `agnes catalog`
        is RBAC-filtered, so non-admin users won't see the new row
        until a grant is created.

    Without these hints operators bounce between symptoms and assume
    something's broken when it's just unstated post-register UX."""
    monkeypatch.setattr(
        "cli.commands.admin.api_post",
        lambda *a, **kw: _fake_resp(201),
    )

    runner = CliRunner()
    result = runner.invoke(app, [
        "admin", "register-table", "category_summary",
        "--source-type", "bigquery",
        "--query-mode", "materialized",
        "--bucket", "analytics",
        "--query", "SELECT category, SUM(rev) FROM `prj.ds.tx` GROUP BY 1",
    ])
    assert result.exit_code == 0, result.stdout
    out = result.stdout
    assert "agnes setup first-sync" in out
    assert "agnes admin grant create" in out
    # The grant hint should mention the registered name so operators can
    # copy-paste the next command verbatim.
    assert "category_summary" in out
