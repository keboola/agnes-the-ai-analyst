"""Unit tests for the curated marketplace asset-mirror cache.

Covers:
* allowlist enforcement (Content-Type + URL extension fallback),
* SSRF guards (private IPs, non-http schemes),
* size cap,
* conditional GET (304 Not Modified vs 200 OK + new sha256),
* b1 fallback (preserve last good copy on fetch failure),
* manifest cleanup when an upstream URL disappears.

The HTTP layer is mocked at ``urllib.request.urlopen`` so we don't depend
on a network. Each test instantiates a small fake response object.
"""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from unittest.mock import patch

from src.marketplace_asset_mirror import (
    HTTP_TIMEOUT_SEC,
    MAX_BODY_BYTES,
    MirrorEntry,
    _is_safe_url,
    sync_assets,
)


PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"           # signature
    b"\x00\x00\x00\rIHDR"           # IHDR chunk header
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"  # 1x1
    b"\x1f\x15\xc4\x89"             # CRC
    b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
    b"\r\n-\xb4"                    # IDAT
    b"\x00\x00\x00\x00IEND\xaeB`\x82"  # IEND
)
PDF_BYTES = b"%PDF-1.4\n%minimal\n"


class _FakeResponse:
    """Minimal `urllib.request.urlopen` return value double."""

    def __init__(self, *, body: bytes = b"", content_type: str = "",
                 etag: str = "", last_modified: str = "", status: int = 200):
        self._body = body
        self.status = status
        self.headers = {
            "Content-Type": content_type,
            "ETag": etag,
            "Last-Modified": last_modified,
        }

    def read(self, n: int = -1) -> bytes:
        if n < 0:
            return self._body
        return self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _patch_urlopen(responses):
    """Return a context manager patching urlopen to consume from the list."""
    iterator = iter(responses)

    def fake_urlopen(req, timeout=HTTP_TIMEOUT_SEC):
        return next(iterator)

    return patch("src.marketplace_asset_mirror.urllib.request.urlopen", fake_urlopen)


def _patch_safe_url(safe: bool = True, reason: str = ""):
    """Bypass DNS-based SSRF detection so unit tests don't touch the network."""
    return patch(
        "src.marketplace_asset_mirror._is_safe_url",
        return_value=(safe, reason),
    )


# --- _is_safe_url --------------------------------------------------------


def test_is_safe_url_rejects_non_http():
    ok, reason = _is_safe_url("ftp://example.com/x")
    assert not ok and "unsupported_scheme" in reason


def test_is_safe_url_rejects_file_scheme():
    ok, reason = _is_safe_url("file:///etc/passwd")
    assert not ok


def test_is_safe_url_rejects_loopback():
    ok, reason = _is_safe_url("http://127.0.0.1/x")
    assert not ok and "blocked_range" in reason


def test_is_safe_url_rejects_link_local_metadata():
    ok, reason = _is_safe_url("http://169.254.169.254/latest/meta-data/")
    assert not ok and "blocked_range" in reason


def test_is_safe_url_rejects_missing_host():
    ok, reason = _is_safe_url("https:///")
    assert not ok and "missing_host" in reason


# --- sync_assets: allowlist enforcement ----------------------------------


def test_sync_assets_rejects_image_with_html_content_type(tmp_path):
    """Cover photo URLs that return text/html (a page, not an image) must be
    rejected — accept_image_response only allows image/png|jpeg|webp."""
    resps = [_FakeResponse(content_type="text/html", status=200, body=b"<html/>")]
    with _patch_safe_url(), _patch_urlopen(resps):
        report = sync_assets(
            cache_dir=tmp_path,
            requests=[("plugin1", "cover", "https://x.com/c.png")],
        )
    assert report.rejected == 1
    assert report.fetched == 0
    entry = report.entries["https://x.com/c.png"]
    assert entry.status == "rejected"


def test_sync_assets_rejects_doc_with_html_content_type(tmp_path):
    """text/html doc URLs (e.g. Confluence pages) are rejected — they don't
    survive the allowlist, which intentionally has no HTML entry."""
    resps = [_FakeResponse(content_type="text/html", status=200, body=b"<html/>")]
    with _patch_safe_url(), _patch_urlopen(resps):
        report = sync_assets(
            cache_dir=tmp_path,
            requests=[("plugin1", "doc", "https://x.com/page")],
        )
    assert report.rejected == 1


def test_sync_assets_accepts_pdf_via_octet_stream_with_pdf_extension(tmp_path):
    """CDNs often serve .pdf as application/octet-stream — extension fallback
    must allow that combination."""
    resps = [_FakeResponse(
        content_type="application/octet-stream",
        body=PDF_BYTES,
    )]
    with _patch_safe_url(), _patch_urlopen(resps):
        report = sync_assets(
            cache_dir=tmp_path,
            requests=[("p", "doc", "https://x.com/setup.pdf")],
        )
    assert report.fetched == 1
    entry = report.entries["https://x.com/setup.pdf"]
    assert entry.status == "ok"
    assert entry.local
    assert (tmp_path / entry.local).exists()


def test_sync_assets_rejects_image_via_extension_only(tmp_path):
    """Images cannot use the generic-content-type fallback — image dispatch
    must be explicit (image/png/jpeg/webp). Octet-stream alone is rejected."""
    resps = [_FakeResponse(
        content_type="application/octet-stream",
        body=PNG_BYTES,
    )]
    with _patch_safe_url(), _patch_urlopen(resps):
        report = sync_assets(
            cache_dir=tmp_path,
            requests=[("p", "cover", "https://x.com/c.png")],
        )
    assert report.rejected == 1


# --- sync_assets: size cap -----------------------------------------------


def test_sync_assets_rejects_oversized_body(tmp_path):
    """Body larger than MAX_BODY_BYTES is rejected at read() time."""
    huge = b"\xff" * (MAX_BODY_BYTES + 1024)
    resps = [_FakeResponse(content_type="image/png", body=huge)]
    with _patch_safe_url(), _patch_urlopen(resps):
        report = sync_assets(
            cache_dir=tmp_path,
            requests=[("p", "cover", "https://x.com/c.png")],
        )
    assert report.rejected == 1
    assert "body_exceeds_cap" in report.entries["https://x.com/c.png"].error


# --- sync_assets: conditional GET (304 / 200 sha256) ---------------------


def test_sync_assets_304_keeps_cached_file(tmp_path):
    """A second sync that gets 304 Not Modified must keep the prior file
    intact — this is the steady state on stable CDN content and the path
    we want to be cheap."""
    # First sync: download the body.
    resps = [_FakeResponse(content_type="image/png", body=PNG_BYTES,
                           etag='"abc"', last_modified="Wed, 01 Jan 2026 00:00:00 GMT")]
    with _patch_safe_url(), _patch_urlopen(resps):
        report = sync_assets(
            cache_dir=tmp_path,
            requests=[("p", "cover", "https://x.com/c.png")],
        )
    first_local = (tmp_path / report.entries["https://x.com/c.png"].local)
    assert first_local.exists()
    first_sha = report.entries["https://x.com/c.png"].sha256

    # Second sync: 304 response. The mocked _fetch_url should still receive
    # the conditional headers from the prior manifest entry; we don't assert
    # that here, just that the file survives untouched.
    not_modified = urllib.error.HTTPError(
        url="https://x.com/c.png", code=304, msg="Not Modified",
        hdrs={}, fp=io.BytesIO(b""),
    )

    def fake_urlopen(req, timeout=HTTP_TIMEOUT_SEC):
        raise not_modified

    with _patch_safe_url(), patch(
        "src.marketplace_asset_mirror.urllib.request.urlopen", fake_urlopen
    ):
        report2 = sync_assets(
            cache_dir=tmp_path,
            requests=[("p", "cover", "https://x.com/c.png")],
        )
    assert report2.not_modified == 1
    assert report2.fetched == 0
    # File still there + same hash (we never re-wrote it).
    assert first_local.exists()
    assert report2.entries["https://x.com/c.png"].sha256 == first_sha


# --- sync_assets: failure preserves last good copy -----------------------


def test_sync_assets_fetch_failure_keeps_prior_file(tmp_path):
    """b1 fallback: when a URL we previously mirrored fails on a later sync,
    the last good copy stays in place and the manifest records the error."""
    # Seed a successful first sync.
    resps = [_FakeResponse(content_type="image/png", body=PNG_BYTES)]
    with _patch_safe_url(), _patch_urlopen(resps):
        sync_assets(
            cache_dir=tmp_path,
            requests=[("p", "cover", "https://x.com/c.png")],
        )

    # Second sync: server returns 500.
    server_error = urllib.error.HTTPError(
        url="https://x.com/c.png", code=500, msg="Internal Server Error",
        hdrs={}, fp=io.BytesIO(b""),
    )

    def fake_urlopen(req, timeout=HTTP_TIMEOUT_SEC):
        raise server_error

    with _patch_safe_url(), patch(
        "src.marketplace_asset_mirror.urllib.request.urlopen", fake_urlopen
    ):
        report = sync_assets(
            cache_dir=tmp_path,
            requests=[("p", "cover", "https://x.com/c.png")],
        )
    assert report.failed == 1
    entry = report.entries["https://x.com/c.png"]
    assert entry.status == "failed_recent"
    assert entry.local
    assert (tmp_path / entry.local).exists(), "last good copy must survive"


# --- sync_assets: cleanup of removed URLs --------------------------------


def test_sync_assets_drops_removed_url(tmp_path):
    """When a URL disappears from `requests` between syncs, its manifest
    entry and local file are removed."""
    resps = [_FakeResponse(content_type="application/pdf", body=PDF_BYTES)]
    with _patch_safe_url(), _patch_urlopen(resps):
        report1 = sync_assets(
            cache_dir=tmp_path,
            requests=[("p", "doc", "https://x.com/d.pdf")],
        )
    local_path = tmp_path / report1.entries["https://x.com/d.pdf"].local
    assert local_path.exists()

    # Second sync — empty request list (curator removed the doc_link).
    with _patch_safe_url(), _patch_urlopen([]):
        report2 = sync_assets(cache_dir=tmp_path, requests=[])
    assert report2.removed == 1
    assert "https://x.com/d.pdf" not in report2.entries
    assert not local_path.exists()


# --- sync_assets: SSRF block at sync-time --------------------------------


def test_sync_assets_blocks_unsafe_url_without_calling_urlopen(tmp_path):
    """SSRF check fires before the HTTP fetch — urlopen is never invoked."""
    called = {"hit": False}

    def fake_urlopen(req, timeout=HTTP_TIMEOUT_SEC):
        called["hit"] = True
        raise AssertionError("urlopen must not be invoked for unsafe URLs")

    with _patch_safe_url(False, "address_in_blocked_range: 127.0.0.1"), patch(
        "src.marketplace_asset_mirror.urllib.request.urlopen", fake_urlopen
    ):
        report = sync_assets(
            cache_dir=tmp_path,
            requests=[("p", "cover", "http://internal/x.png")],
        )
    assert called["hit"] is False
    assert report.rejected == 1


# --- manifest persistence -------------------------------------------------


def test_sync_assets_writes_manifest_json(tmp_path):
    resps = [_FakeResponse(content_type="image/png", body=PNG_BYTES)]
    with _patch_safe_url(), _patch_urlopen(resps):
        sync_assets(
            cache_dir=tmp_path,
            requests=[("p", "cover", "https://x.com/c.png")],
        )
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert "entries" in manifest
    assert "https://x.com/c.png" in manifest["entries"]
    entry = manifest["entries"]["https://x.com/c.png"]
    assert entry["status"] == "ok"
    assert entry["plugin_name"] == "p"
    assert entry["kind"] == "cover"
