"""Keboola legacy client (`connectors/keboola/client.py`) s3:// sliced-export
URL rewrite — the AWS counterpart of test_keboola_client_gcs_slice.py.

AWS-backed Keboola stacks return raw ``s3://`` URIs in sliced-export
manifests; the legacy client (still the download path for incremental.py and
partitioned.py) previously passed them straight to `requests.get`, which
dies with `InvalidSchema: No connection adapters were found for 's3://…'`.
The fix presigns each slice with the temporary federation credentials from
the file-detail response, via storage_api's shared `_s3_to_https`.
"""

from unittest.mock import MagicMock
from urllib.parse import parse_qsl, urlsplit

import pytest

# Optional kbcstorage dep — skip cleanly on installs that don't ship it.
# See tests/test_keboola_extractor_typed.py for the same pattern.
pytest.importorskip("kbcstorage")

from connectors.keboola.client import KeboolaClient  # noqa: E402


def test_sliced_s3_slice_url_presigned_with_federation_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(KeboolaClient, "__init__", lambda self, **kw: None)
    client = KeboolaClient()
    client.token = "storage-tok"
    client.url = "https://connection.keboola.com"
    client.client = MagicMock()
    client.client.tables.detail.return_value = {"columns": ["id", "name"]}
    client.metadata_cache = {}
    client.metadata_cache_path = tmp_path / "meta.json"

    monkeypatch.setattr("connectors.keboola.client.time.sleep", lambda *a, **kw: None)

    export_post_resp = MagicMock()
    export_post_resp.raise_for_status = MagicMock()
    export_post_resp.json.return_value = {"id": 100}

    job_poll_resp = MagicMock()
    job_poll_resp.raise_for_status = MagicMock()
    job_poll_resp.json.return_value = {
        "id": 100,
        "status": "success",
        "results": {"file": {"id": 200}},
    }

    file_detail_resp = MagicMock()
    file_detail_resp.raise_for_status = MagicMock()
    file_detail_resp.json.return_value = {
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

    manifest_resp = MagicMock()
    manifest_resp.raise_for_status = MagicMock()
    manifest_resp.json.return_value = {
        "entries": [{"url": "s3://bkt/exp/slice-0"}],
    }

    slice_resp = MagicMock()
    slice_resp.raise_for_status = MagicMock()
    slice_resp.content = b"1,alice\n"

    monkeypatch.setattr(
        "connectors.keboola.client.requests.post",
        MagicMock(return_value=export_post_resp),
    )
    get_mock = MagicMock(side_effect=[job_poll_resp, file_detail_resp, manifest_resp, slice_resp])
    monkeypatch.setattr("connectors.keboola.client.requests.get", get_mock)

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
