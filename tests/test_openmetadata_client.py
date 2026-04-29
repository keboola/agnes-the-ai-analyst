"""
Tests for OpenMetadata client
"""

import warnings

import pytest
import httpx
from unittest.mock import Mock, patch, MagicMock

from connectors.openmetadata.client import OpenMetadataClient


@pytest.fixture
def mock_httpx_client():
    """Mock httpx.Client."""
    with patch("connectors.openmetadata.client.httpx.Client") as mock:
        yield mock


def test_client_init(mock_httpx_client):
    """Test OpenMetadataClient initialization."""
    client = OpenMetadataClient(
        base_url="https://catalog.example.com",
        token="test-token",
        timeout=30,
    )

    assert client.base_url == "https://catalog.example.com"
    assert client.token == "test-token"
    assert client.timeout == 30

    # Verify httpx.Client was called with correct headers
    mock_httpx_client.assert_called_once()
    call_kwargs = mock_httpx_client.call_args[1]
    assert call_kwargs["headers"]["Authorization"] == "Bearer test-token"


def test_client_init_strips_trailing_slash():
    """Test that base_url trailing slash is stripped."""
    with patch("connectors.openmetadata.client.httpx.Client"):
        client = OpenMetadataClient(
            base_url="https://catalog.example.com/",
            token="test-token",
        )
        assert client.base_url == "https://catalog.example.com"


def test_get_table_success():
    """Test successful get_table() call."""
    with patch("connectors.openmetadata.client.httpx.Client") as mock_client_class:
        mock_client_instance = MagicMock()
        mock_client_class.return_value = mock_client_instance

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "id": "table-id",
            "name": "roi_datamart_v2",
            "fullyQualifiedName": "bigquery.project.dataset.table",
            "description": "Test table",
            "columns": [
                {"name": "id", "dataType": "INTEGER", "description": "ID column"},
                {"name": "name", "dataType": "STRING", "description": "Name column"},
            ],
            "tags": [{"name": "important"}],
            "owners": [{"name": "Data Team", "email": "data@example.com"}],
        }
        mock_client_instance.get.return_value = mock_response

        client = OpenMetadataClient(
            base_url="https://catalog.example.com",
            token="test-token",
        )
        result = client.get_table("bigquery.project.dataset.table")

        assert result["name"] == "roi_datamart_v2"
        assert len(result["columns"]) == 2

        # Verify correct API endpoint and params
        mock_client_instance.get.assert_called_once()
        call_args = mock_client_instance.get.call_args
        assert "/api/v1/tables/name/bigquery.project.dataset.table" in str(call_args)


def test_get_table_http_error():
    """Test get_table() with HTTP error."""
    with patch("connectors.openmetadata.client.httpx.Client") as mock_client_class:
        mock_client_instance = MagicMock()
        mock_client_class.return_value = mock_client_instance

        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "401 Unauthorized",
            request=MagicMock(),
            response=MagicMock(status_code=401),
        )
        mock_client_instance.get.return_value = mock_response

        client = OpenMetadataClient(
            base_url="https://catalog.example.com",
            token="invalid-token",
        )

        with pytest.raises(httpx.HTTPStatusError):
            client.get_table("bigquery.project.dataset.table")


def test_get_metrics_success():
    """Test successful get_metrics() call."""
    with patch("connectors.openmetadata.client.httpx.Client") as mock_client_class:
        mock_client_instance = MagicMock()
        mock_client_class.return_value = mock_client_instance

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "data": [
                {
                    "id": "metric-1",
                    "name": "revenue",
                    "fullyQualifiedName": "metrics.revenue",
                    "description": "Total revenue",
                    "expression": "SUM(amount)",
                },
                {
                    "id": "metric-2",
                    "name": "users",
                    "fullyQualifiedName": "metrics.users",
                    "description": "Active users",
                    "expression": "COUNT(DISTINCT user_id)",
                },
            ]
        }
        mock_client_instance.get.return_value = mock_response

        client = OpenMetadataClient(
            base_url="https://catalog.example.com",
            token="test-token",
        )
        result = client.get_metrics(limit=10)

        assert len(result) == 2
        assert result[0]["name"] == "revenue"
        assert result[1]["name"] == "users"


def test_context_manager():
    """Test client can be used as context manager."""
    with patch("connectors.openmetadata.client.httpx.Client") as mock_client_class:
        mock_client_instance = MagicMock()
        mock_client_class.return_value = mock_client_instance

        with OpenMetadataClient(
            base_url="https://catalog.example.com",
            token="test-token",
        ) as client:
            assert client is not None

        # Verify close() was called
        mock_client_instance.close.assert_called_once()


# --- TLS verify (#89) -------------------------------------------------------


def test_client_verifies_tls_by_default():
    """Default `verify=True` — no more silent MITM exposure of the JWT."""
    with patch("connectors.openmetadata.client.httpx.Client") as mock_client:
        OpenMetadataClient(base_url="https://catalog.example.com", token="t")
    kwargs = mock_client.call_args.kwargs
    assert kwargs["verify"] is True


def test_client_accepts_explicit_verify_false():
    """Operators on internal CAs may opt out — but it must be explicit."""
    with patch("connectors.openmetadata.client.httpx.Client") as mock_client:
        OpenMetadataClient(base_url="https://catalog.example.com", token="t", verify=False)
    assert mock_client.call_args.kwargs["verify"] is False


def test_client_accepts_custom_ca_bundle_path():
    """A path string passed to verify is forwarded to httpx untouched
    (httpx then uses it as the trust store)."""
    with patch("connectors.openmetadata.client.httpx.Client") as mock_client:
        OpenMetadataClient(
            base_url="https://catalog.example.com",
            token="t",
            verify="/etc/ssl/certs/internal-ca.pem",
        )
    assert mock_client.call_args.kwargs["verify"] == "/etc/ssl/certs/internal-ca.pem"


def test_module_import_does_not_mutate_global_warnings_filter():
    """The previous version called warnings.filterwarnings('ignore', ...)
    at import time, suppressing urllib3 warnings for ALL httpx clients in
    the process. Drop the side effect."""
    import importlib
    pre_filters = list(warnings.filters)
    import connectors.openmetadata.client as om
    importlib.reload(om)
    post_filters = list(warnings.filters)
    new = [f for f in post_filters if f not in pre_filters]
    for action, message, *_ in new:
        if message is not None:
            assert "Unverified HTTPS request" not in message.pattern
