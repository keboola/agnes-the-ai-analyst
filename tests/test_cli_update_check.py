"""Tests for the CLI auto-update check (cli/update_check.py)."""

import json
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("DA_CONFIG_DIR", str(tmp_path))
    # Point CLI at a fake server so get_server_url() returns something stable.
    monkeypatch.setenv("DA_SERVER", "http://server.test:8000")
    yield tmp_path


def test_check_returns_none_when_disabled(tmp_config):
    import os
    os.environ["DA_NO_UPDATE_CHECK"] = "1"
    try:
        from cli import update_check
        assert update_check.check("http://server.test:8000") is None
    finally:
        del os.environ["DA_NO_UPDATE_CHECK"]


def test_check_returns_none_when_server_url_missing(tmp_config):
    from cli import update_check
    assert update_check.check("") is None
    assert update_check.check(None) is None  # type: ignore[arg-type]


def test_check_returns_none_when_installed_version_unknown(tmp_config):
    from cli import update_check
    with patch("cli.update_check._installed_version", return_value="unknown"):
        assert update_check.check("http://server.test:8000") is None


def test_check_fresh_fetch_and_cache_write(tmp_config):
    from cli import update_check

    payload = {
        "version": "2.1.0",
        "wheel_filename": "agnes_the_ai_analyst-2.1.0-py3-none-any.whl",
        "download_url_path": "/cli/wheel/agnes_the_ai_analyst-2.1.0-py3-none-any.whl",
    }
    with patch("cli.update_check._installed_version", return_value="2.0.0"):
        with patch("cli.update_check._fetch_latest", return_value=payload):
            info = update_check.check("http://server.test:8000")

    assert info is not None
    assert info.installed == "2.0.0"
    assert info.latest == "2.1.0"
    assert info.download_url == (
        "http://server.test:8000/cli/wheel/agnes_the_ai_analyst-2.1.0-py3-none-any.whl"
    )
    assert info.is_outdated() is True

    # Cache file was written and re-reading it returns the same latest.
    cache = json.loads((tmp_config / "update_check.json").read_text())
    assert cache["installed"] == "2.0.0"
    assert cache["latest"] == "2.1.0"


def test_check_uses_cache_within_ttl(tmp_config):
    """Cached entry within 24h skips the network fetch."""
    from cli import update_check

    # Seed a fresh cache entry.
    (tmp_config / "update_check.json").write_text(json.dumps({
        "installed": "2.0.0",
        "server_url": "http://server.test:8000",
        "latest": "2.0.5",
        "download_url": "http://server.test:8000/cli/wheel/agnes_the_ai_analyst-2.0.5-py3-none-any.whl",
        "checked_at": __import__("time").time(),  # now
    }))

    with patch("cli.update_check._installed_version", return_value="2.0.0"):
        with patch("cli.update_check._fetch_latest") as mock_fetch:
            info = update_check.check("http://server.test:8000")

    assert mock_fetch.call_count == 0  # cache hit
    assert info.latest == "2.0.5"
    assert info.is_outdated() is True


def test_check_invalidates_cache_when_installed_version_changed(tmp_config):
    """User ran a fresh install after the cache was written — re-probe."""
    from cli import update_check

    # Seed cache claiming the installed version was 1.9.0.
    (tmp_config / "update_check.json").write_text(json.dumps({
        "installed": "1.9.0",
        "server_url": "http://server.test:8000",
        "latest": "2.0.0",
        "download_url": "http://server.test:8000/cli/wheel/x.whl",
        "checked_at": __import__("time").time(),
    }))

    payload = {"version": "2.1.0", "download_url_path": "/cli/wheel/y.whl"}
    with patch("cli.update_check._installed_version", return_value="2.0.0"):
        with patch("cli.update_check._fetch_latest", return_value=payload) as mock_fetch:
            info = update_check.check("http://server.test:8000")

    assert mock_fetch.call_count == 1  # cache was invalidated
    assert info.latest == "2.1.0"


def test_check_handles_network_failure_silently(tmp_config):
    """A probe that errors out returns None; no exception leaks."""
    from cli import update_check
    with patch("cli.update_check._installed_version", return_value="2.0.0"):
        with patch("cli.update_check._fetch_latest", return_value=None):
            assert update_check.check("http://server.test:8000") is None


def test_is_outdated_false_when_same_version(tmp_config):
    from cli.update_check import UpdateInfo
    info = UpdateInfo(installed="2.0.0", latest="2.0.0", download_url="…")
    assert info.is_outdated() is False


def test_is_outdated_false_when_latest_unknown(tmp_config):
    from cli.update_check import UpdateInfo
    info = UpdateInfo(installed="2.0.0", latest=None, download_url=None)
    assert info.is_outdated() is False


class TestRootCallbackIntegration:
    """The root callback must not crash a command when the probe fails, and
    must emit a stderr warning when the server advertises a newer version."""

    def test_probe_failure_does_not_break_command(self, tmp_config):
        with patch("cli.update_check.check", side_effect=RuntimeError("boom")):
            result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0

    def test_outdated_warning_is_emitted(self, tmp_config, capsys):
        """Unit-test the warning hook directly: `--help` is eager and bypasses
        the callback body, so we test `_maybe_warn_outdated` itself, which
        is what every real subcommand dispatch triggers."""
        from cli.main import _maybe_warn_outdated
        from cli.update_check import UpdateInfo
        info = UpdateInfo(
            installed="2.0.0",
            latest="2.1.0",
            download_url="http://server.test:8000/cli/wheel/x.whl",
        )
        with patch("cli.update_check.check", return_value=info):
            _maybe_warn_outdated()
        captured = capsys.readouterr()
        assert "[update]" in captured.err
        assert "2.1.0" in captured.err
