"""Keboola legacy client (`connectors/keboola/client.py`) s3:// sliced-export
URL rewrite — the AWS counterpart of test_keboola_client_gcs_slice.py.

AWS-backed Keboola stacks return raw ``s3://`` URIs in sliced-export
manifests; the legacy client (still the download path for incremental.py and
partitioned.py) previously passed them straight to `requests.get`, which
dies with `InvalidSchema: No connection adapters were found for 's3://…'`.
The fix presigns each slice with the temporary federation credentials from
the file-detail response, via storage_api's shared `_s3_to_https`.
"""

import gzip
from unittest.mock import MagicMock
from urllib.parse import parse_qsl, urlsplit

import pytest

# Optional kbcstorage dep — skip cleanly on installs that don't ship it.
# See tests/test_keboola_extractor_typed.py for the same pattern.
pytest.importorskip("kbcstorage")

from connectors.keboola.client import KeboolaClient  # noqa: E402

_AWS_FILE_DETAIL = {
    "url": "https://signed/manifest.json",
    "isSliced": True,
    "provider": "aws",
    "region": "us-east-1",
    "credentials": {
        "AccessKeyId": "ASIAEXAMPLEKEY",
        "SecretAccessKey": "example-secret",
        "SessionToken": "example-session-token",
    },
}


def _stub_client(tmp_path, monkeypatch):
    monkeypatch.setattr(KeboolaClient, "__init__", lambda self, **kw: None)
    client = KeboolaClient()
    client.token = "storage-tok"
    client.url = "https://connection.keboola.com"
    client.client = MagicMock()
    client.client.tables.detail.return_value = {"columns": ["id", "name"]}
    client.metadata_cache = {}
    client.metadata_cache_path = tmp_path / "meta.json"
    monkeypatch.setattr("connectors.keboola.client.time.sleep", lambda *a, **kw: None)
    return client


def _wire_http(monkeypatch, get_side_effects):
    """Mock the export POST + the GET sequence (job poll, file detail, …)."""
    export_post_resp = MagicMock()
    export_post_resp.raise_for_status = MagicMock()
    export_post_resp.json.return_value = {"id": 100}
    monkeypatch.setattr(
        "connectors.keboola.client.requests.post",
        MagicMock(return_value=export_post_resp),
    )

    job_poll_resp = MagicMock()
    job_poll_resp.raise_for_status = MagicMock()
    job_poll_resp.json.return_value = {
        "id": 100,
        "status": "success",
        "results": {"file": {"id": 200}},
    }
    get_mock = MagicMock(side_effect=[job_poll_resp, *get_side_effects])
    monkeypatch.setattr("connectors.keboola.client.requests.get", get_mock)
    return get_mock


def _json_resp(payload):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json.return_value = payload
    return r


def test_sliced_s3_slice_url_presigned_with_federation_credentials(tmp_path, monkeypatch):
    client = _stub_client(tmp_path, monkeypatch)

    manifest_resp = _json_resp({"entries": [{"url": "s3://bkt/exp/slice-0"}]})
    slice_resp = MagicMock()
    slice_resp.raise_for_status = MagicMock()
    slice_resp.content = b"1,alice\n"

    get_mock = _wire_http(
        monkeypatch,
        [_json_resp(dict(_AWS_FILE_DETAIL)), manifest_resp, slice_resp],
    )

    dest = tmp_path / "out.csv"
    client._export_table_with_filters("in.c-x.t", dest, where_filters=[])

    # Last GET call is the slice download — the raw s3:// URI must arrive as
    # a SigV4 query-presigned HTTPS URL (plain requests can download it, no
    # auth header needed).
    slice_call = get_mock.call_args_list[-1]
    parts = urlsplit(slice_call.args[0])
    assert parts.scheme == "https"
    assert parts.netloc == "bkt.s3.us-east-1.amazonaws.com"
    assert parts.path == "/exp/slice-0"
    q = dict(parse_qsl(parts.query))
    assert q["X-Amz-Security-Token"] == "example-session-token"
    assert "X-Amz-Signature" in q
    assert slice_call.kwargs["headers"] == {}

    # Header line synthesized from table metadata (sliced files carry no
    # header per Storage API contract) followed by the slice content.
    assert dest.read_text() == '"id","name"\n1,alice\n'


def test_gzipped_s3_slice_is_gunzipped_despite_presigned_query_string(tmp_path, monkeypatch):
    """The gzip check must look at the slice's PATH, not the full rewritten
    URL — presigning appends `?X-Amz-…`, so an `endswith('.gz')` on the
    rewritten URL silently stops matching and raw gzip bytes land in the
    output file."""
    client = _stub_client(tmp_path, monkeypatch)

    manifest_resp = _json_resp({"entries": [{"url": "s3://bkt/exp/slice-0.csv.gz"}]})
    slice_resp = MagicMock()
    slice_resp.raise_for_status = MagicMock()
    slice_resp.content = gzip.compress(b"1,alice\n")

    _wire_http(
        monkeypatch,
        [_json_resp(dict(_AWS_FILE_DETAIL)), manifest_resp, slice_resp],
    )

    dest = tmp_path / "out.csv"
    client._export_table_with_filters("in.c-x.t", dest, where_filters=[])

    assert dest.read_text() == '"id","name"\n1,alice\n'


def test_single_file_s3_url_is_presigned(tmp_path, monkeypatch):
    """Non-sliced export whose file-detail `url` arrives as a raw s3:// URI —
    the single-file branch must presign it too, not just the sliced loop."""
    client = _stub_client(tmp_path, monkeypatch)

    detail = dict(_AWS_FILE_DETAIL)
    detail["isSliced"] = False
    detail["url"] = "s3://bkt/exp/single.csv"

    download_resp = MagicMock()
    download_resp.raise_for_status = MagicMock()
    download_resp.headers = {}
    download_resp.iter_content.return_value = [b"id,name\n", b"1,alice\n"]

    get_mock = _wire_http(monkeypatch, [_json_resp(detail), download_resp])

    dest = tmp_path / "out.csv"
    client._export_table_with_filters("in.c-x.t", dest, where_filters=[])

    assert dest.read_text() == "id,name\n1,alice\n"
    parts = urlsplit(get_mock.call_args_list[-1].args[0])
    assert parts.scheme == "https"
    assert parts.netloc == "bkt.s3.us-east-1.amazonaws.com"
    assert parts.path == "/exp/single.csv"
    q = dict(parse_qsl(parts.query))
    assert q["X-Amz-Security-Token"] == "example-session-token"
    assert "X-Amz-Signature" in q
