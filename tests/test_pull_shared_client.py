"""Tests for the persistent HTTP/2-capable shared client (Change 2).

`agnes pull` issues N stream_download calls — one per parquet. Without
pooling, each call performs a fresh TLS handshake. The shared client is
created lazily once per process and closed at exit; HTTP/2 (when `h2` is
available) further multiplexes all chunk Range requests over a single
TCP connection.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path, monkeypatch):
    cfg = tmp_path / "_cfg"
    cfg.mkdir()
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(cfg))
    # Some dev environments point SSL_CERT_FILE / REQUESTS_CA_BUNDLE at a
    # corp-CA bundle that may not exist on every laptop running the test
    # suite. Clear those so httpx.Client() construction in the shared-
    # client path can build a default SSL context without trying to load
    # a missing PEM file.
    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _reset_shared(monkeypatch):
    import cli.client as cc
    cc._close_shared_client()
    monkeypatch.setattr(cc, "_SHARED_CLIENT", None, raising=False)
    yield
    cc._close_shared_client()


def test_get_shared_client_is_cached(monkeypatch):
    """Multiple calls return the same client instance — no fresh TLS
    handshake per stream_download invocation."""
    monkeypatch.setenv("AGNES_SERVER", "https://x.example.test")
    from cli.client import _get_shared_client
    c1 = _get_shared_client()
    c2 = _get_shared_client()
    assert c1 is c2, "shared client must be a single instance"


def test_get_shared_client_falls_back_when_http2_unavailable(monkeypatch):
    """If httpx raises during HTTP/2 client construction (e.g. `h2` not
    installed in the runtime env), we must gracefully build a HTTP/1.1
    client instead of crashing the pull."""
    import httpx

    monkeypatch.setenv("AGNES_SERVER", "https://x.example.test")
    import cli.client as cc

    real_client = httpx.Client

    construction_calls = []

    def fake_client(*args, **kwargs):
        construction_calls.append(kwargs.copy())
        if kwargs.get("http2") is True:
            raise ImportError("Using http2=True, but the 'h2' package is not installed")
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", fake_client)

    client = cc._get_shared_client()
    assert client is not None
    # Two construction attempts: first http2=True (raised), second falls
    # back to HTTP/1.1 (no http2 kwarg).
    assert construction_calls[0].get("http2") is True
    assert construction_calls[1].get("http2") is None or construction_calls[1].get("http2") is False
    cc._close_shared_client()


def test_close_shared_client_idempotent(monkeypatch):
    """Calling close twice (once explicitly, once via atexit) must not
    raise."""
    monkeypatch.setenv("AGNES_SERVER", "https://x.example.test")
    from cli.client import _get_shared_client, _close_shared_client
    _get_shared_client()
    _close_shared_client()
    _close_shared_client()  # second close is a no-op
