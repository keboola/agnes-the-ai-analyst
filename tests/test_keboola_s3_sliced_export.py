"""AWS-stack sliced exports: per-slice URLs arrive as raw ``s3://``.

The GCP and Azure branches of the sliced download already rewrite their
scheme-specific URIs; AWS was assumed to hand back presigned HTTPS. Stacks
that stage exports on S3 return `s3://bucket/key` in the manifest instead,
which `requests` cannot fetch at all ("No connection adapters were found"),
so every sliced export on such a stack failed. These cover the rewrite +
SigV4 signing, on both sliced entry points.
"""

from pathlib import Path

import pytest

from connectors.keboola.storage_api import KeboolaStorageClient, StorageApiError

SLICE_URL = "s3://example-export-bucket/exports/1/table-exports/in.c-demo.SALES/export.parquet_0_0_0.parquet"
KEY = "exports/1/table-exports/in.c-demo.SALES/export.parquet_0_0_0.parquet"

FILE_INFO = {
    "url": "https://signed.example.com/manifest.json",
    "isSliced": True,
    "provider": "aws",
    "region": "us-east-1",
    "s3Path": {"bucket": "example-export-bucket", "key": "exports/1/table-exports/in.c-demo.SALES/export.parquet"},
    "credentials": {
        "AccessKeyId": "AKIAEXAMPLE",
        "SecretAccessKey": "secret-example",
        "SessionToken": "session-token-example",
        "Expiration": "2026-08-19T00:00:00+0000",
    },
}


@pytest.fixture
def client(monkeypatch):
    c = KeboolaStorageClient(url="https://connection.example.com", token="tok")

    class _ManifestResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"entries": [{"url": SLICE_URL, "mandatory": True}]}

    monkeypatch.setattr(c.session, "get", lambda *a, **kw: _ManifestResponse())
    return c


def _capture_downloads(client, monkeypatch):
    """Record what `_download_single` is asked to fetch."""
    calls = []

    def fake_download(url, dest_path, *, gunzip_on_read, extra_headers=None):
        calls.append({"url": url, "headers": extra_headers or {}})
        Path(dest_path).write_bytes(b"parquet")

    monkeypatch.setattr(client, "_download_single", fake_download)
    return calls


def test_download_file_slices_rewrites_s3_uri_and_signs(client, monkeypatch, tmp_path):
    calls = _capture_downloads(client, monkeypatch)

    client.download_file_slices(FILE_INFO, tmp_path / "slices")

    assert len(calls) == 1
    url = calls[0]["url"]
    assert url.startswith("https://"), f"s3:// URI was not rewritten: {url}"
    assert "example-export-bucket" in url and "us-east-1" in url
    assert KEY in url
    headers = calls[0]["headers"]
    assert "Authorization" in headers, "slice request was not SigV4-signed"
    assert headers["Authorization"].startswith("AWS4-HMAC-SHA256")
    assert headers.get("X-Amz-Security-Token") == "session-token-example"
    # The signature must never travel in the query string — it would land in
    # proxy/access logs. Header-signed requests keep it out of the URL.
    assert "X-Amz-Signature" not in url


def test_download_file_csv_path_rewrites_s3_uri(client, monkeypatch, tmp_path):
    """`download_file` (CSV concat path) shares the gap and the fix."""
    calls = _capture_downloads(client, monkeypatch)

    client.download_file({**FILE_INFO, "name": "t.csv"}, tmp_path / "out.csv")

    assert calls and calls[0]["url"].startswith("https://")
    assert "Authorization" in calls[0]["headers"]


def test_slice_bucket_outside_the_export_is_refused(client, monkeypatch, tmp_path):
    """A manifest naming a foreign bucket must not receive the credentials."""
    _capture_downloads(client, monkeypatch)

    class _EvilManifest:
        def raise_for_status(self):
            return None

        def json(self):
            return {"entries": [{"url": "s3://attacker-bucket/steal", "mandatory": True}]}

    monkeypatch.setattr(client.session, "get", lambda *a, **kw: _EvilManifest())

    with pytest.raises(StorageApiError, match="bucket"):
        client.download_file_slices(FILE_INFO, tmp_path / "slices")


def test_missing_credentials_reports_what_is_missing(client, monkeypatch, tmp_path):
    _capture_downloads(client, monkeypatch)
    info = {k: v for k, v in FILE_INFO.items() if k != "credentials"}

    with pytest.raises(StorageApiError, match="credentials"):
        client.download_file_slices(info, tmp_path / "slices")
