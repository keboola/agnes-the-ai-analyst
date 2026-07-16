"""MetastoreClient — GET-only Keboola semantic-layer (Metastore) API client.

Tests mock requests.Session directly (same pattern as
tests/test_keboola_storage_api.py) so we exercise real HTTP shapes without
touching the network.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
import requests

from connectors.keboola.metastore_client import (
    MetastoreApiError,
    MetastoreClient,
    derive_metastore_url,
)


def _mock_response(status, body):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.json.return_value = body
    resp.text = json.dumps(body)
    return resp


class TestDeriveMetastoreUrl:
    def test_replaces_connection_with_metastore(self):
        assert (
            derive_metastore_url("https://connection.us-east4.gcp.keboola.com")
            == "https://metastore.us-east4.gcp.keboola.com"
        )

    def test_strips_trailing_slash(self):
        assert derive_metastore_url("https://connection.keboola.com/") == "https://metastore.keboola.com"


class TestMetastoreClientInit:
    def test_rejects_missing_url_or_token(self):
        with pytest.raises(ValueError):
            MetastoreClient(url="", token="t")
        with pytest.raises(ValueError):
            MetastoreClient(url="https://connection.keboola.com", token="")

    def test_base_url_includes_api_v1(self):
        c = MetastoreClient(url="https://connection.keboola.com", token="t")
        assert c.base == "https://metastore.keboola.com/api/v1"


class TestListItems:
    def test_list_items_sends_token_header_and_returns_data(self):
        sess = MagicMock()
        sess.get.return_value = _mock_response(
            200,
            {
                "data": [
                    {"type": "semantic-model", "id": "m1", "attributes": {"name": "core"}},
                ]
            },
        )
        c = MetastoreClient(url="https://connection.keboola.com", token="tok", session=sess)

        items = c.list_items("semantic-model")

        assert items == [{"type": "semantic-model", "id": "m1", "attributes": {"name": "core"}}]
        url = sess.get.call_args.args[0]
        assert url == "https://metastore.keboola.com/api/v1/repository/semantic-model"
        headers = sess.get.call_args.kwargs["headers"]
        assert headers["X-StorageApi-Token"] == "tok"

    def test_list_items_filters_by_model_uuid(self):
        sess = MagicMock()
        sess.get.return_value = _mock_response(
            200,
            {
                "data": [
                    {"type": "semantic-metric", "id": "a", "attributes": {"modelUUID": "u1", "name": "a"}},
                    {"type": "semantic-metric", "id": "b", "attributes": {"modelUUID": "u2", "name": "b"}},
                ]
            },
        )
        c = MetastoreClient(url="https://connection.keboola.com", token="tok", session=sess)

        items = c.list_items("semantic-metric", model_uuid="u1")

        assert [i["id"] for i in items] == ["a"]

    def test_list_items_no_model_uuid_returns_all(self):
        sess = MagicMock()
        sess.get.return_value = _mock_response(
            200,
            {
                "data": [
                    {"type": "semantic-metric", "id": "a", "attributes": {"modelUUID": "u1"}},
                    {"type": "semantic-metric", "id": "b", "attributes": {"modelUUID": "u2"}},
                ]
            },
        )
        c = MetastoreClient(url="https://connection.keboola.com", token="tok", session=sess)

        items = c.list_items("semantic-metric")

        assert len(items) == 2

    def test_401_raises_metastore_api_error_with_status(self):
        sess = MagicMock()
        sess.get.return_value = _mock_response(
            401,
            {
                "error": 401,
                "code": "401",
                "exception": "Failed to create project scope",
                "status": "error",
                "context": {"path": "/api/v1/repository/semantic-model"},
            },
        )
        c = MetastoreClient(url="https://connection.keboola.com", token="tok", session=sess)

        with pytest.raises(MetastoreApiError) as exc_info:
            c.list_items("semantic-model")

        assert exc_info.value.status == 401
        assert "Failed to create project scope" in str(exc_info.value)

    def test_token_redacted_in_error_message(self):
        sess = MagicMock()
        sess.get.return_value = _mock_response(
            403,
            {"detail": "rejected token=secrettoken123"},
        )
        c = MetastoreClient(url="https://connection.keboola.com", token="secrettoken123", session=sess)

        with pytest.raises(MetastoreApiError) as exc_info:
            c.list_items("semantic-model")

        assert "secrettoken123" not in str(exc_info.value)
        assert "<redacted-storage-token>" in str(exc_info.value)
