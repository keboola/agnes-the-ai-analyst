"""Verify cli/client.py:get_client() hard-stops on min_version mismatch."""

from unittest.mock import patch

import httpx
import pytest


def _fake_response(headers: dict) -> httpx.Response:
    return httpx.Response(status_code=200, headers=headers, content=b"{}", request=httpx.Request("GET", "http://x/"))


def test_local_below_min_exits_with_code_2():
    from cli.client import _check_version_headers
    with patch("cli.client._installed_version", return_value="0.30.0"):
        resp = _fake_response({
            "X-Agnes-Latest-Version": "0.40.0",
            "X-Agnes-Min-Version": "0.35.0",
        })
        with pytest.raises(SystemExit) as exc:
            _check_version_headers(resp)
        assert exc.value.code == 2


def test_local_at_or_above_min_does_not_exit():
    from cli.client import _check_version_headers
    with patch("cli.client._installed_version", return_value="0.40.0"):
        resp = _fake_response({
            "X-Agnes-Latest-Version": "0.40.0",
            "X-Agnes-Min-Version": "0.35.0",
        })
        _check_version_headers(resp)  # must not raise


def test_local_equal_to_min_does_not_exit():
    """`Version("X.Y.Z") < Version("X.Y.Z")` is False — equality must pass."""
    from cli.client import _check_version_headers
    with patch("cli.client._installed_version", return_value="0.35.0"):
        resp = _fake_response({
            "X-Agnes-Latest-Version": "0.40.0",
            "X-Agnes-Min-Version": "0.35.0",
        })
        _check_version_headers(resp)  # must not raise


def test_missing_headers_no_enforcement():
    """Older server without middleware → no headers → no-op."""
    from cli.client import _check_version_headers
    with patch("cli.client._installed_version", return_value="0.10.0"):
        resp = _fake_response({})  # empty headers
        _check_version_headers(resp)  # must not raise


def test_unknown_local_version_no_enforcement():
    """Source-checkout / editable install → never block."""
    from cli.client import _check_version_headers
    with patch("cli.client._installed_version", return_value="unknown"):
        resp = _fake_response({
            "X-Agnes-Latest-Version": "0.40.0",
            "X-Agnes-Min-Version": "0.35.0",
        })
        _check_version_headers(resp)  # must not raise


def test_self_upgrade_in_progress_disables_enforcement(monkeypatch):
    """Recursion barrier: while self-upgrade runs, no /api/* call may
    block on min-version drift. Otherwise an in-flight upgrade could
    sys.exit(2) with 'Run: agnes self-upgrade' from inside itself."""
    from cli.client import _check_version_headers
    monkeypatch.setenv("AGNES_SELF_UPGRADE_IN_PROGRESS", "1")
    with patch("cli.client._installed_version", return_value="0.10.0"):
        resp = _fake_response({
            "X-Agnes-Latest-Version": "0.40.0",
            "X-Agnes-Min-Version": "0.35.0",
        })
        _check_version_headers(resp)  # must not raise
