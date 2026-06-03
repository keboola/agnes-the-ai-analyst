"""Integration tests for the CLI loopback listener (cli/lib/loopback.py).

These exercise the real ephemeral-port HTTP server by faking
``webbrowser.open`` to fire the callback the way a browser redirect would.
"""

import threading
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from cli.lib import loopback


def _make_fake_open(*, code="abc123", state_override=None, delay=0.1):
    """Return a fake webbrowser.open that fires the loopback callback."""
    def fake_open(url):
        q = parse_qs(urlparse(url).query)
        port = int(q["port"][0])
        state = state_override if state_override is not None else q["state"][0]

        def hit():
            params = {"state": state}
            if code is not None:
                params["code"] = code
            try:
                httpx.get(f"http://127.0.0.1:{port}/callback", params=params, timeout=5)
            except Exception:
                pass

        threading.Timer(delay, hit).start()
        return True

    return fake_open


def test_captures_code_from_callback(monkeypatch):
    monkeypatch.setattr(loopback.webbrowser, "open", _make_fake_open(code="abc123"))
    code = loopback.capture_code_via_browser("http://server.test", timeout=5)
    assert code == "abc123"


def test_state_mismatch_raises(monkeypatch):
    monkeypatch.setattr(
        loopback.webbrowser, "open",
        _make_fake_open(code="abc123", state_override="WRONG-STATE"),
    )
    with pytest.raises(RuntimeError, match="state mismatch"):
        loopback.capture_code_via_browser("http://server.test", timeout=5)


def test_missing_code_raises(monkeypatch):
    monkeypatch.setattr(loopback.webbrowser, "open", _make_fake_open(code=None))
    with pytest.raises(RuntimeError):
        loopback.capture_code_via_browser("http://server.test", timeout=5)


def test_timeout_raises(monkeypatch):
    # Browser "opens" but never calls back.
    monkeypatch.setattr(loopback.webbrowser, "open", lambda url: True)
    with pytest.raises(TimeoutError):
        loopback.capture_code_via_browser("http://server.test", timeout=0.5)
