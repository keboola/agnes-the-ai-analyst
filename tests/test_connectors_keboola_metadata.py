"""Keboola metadata provider — happy + unconfigured + api-error paths."""

from unittest.mock import MagicMock, patch

import pytest

from app.api._metadata_models import MetadataRequest, TableMetadata


@pytest.fixture
def req():
    return MetadataRequest(
        table_id="orders", bucket="in.c-crm", source_table="orders",
    )


def test_happy_path_returns_populated_metadata(req, monkeypatch):
    from connectors.keboola import metadata
    # KeboolaClient(token=None, url=None) reads env vars; pretend they're set.
    monkeypatch.setenv("KEBOOLA_STACK_URL", "https://connection.keboola.com")
    monkeypatch.setenv("KEBOOLA_STORAGE_TOKEN", "tok")

    with patch("connectors.keboola.metadata.KeboolaStorageClient") as MockStorage:
        instance = MockStorage.return_value
        instance.get_table_info.return_value = {
            "rowsCount": 1234,
            "dataSizeBytes": 500_000,
            "primaryKey": ["id"],
        }
        result = metadata.fetch(req)

    assert result == TableMetadata(
        rows=1234,
        size_bytes=500_000,
        partition_by=None,
        clustered_by=None,
    )


def test_returns_none_when_unconfigured(req, monkeypatch):
    """No KEBOOLA_STACK_URL / KEBOOLA_STORAGE_TOKEN env → return None."""
    from connectors.keboola import metadata
    monkeypatch.delenv("KEBOOLA_STACK_URL", raising=False)
    monkeypatch.delenv("KEBOOLA_STORAGE_TOKEN", raising=False)
    assert metadata.fetch(req) is None


def test_returns_none_on_storage_api_error(req, monkeypatch):
    """`StorageApiError` from get_table_info → log + return None."""
    from connectors.keboola import metadata
    from connectors.keboola.storage_api import StorageApiError
    monkeypatch.setenv("KEBOOLA_STACK_URL", "https://x.keboola.com")
    monkeypatch.setenv("KEBOOLA_STORAGE_TOKEN", "tok")

    with patch("connectors.keboola.metadata.KeboolaStorageClient") as MockStorage:
        instance = MockStorage.return_value
        instance.get_table_info.side_effect = StorageApiError(
            "404 not found", status=404, body={},
        )
        assert metadata.fetch(req) is None


def test_table_id_uses_bucket_dot_source_table(req, monkeypatch):
    """Storage API path is `<bucket>.<source_table>`."""
    from connectors.keboola import metadata
    monkeypatch.setenv("KEBOOLA_STACK_URL", "https://x.keboola.com")
    monkeypatch.setenv("KEBOOLA_STORAGE_TOKEN", "tok")

    with patch("connectors.keboola.metadata.KeboolaStorageClient") as MockStorage:
        instance = MockStorage.return_value
        instance.get_table_info.return_value = {
            "rowsCount": 0, "dataSizeBytes": 0,
        }
        metadata.fetch(req)
        instance.get_table_info.assert_called_once_with("in.c-crm.orders")
