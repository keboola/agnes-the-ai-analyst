"""`agnes admin register-table --server-only` — CLI surface for the #607
distribution flag.

The flag existed only as a REST field (`RegisterTableRequest.server_only`,
validated in `tests/test_admin_server_only_validation.py`), so the one
documented way to register a queryable-but-never-distributed table was a
raw API call. These tests pin the CLI parity: the flag reaches the payload,
its absence leaves the payload byte-identical to before, and the
remote-mode conflict fails fast client-side with the same rationale the
server-side validator gives.
"""

from unittest.mock import MagicMock

from typer.testing import CliRunner

from cli.main import app


def _fake_resp(status_code=201, body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json = lambda: body or {"id": "x", "name": "x", "status": "registered"}
    return resp


def _capture_post(monkeypatch):
    captured = {}

    def fake_post(path, json):
        captured["path"] = path
        captured["json"] = json
        return _fake_resp()

    monkeypatch.setattr("cli.commands.admin.api_post", fake_post)
    return captured


def test_server_only_flag_reaches_payload(monkeypatch):
    captured = _capture_post(monkeypatch)

    result = CliRunner().invoke(
        app,
        [
            "admin",
            "register-table",
            "salaries",
            "--source-type",
            "keboola",
            "--bucket",
            "in.c-hr",
            "--source-table",
            "salaries",
            "--query-mode",
            "local",
            "--server-only",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert captured["path"] == "/api/admin/register-table"
    assert captured["json"]["server_only"] is True


def test_server_only_valid_with_materialized(monkeypatch):
    captured = _capture_post(monkeypatch)

    result = CliRunner().invoke(
        app,
        [
            "admin",
            "register-table",
            "monthly_kpis",
            "--source-type",
            "bigquery",
            "--bucket",
            "fin",
            "--query-mode",
            "materialized",
            "--query",
            "SELECT 1",
            "--server-only",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert captured["json"]["server_only"] is True


def test_omitted_flag_leaves_payload_unchanged(monkeypatch):
    """Default false is the server's default too — omit the key entirely so
    an existing registration flow sends the exact same body it sent before
    this flag existed."""
    captured = _capture_post(monkeypatch)

    result = CliRunner().invoke(
        app,
        [
            "admin",
            "register-table",
            "users",
            "--source-type",
            "keboola",
            "--bucket",
            "in.c-crm",
            "--source-table",
            "users",
            "--query-mode",
            "local",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert "server_only" not in captured["json"]


def test_server_only_with_remote_fails_fast(monkeypatch):
    """A remote row has no server-stored parquet to suppress. The server
    rejects this pairing (RegisterTableRequest validator); the CLI catches
    it before the round-trip so the operator sees the conflict immediately."""
    posted = {"called": False}

    def fake_post(path, json):
        posted["called"] = True
        return _fake_resp()

    monkeypatch.setattr("cli.commands.admin.api_post", fake_post)

    result = CliRunner().invoke(
        app,
        [
            "admin",
            "register-table",
            "web_sessions",
            "--source-type",
            "bigquery",
            "--bucket",
            "dwh",
            "--source-table",
            "web_sessions",
            "--query-mode",
            "remote",
            "--server-only",
        ],
    )

    assert result.exit_code == 2
    assert not posted["called"], "CLI must not round-trip an invalid combination"
    # The error goes to stderr (typer.echo(err=True)); `output` carries both.
    assert "--server-only" in result.output
