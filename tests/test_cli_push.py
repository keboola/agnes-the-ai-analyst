"""Tests for agnes push command (SessionEnd uploader)."""

from typer.testing import CliRunner

# CI-safety: Typer/rich emits ANSI escapes in --help output. Strip before asserts.
_ANSI_RE = __import__("re").compile(r"\x1b\[[0-9;]*m")
def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)

from cli.commands.push import push_app

runner = CliRunner()


def test_push_help():
    result = runner.invoke(push_app, ["--help"])
    assert result.exit_code == 0
    assert "--quiet" in _clean(result.output)
    assert "--json" in _clean(result.output)
    assert "--dry-run" in _clean(result.output)


def test_push_no_sessions_no_mkdir(tmp_path, monkeypatch):
    """Empty workspace -> push exits 0, doesn't create user/sessions/."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    monkeypatch.setattr("cli.commands.push.get_server_url", lambda: "http://x")
    monkeypatch.setattr("cli.commands.push.get_token", lambda: "test-pat")
    result = runner.invoke(push_app, ["--quiet"])
    assert result.exit_code == 0
    assert not (tmp_path / "user" / "sessions").exists(), \
        "lazy mkdir: nothing to upload must not create user/sessions/"


def test_push_dry_run_no_writes(tmp_path, monkeypatch):
    """--dry-run lists what would upload but sends nothing."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    monkeypatch.setattr("cli.commands.push.get_server_url", lambda: "http://x")
    monkeypatch.setattr("cli.commands.push.get_token", lambda: "test-pat")
    # Create a session file
    sessions_dir = tmp_path / "user" / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "abc.jsonl").write_text('{"event":"test"}\n')

    # No api_post should be called - patch it to fail loudly if invoked
    def _raise(*a, **kw):
        raise AssertionError("api_post was called during --dry-run")
    monkeypatch.setattr("cli.commands.push.api_post", _raise)

    result = runner.invoke(push_app, ["--dry-run"])
    assert result.exit_code == 0
