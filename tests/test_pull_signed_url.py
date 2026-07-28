"""Tests for `agnes pull` signed-URL preference (WF-4, wave 2H).

`agnes pull` prefers a manifest entry's `signed_url` for downloading a
table's parquet directly from object storage, falling back to the
app-served `/api/data/{tid}/download` route on ANY failure — network
error, non-2xx (including a 403/404 on an expired/not-yet-mirrored
object), or md5 mismatch. md5 verification against the manifest `hash`
gates BOTH paths unconditionally: a signed-URL download that mismatches
must never be promoted, it must fall back and re-verify via the app
path.
"""

from __future__ import annotations

import hashlib
import socket
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from cli.lib.pull import run_pull


GOOD_BYTES = b"PAR1" + b"\x00" * 1000 + b"PAR1"
GOOD_HASH = hashlib.md5(GOOD_BYTES).hexdigest()
BAD_BYTES = b"PAR1" + b"\xff" * 1000 + b"PAR1"


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path, monkeypatch):
    """Sandbox `cli.config` reads/writes so a leftover on-disk
    sync_state.json from a prior run doesn't short-circuit the
    hash-comparison logic in run_pull."""
    cfg_dir = tmp_path / "_agnes_cfg"
    cfg_dir.mkdir()
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(cfg_dir))


def _manifest(signed_url: str | None = None) -> dict:
    entry = {"hash": GOOD_HASH, "rows": 1, "size_bytes": len(GOOD_BYTES)}
    if signed_url:
        entry["signed_url"] = signed_url
        entry["signed_url_expires_at"] = "2026-07-20T00:15:00Z"
    return {"tables": {"tbl1": entry}}


def _fake_api_get(manifest: dict):
    def _api_get(path, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if path == "/api/sync/manifest":
            resp.json.return_value = manifest
        elif path == "/api/memory/bundle":
            resp.json.return_value = {"mandatory": [], "approved": []}
        resp.raise_for_status = lambda: None
        return resp

    return _api_get


def _write_bytes_stream_download(body: bytes):
    def _stream_download(path, target_path, progress_callback=None):
        Path(target_path).write_bytes(body)
        return len(body)

    return _stream_download


def test_signed_url_preferred_app_not_called(tmp_path, monkeypatch):
    """A manifest entry carrying `signed_url` is fetched directly — the
    app-served `/api/data/{tid}/download` route is never hit."""
    manifest = _manifest(signed_url="https://bucket.example.com/tbl1.parquet?sig=abc")
    monkeypatch.setattr("cli.lib.pull.api_get", _fake_api_get(manifest), raising=False)

    def _fetch_signed_url(url, target_path, progress_callback=None):
        Path(target_path).write_bytes(GOOD_BYTES)

    monkeypatch.setattr("cli.lib.pull._fetch_signed_url", _fetch_signed_url, raising=False)
    stream_download_mock = MagicMock(side_effect=AssertionError("app path must not be used"))
    monkeypatch.setattr("cli.lib.pull.stream_download", stream_download_mock, raising=False)

    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

    assert result.tables_updated == 1
    assert result.errors == []
    stream_download_mock.assert_not_called()
    assert (tmp_path / "server" / "parquet" / "tbl1.parquet").read_bytes() == GOOD_BYTES
    assert getattr(result, "tables_via_signed_url", 0) == 1
    assert getattr(result, "tables_via_app", 0) == 0


def test_signed_url_fetch_error_falls_back_to_app(tmp_path, monkeypatch):
    """A raised exception from the signed-URL fetch (network error, or a
    non-2xx the helper raises on, e.g. an expired-403) falls back to the
    app path and still promotes a verified file."""
    manifest = _manifest(signed_url="https://bucket.example.com/tbl1.parquet?sig=abc")
    monkeypatch.setattr("cli.lib.pull.api_get", _fake_api_get(manifest), raising=False)

    def _fetch_signed_url(url, target_path, progress_callback=None):
        raise ConnectionError("boom")

    monkeypatch.setattr("cli.lib.pull._fetch_signed_url", _fetch_signed_url, raising=False)
    monkeypatch.setattr("cli.lib.pull.stream_download", _write_bytes_stream_download(GOOD_BYTES), raising=False)

    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

    assert result.tables_updated == 1
    assert result.errors == []
    assert (tmp_path / "server" / "parquet" / "tbl1.parquet").read_bytes() == GOOD_BYTES
    assert getattr(result, "tables_via_signed_url", 0) == 0
    assert getattr(result, "tables_via_app", 0) == 1


def test_signed_url_md5_mismatch_falls_back_to_app(tmp_path, monkeypatch):
    """A signed-URL download that completes but md5-mismatches must NOT be
    promoted from the signed URL — it falls back to the app path, whose
    bytes are verified and promoted instead."""
    manifest = _manifest(signed_url="https://bucket.example.com/tbl1.parquet?sig=abc")
    monkeypatch.setattr("cli.lib.pull.api_get", _fake_api_get(manifest), raising=False)

    def _fetch_signed_url(url, target_path, progress_callback=None):
        Path(target_path).write_bytes(BAD_BYTES)

    monkeypatch.setattr("cli.lib.pull._fetch_signed_url", _fetch_signed_url, raising=False)
    monkeypatch.setattr("cli.lib.pull.stream_download", _write_bytes_stream_download(GOOD_BYTES), raising=False)

    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

    assert result.tables_updated == 1
    assert result.errors == []
    assert (tmp_path / "server" / "parquet" / "tbl1.parquet").read_bytes() == GOOD_BYTES
    assert getattr(result, "tables_via_signed_url", 0) == 0
    assert getattr(result, "tables_via_app", 0) == 1


def test_no_signed_url_behavior_unchanged(tmp_path, monkeypatch):
    """A manifest entry with no `signed_url` key behaves exactly as
    before — only the app path is ever consulted."""
    manifest = _manifest(signed_url=None)
    monkeypatch.setattr("cli.lib.pull.api_get", _fake_api_get(manifest), raising=False)

    fetch_signed_url_mock = MagicMock(side_effect=AssertionError("must not be called"))
    monkeypatch.setattr("cli.lib.pull._fetch_signed_url", fetch_signed_url_mock, raising=False)
    monkeypatch.setattr("cli.lib.pull.stream_download", _write_bytes_stream_download(GOOD_BYTES), raising=False)

    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

    assert result.tables_updated == 1
    fetch_signed_url_mock.assert_not_called()
    assert getattr(result, "tables_via_app", 0) == 1


def test_both_paths_mismatch_reports_failure_not_silent_promote(tmp_path, monkeypatch):
    """md5 verify gates both paths: if the signed URL AND the app path both
    mismatch (after retries), the table is recorded as a hard failure —
    never silently promoted — and no file lands on disk."""
    manifest = _manifest(signed_url="https://bucket.example.com/tbl1.parquet?sig=abc")
    monkeypatch.setattr("cli.lib.pull.api_get", _fake_api_get(manifest), raising=False)
    monkeypatch.setattr("cli.lib.pull._DOWNLOAD_RETRY_BACKOFFS_S", (0.0, 0.0), raising=False)

    def _fetch_signed_url(url, target_path, progress_callback=None):
        Path(target_path).write_bytes(BAD_BYTES)

    monkeypatch.setattr("cli.lib.pull._fetch_signed_url", _fetch_signed_url, raising=False)
    monkeypatch.setattr("cli.lib.pull.stream_download", _write_bytes_stream_download(BAD_BYTES), raising=False)

    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

    assert result.tables_updated == 0
    assert not (tmp_path / "server" / "parquet" / "tbl1.parquet").exists()
    assert result.errors and result.errors[0]["table"] == "tbl1"
    assert "hash mismatch" in result.errors[0]["error"]


def test_signed_url_ssrf_guard_rejects_disallowed_scheme(tmp_path, monkeypatch):
    """A signed_url with a non-http(s) scheme is refused before any fetch
    is attempted — falls back to the app path unguarded-fetch-free."""
    manifest = _manifest(signed_url="ftp://bucket.example.com/tbl1.parquet")
    monkeypatch.setattr("cli.lib.pull.api_get", _fake_api_get(manifest), raising=False)
    monkeypatch.setattr("cli.lib.pull.stream_download", _write_bytes_stream_download(GOOD_BYTES), raising=False)
    # `_fetch_signed_url` is intentionally NOT mocked here — this exercises
    # the real SSRF/scheme guard inside it.

    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

    assert result.tables_updated == 1
    assert getattr(result, "tables_via_signed_url", 0) == 0
    assert getattr(result, "tables_via_app", 0) == 1


def test_signed_url_ssrf_guard_rejects_private_ip(tmp_path, monkeypatch):
    """A signed_url resolving to a private/link-local/metadata IP (e.g. the
    cloud metadata endpoint) is refused before any fetch is attempted —
    falls back to the app path. Real guard, not mocked."""
    manifest = _manifest(signed_url="http://169.254.169.254/latest/meta-data/tbl1.parquet")
    monkeypatch.setattr("cli.lib.pull.api_get", _fake_api_get(manifest), raising=False)
    monkeypatch.setattr("cli.lib.pull.stream_download", _write_bytes_stream_download(GOOD_BYTES), raising=False)

    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

    assert result.tables_updated == 1
    assert getattr(result, "tables_via_signed_url", 0) == 0
    assert getattr(result, "tables_via_app", 0) == 1


def test_signed_url_dns_rebind_mid_fetch_falls_back(tmp_path, monkeypatch):
    """DNS-rebinding attack: the signed_url hostname resolves to a PUBLIC IP
    on the pre-flight `_resolve_safe` check but flips to a PRIVATE/metadata
    IP on the very next lookup (the guarded transport's own re-validation,
    which is also what would pin the connection). Proves the fetch is
    refused before any connection is pinned to the private IP — falls back
    to the app path rather than connecting to 169.254.169.254 — closing the
    hole where a prior version of `_fetch_signed_url` discarded the pinned
    IP and let a plain `httpx.Client` re-resolve (and potentially connect
    to the rebound private address) at connect time.
    """
    manifest = _manifest(signed_url="https://attacker.example/tbl1.parquet")
    monkeypatch.setattr("cli.lib.pull.api_get", _fake_api_get(manifest), raising=False)
    monkeypatch.setattr("cli.lib.pull.stream_download", _write_bytes_stream_download(GOOD_BYTES), raising=False)

    calls = {"n": 0}

    def fake_getaddrinfo(host, *args, **kwargs):
        calls["n"] += 1
        ip = "8.8.8.8" if calls["n"] == 1 else "169.254.169.254"
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0))]

    monkeypatch.setattr("src.marketplace_asset_mirror.socket.getaddrinfo", fake_getaddrinfo)

    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

    # Both the pre-flight check AND the guarded transport's own
    # re-validation ran a lookup — the rebind (2nd lookup returning a
    # private IP) was caught before any connection was made.
    assert calls["n"] >= 2
    assert getattr(result, "tables_via_signed_url", 0) == 0
    assert getattr(result, "tables_via_app", 0) == 1
    assert (tmp_path / "server" / "parquet" / "tbl1.parquet").read_bytes() == GOOD_BYTES


def test_fetch_signed_url_pins_connection_to_resolved_ip_not_rehostname(tmp_path, monkeypatch):
    """Unit-level (mirrors
    ``tests/test_marketplace_asset_mirror.py::test_dns_rebinding_does_not_bypass_ssrf``):
    calling `_fetch_signed_url` directly, the actual wire request must
    target the pinned IP `_resolve_safe` resolved — never the bare
    hostname (which httpcore could independently re-resolve at connect
    time, reopening the DNS-rebinding hole). Exercised at the function
    level (not through `run_pull`) so it isn't entangled with the other
    HTTP calls a full pull makes."""
    from cli.lib.pull import _fetch_signed_url

    monkeypatch.setattr(
        "src.marketplace_asset_mirror.socket.getaddrinfo",
        lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 0))],
    )

    seen_hosts = []

    def fake_super_handle_request(self, request):
        seen_hosts.append(request.url.host)
        return httpx.Response(200, content=GOOD_BYTES)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fake_super_handle_request)

    target = tmp_path / "sidecar.parquet"
    _fetch_signed_url("https://attacker.example/tbl1.parquet", str(target))

    assert seen_hosts == ["8.8.8.8"]
    assert target.read_bytes() == GOOD_BYTES
