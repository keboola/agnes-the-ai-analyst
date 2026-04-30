"""Verify `da sync --quiet` truly suppresses stdout chatter, including the
download loop and final summary. Without --quiet, the same fixture prints
'Downloading', 'Downloaded:', etc.; with --quiet, stdout stays clean and
the terse one-liner lands on stderr."""
from typer.testing import CliRunner
from unittest.mock import patch, MagicMock

from cli.main import app


def _fake_manifest_one_table():
    resp = MagicMock()
    resp.json.return_value = {
        "tables": {"orders": {"hash": "abc123", "rows": 5, "size_bytes": 100}},
        "assets": {},
        "server_time": "2026-04-30T00:00:00Z",
    }
    resp.raise_for_status = MagicMock()
    return resp


def _stub_download(_url, target_path):
    # Write a parquet-magic-bytes file so _is_valid_parquet's structural
    # check passes (we don't have a hash for the empty case, but here we
    # ship a hash in the manifest, so the test path goes through _md5_file).
    from pathlib import Path
    p = Path(target_path)
    # Real-ish parquet stub: PAR1 header + minimal body + PAR1 footer
    p.write_bytes(b"PAR1" + b"\x00" * 16 + b"PAR1")


def test_quiet_suppresses_stdout_when_downloading(tmp_path, monkeypatch):
    """The interesting case: manifest has tables that actually trigger downloads.
    Without --quiet, stdout would contain 'Downloading' / 'Downloaded:'.
    With --quiet, stdout stays empty and the terse summary lands on stderr.
    """
    monkeypatch.setenv("DA_LOCAL_DIR", str(tmp_path))
    monkeypatch.setenv("DA_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    runner = CliRunner()

    with patch("cli.commands.sync.api_get", return_value=_fake_manifest_one_table()), \
         patch("cli.commands.sync.stream_download", side_effect=_stub_download), \
         patch("cli.commands.sync._md5_file", return_value="abc123"), \
         patch("cli.commands.sync._rebuild_duckdb_views"):
        result = runner.invoke(app, ["sync", "--quiet"])

    assert result.exit_code == 0
    # stdout MUST be empty in quiet mode (no progress, no summary)
    assert result.stdout == "", f"expected empty stdout, got: {result.stdout!r}"
    # The terse one-line summary lands on stderr
    assert "sync: 1 tables" in result.stderr


def test_noisy_mode_prints_to_stdout(tmp_path, monkeypatch):
    """Anchor test: confirm the noisy path DOES print download chatter to stdout,
    so the contrast in the quiet test above is meaningful."""
    monkeypatch.setenv("DA_LOCAL_DIR", str(tmp_path))
    monkeypatch.setenv("DA_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    runner = CliRunner()

    with patch("cli.commands.sync.api_get", return_value=_fake_manifest_one_table()), \
         patch("cli.commands.sync.stream_download", side_effect=_stub_download), \
         patch("cli.commands.sync._md5_file", return_value="abc123"), \
         patch("cli.commands.sync._rebuild_duckdb_views"):
        result = runner.invoke(app, ["sync"])  # no --quiet

    assert result.exit_code == 0
    # Noisy mode prints the multi-line summary to stdout
    assert "Downloaded:" in result.stdout


def test_quiet_manifest_failure_exits_nonzero(tmp_path, monkeypatch):
    """Hook contract: if the server is unreachable, exit code is non-zero
    so the SessionStart hook's `|| true` fallback can swallow it cleanly,
    and the error message lands on stderr."""
    monkeypatch.setenv("DA_LOCAL_DIR", str(tmp_path))
    monkeypatch.setenv("DA_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    runner = CliRunner()

    fake_resp = MagicMock()
    fake_resp.raise_for_status.side_effect = RuntimeError("boom")

    with patch("cli.commands.sync.api_get", return_value=fake_resp):
        result = runner.invoke(app, ["sync", "--quiet"])

    assert result.exit_code == 1
    assert "manifest fetch failed" in result.stderr
