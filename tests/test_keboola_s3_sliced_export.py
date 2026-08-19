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


def test_legacy_sdk_client_signs_s3_slices(monkeypatch, tmp_path):
    """The legacy client is the download path for incremental + partitioned
    syncs, and it had the same gap: it rewrote `gs://` only, then handed the
    raw `s3://` URI to `requests`. It must now go through the same signing
    helper as the Storage API client.
    """
    # `connectors/keboola/client.py` imports the Keboola SDK at module level,
    # and that lives in the [server] extra. Stub it rather than skipping, so
    # this test actually runs in a plain dev env instead of quietly passing.
    import sys
    import types

    if "kbcstorage" not in sys.modules:

        class _StubSdkClient:
            def __init__(self, *args, **kwargs):
                pass

        sdk = types.ModuleType("kbcstorage")
        sdk_client = types.ModuleType("kbcstorage.client")
        sdk_client.Client = _StubSdkClient
        sdk.client = sdk_client
        monkeypatch.setitem(sys.modules, "kbcstorage", sdk)
        monkeypatch.setitem(sys.modules, "kbcstorage.client", sdk_client)

    from connectors.keboola import client as legacy

    fetched: list[dict] = []

    class _Resp:
        def __init__(self, payload=None, content=b""):
            self._payload = payload
            self.content = content
            self.headers: dict[str, str] = {}

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def fake_get(url, headers=None, **kw):
        if "/jobs/" in url:
            return _Resp({"status": "success", "results": {"file": {"id": 42}}})
        if "/files/" in url:
            return _Resp(dict(FILE_INFO))
        if "manifest" in url:
            return _Resp({"entries": [{"url": SLICE_URL}]})
        fetched.append({"url": url, "headers": dict(headers or {})})
        return _Resp(content=b"a,b\n")

    monkeypatch.setattr(legacy.requests, "post", lambda *a, **kw: _Resp({"id": "job-1"}))
    monkeypatch.setattr(legacy.requests, "get", fake_get)
    monkeypatch.setattr(legacy.time, "sleep", lambda *_: None)

    c = legacy.KeboolaClient(token="tok", url="https://connection.example.com")
    monkeypatch.setattr(c, "get_table_metadata", lambda _tid: {"columns": ["a", "b"]})

    out = tmp_path / "out.csv"
    c._export_table_with_filters("in.c-demo.SALES", out, [])

    assert fetched, "no slice was fetched"
    slice_call = fetched[-1]
    assert not slice_call["url"].startswith("s3://"), "raw s3:// URI reached requests"
    assert slice_call["url"].startswith(f"https://example-export-bucket.s3.{FILE_INFO['region']}.amazonaws.com/")
    assert KEY in slice_call["url"]
    # SigV4 in headers, never presigned into the query string.
    assert "Authorization" in slice_call["headers"]
    assert "AWS4-HMAC-SHA256" in slice_call["headers"]["Authorization"]
    assert "X-Amz-Signature" not in slice_call["url"]
