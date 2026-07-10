"""Keboola legacy client (`connectors/keboola/client.py`) gs:// sliced-export
URL rewrite — the kbcstorage-SDK-based counterpart to storage_api.py's
`_gs_to_https`. Previously zero coverage: `_export_table_with_filters`'s
sliced branch does a simpler string-replace rewrite (not the JSON-API media
URL storage_api.py builds), and that divergence had never been exercised.
"""

from unittest.mock import MagicMock

import pytest

# Optional kbcstorage dep — skip cleanly on installs that don't ship it.
# See tests/test_keboola_extractor_typed.py for the same pattern.
pytest.importorskip("kbcstorage")

from connectors.keboola.client import KeboolaClient  # noqa: E402


def test_sliced_gcs_slice_url_rewritten_with_bearer_token(tmp_path, monkeypatch):
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
        "gcsCredentials": {"access_token": "gcs-bearer-tok"},
    }

    manifest_resp = MagicMock()
    manifest_resp.raise_for_status = MagicMock()
    manifest_resp.json.return_value = {
        "entries": [{"url": "gs://bkt/exp/slice-0"}],
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

    # Last GET call is the slice download — verify the gs:// URI got
    # rewritten to a plain https://storage.googleapis.com path (client.py's
    # simpler rewrite, distinct from storage_api.py's JSON-API media URL)
    # and that the OAuth bearer token was attached.
    slice_call = get_mock.call_args_list[-1]
    assert slice_call.args[0] == "https://storage.googleapis.com/bkt/exp/slice-0"
    assert slice_call.kwargs["headers"] == {"Authorization": "Bearer gcs-bearer-tok"}

    # Header line synthesized from table metadata (sliced files carry no
    # header per Storage API contract) followed by the slice content.
    assert dest.read_text() == '"id","name"\n1,alice\n'
