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
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(snapshot_app, ["create", "any_table", "--as", "x", "--estimate"])
    assert result.exit_code == 1
    out = result.output + (result.stderr or "")
    assert "Run: agnes pull" in out
