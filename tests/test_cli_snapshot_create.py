"""Tests for `agnes snapshot create` (folded from `da fetch`)."""

from typer.testing import CliRunner

# CI-safety: Typer/rich emits ANSI escapes in --help output. Strip before asserts.
_ANSI_RE = __import__("re").compile(r"\x1b\[[0-9;]*m")
def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)

from cli.commands.snapshot import snapshot_app


def test_snapshot_create_help():
    runner = CliRunner()
    result = runner.invoke(snapshot_app, ["create", "--help"])
    assert result.exit_code == 0
    for flag in [
        "--select",
        "--where",
        "--limit",
        "--order-by",
        "--as",
        "--estimate",
        "--no-estimate",
        "--force",
    ]:
        assert flag in _clean(result.output)


def test_snapshot_create_no_duckdb_friendly_exit(tmp_path, monkeypatch):
    """Real-fetch path (no --estimate) refuses without a local DuckDB."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(snapshot_app, ["create", "any_table", "--as", "x"])
    assert result.exit_code == 1
    out = result.output + (result.stderr or "")
    assert "Run: agnes pull" in out


def test_snapshot_create_estimate_skips_duckdb_guard(tmp_path, monkeypatch):
    """--estimate is server-side dry-run only; doesn't need local DuckDB.

    Analysts use it pre-bootstrap to scope a fetch before committing to
    materialize, so the local-DB guard would block the use case it's most
    useful for. Per Devin review finding ANALYSIS_0004.
    """
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    # Stub api_post so we don't actually hit the network — what we care about
    # is that the guard doesn't fire BEFORE the API call.
    from unittest.mock import MagicMock
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {"estimated_scan_bytes": 0, "estimated_rows": 0,
                                   "estimated_local_bytes": 0, "table_id": "any_table"}
    monkeypatch.setattr("cli.commands.snapshot.api_post", lambda *a, **kw: fake_resp,
                        raising=False)

    runner = CliRunner()
    result = runner.invoke(snapshot_app, ["create", "any_table", "--as", "x", "--estimate"])
    # Should NOT exit 1 with "Run: agnes pull" — that hint is for the fetch path.
    out = result.output + (result.stderr or "")
    assert "Run: agnes pull" not in out, \
        "--estimate must not be blocked by the local-DuckDB guard"
