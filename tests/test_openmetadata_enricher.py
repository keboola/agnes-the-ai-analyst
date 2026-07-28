"""
Tests for OpenMetadata catalog enricher
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock
from dataclasses import dataclass

from connectors.openmetadata.enricher import (
    CatalogEnricher,
    CatalogTableData,
    CatalogColumnData,
    TableConfig,
)


@pytest.fixture
def sample_table_config():
    """Sample table configuration."""
    return TableConfig(
        id="prj-example-1234.marketing.roi_datamart_v2",
        name="roi_datamart_v2",
    )


@pytest.fixture
def sample_om_response():
    """Sample OpenMetadata API response."""
    return {
        "id": "table-uuid",
        "name": "roi_datamart_v2",
        "fullyQualifiedName": "bigquery.prj-example-1234.marketing.roi_datamart_v2",
        "description": "Daily ROI analytics",
        "columns": [
            {
                "name": "id",
                "dataType": "BIGINT",
                "description": "Record ID",
                "tags": [{"name": "pii"}],
            },
            {
                "name": "revenue",
                "dataType": "DECIMAL",
                "description": "Revenue amount",
                "tags": [],
            },
        ],
        "tags": [{"name": "analytics"}, {"name": "daily"}],
        "owners": [
            {"name": "Analytics Team", "email": "analytics@example.com"},
        ],
        "extension": {"tier": "Tier1"},
    }


def test_enricher_disabled_no_config():
    """Test enricher is disabled when openmetadata section is missing."""
    enricher = CatalogEnricher({})
    assert enricher.enabled is False


def test_enricher_disabled_no_token():
    """Test enricher is disabled when token is missing."""
    enricher = CatalogEnricher(
        {
            "openmetadata": {
                "url": "https://catalog.example.com",
                # no token
            }
        }
    )
    assert enricher.enabled is False


def test_enricher_disabled_no_url():
    """Test enricher is disabled when URL is missing."""
    enricher = CatalogEnricher(
        {
            "openmetadata": {
                "token": "test-token",
                # no url
            }
        }
    )
    assert enricher.enabled is False


def test_enricher_init_success():
    """Test enricher initialization with valid config."""
    with patch("connectors.openmetadata.enricher.OpenMetadataClient"):
        enricher = CatalogEnricher(
            {
                "openmetadata": {
                    "url": "https://catalog.example.com",
                    "token": "test-token",
                    "cache_ttl_seconds": 3600,
                }
            }
        )
        assert enricher.enabled is True


def test_enrich_table_disabled():
    """Test enrich_table returns None when enricher is disabled."""
    enricher = CatalogEnricher({})

    table_config = TableConfig(
        id="test.table",
        name="test",
    )

    result = enricher.enrich_table(table_config)
    assert result is None


def test_enrich_table_cache_hit():
    """Test enrich_table returns cached data."""
    with patch("connectors.openmetadata.enricher.OpenMetadataClient"):
        enricher = CatalogEnricher(
            {
                "openmetadata": {
                    "url": "https://catalog.example.com",
                    "token": "test-token",
                }
            }
        )

        # Pre-populate cache
        cached_data = CatalogTableData(
            description="Cached description",
            columns={"id": CatalogColumnData(description="ID", data_type="BIGINT")},
        )
        enricher._cache_entry(
            "bigquery.prj-example-1234.marketing.test",
            cached_data,
        )

        table_config = TableConfig(
            id="prj-example-1234.marketing.test",
            name="test",
        )

        result = enricher.enrich_table(table_config)
        assert result is not None
        assert result.description == "Cached description"


def test_enrich_table_cache_expiry():
    """Test cache entry expires after TTL."""
    with patch("connectors.openmetadata.enricher.OpenMetadataClient"):
        enricher = CatalogEnricher(
            {
                "openmetadata": {
                    "url": "https://catalog.example.com",
                    "token": "test-token",
                    "cache_ttl_seconds": 1,  # 1 second TTL
                }
            }
        )

        # Pre-populate cache with old entry
        cached_data = CatalogTableData(
            description="Old data",
            columns={},
        )
        fqn = "bigquery.prj-example-1234.marketing.test"
        enricher._cache[fqn] = {
            "data": cached_data,
            "fetched_at": datetime.now() - timedelta(seconds=2),  # 2 seconds old
        }

        # Should return None due to expiry
        result = enricher._get_from_cache(fqn)
        assert result is None


def test_derive_fqn_auto():
    """Test FQN auto-derivation from table ID."""
    with patch("connectors.openmetadata.enricher.OpenMetadataClient"):
        enricher = CatalogEnricher(
            {
                "openmetadata": {
                    "url": "https://catalog.example.com",
                    "token": "test-token",
                }
            }
        )

        table_config = TableConfig(
            id="prj-example-1234.marketing.roi_datamart_v2",
            name="roi_datamart_v2",
        )

        fqn = enricher._derive_fqn(table_config)
        assert fqn == "bigquery.prj-example-1234.marketing.roi_datamart_v2"


def test_derive_fqn_explicit_override():
    """Test FQN explicit override via catalog_fqn."""
    with patch("connectors.openmetadata.enricher.OpenMetadataClient"):
        enricher = CatalogEnricher(
            {
                "openmetadata": {
                    "url": "https://catalog.example.com",
                    "token": "test-token",
                }
            }
        )

        table_config = TableConfig(
            id="prj-example-1234.marketing.roi_datamart_v2",
            name="roi_datamart_v2",
        )
        table_config.catalog_fqn = "bigquery.custom.fqn.override"

        fqn = enricher._derive_fqn(table_config)
        assert fqn == "bigquery.custom.fqn.override"


def test_parse_table_response(sample_om_response):
    """Test parsing OpenMetadata table response."""
    with patch("connectors.openmetadata.enricher.OpenMetadataClient") as mock_client_cls:
        mock_client_cls.return_value.base_url = "https://catalog.example.com"
        enricher = CatalogEnricher(
            {
                "openmetadata": {
                    "url": "https://catalog.example.com",
                    "token": "test-token",
                }
            }
        )

        result = enricher._parse_table_response(sample_om_response)

        assert result is not None
        assert result.description == "Daily ROI analytics"
        assert len(result.columns) == 2

        # Check lowercase column key
        assert "id" in result.columns
        assert result.columns["id"].description == "Record ID"
        assert result.columns["id"].data_type == "BIGINT"

        assert len(result.tags) == 2
        assert "analytics" in result.tags

        assert len(result.owners) == 1
        assert "Analytics Team" in result.owners

        assert result.tier == "Tier1"
        assert "catalog.example.com" in result.catalog_url


def test_parse_table_response_with_minimal_data():
    """Test parsing response with minimal fields."""
    with patch("connectors.openmetadata.enricher.OpenMetadataClient"):
        enricher = CatalogEnricher(
            {
                "openmetadata": {
                    "url": "https://catalog.example.com",
                    "token": "test-token",
                }
            }
        )

        minimal_response = {
            "name": "minimal_table",
            "fullyQualifiedName": "bigquery.minimal.table",
            # Missing description, columns, tags, owners, extension
        }

        result = enricher._parse_table_response(minimal_response)

        assert result is not None
        assert result.description == ""
        assert len(result.columns) == 0
        assert len(result.tags) == 0
        assert len(result.owners) == 0
        assert result.tier is None


def test_extract_tags():
    """Test tag extraction."""
    with patch("connectors.openmetadata.enricher.OpenMetadataClient"):
        enricher = CatalogEnricher(
            {
                "openmetadata": {
                    "url": "https://catalog.example.com",
                    "token": "test-token",
                }
            }
        )

        tags = [
            {"name": "important"},
            {"tagFQN": "tags.sensitive"},
            {"name": "", "tagFQN": "tags.fallback"},  # Test fallback
        ]

        result = enricher._extract_tags(tags)
        assert "important" in result
        assert "sensitive" in result
        assert "fallback" in result


def test_cache_behavior():
    """Test cache hit and miss."""
    with patch("connectors.openmetadata.enricher.OpenMetadataClient"):
        enricher = CatalogEnricher(
            {
                "openmetadata": {
                    "url": "https://catalog.example.com",
                    "token": "test-token",
                    "cache_ttl_seconds": 3600,
                }
            }
        )

        fqn = "bigquery.test.table"
        data = CatalogTableData(
            description="Test",
            columns={},
        )

        # Cache miss
        assert enricher._get_from_cache(fqn) is None

        # Cache entry
        enricher._cache_entry(fqn, data)

        # Cache hit
        cached = enricher._get_from_cache(fqn)
        assert cached is not None
        assert cached.description == "Test"


def test_clear_cache():
    """Test cache clearing."""
    with patch("connectors.openmetadata.enricher.OpenMetadataClient"):
        enricher = CatalogEnricher(
            {
                "openmetadata": {
                    "url": "https://catalog.example.com",
                    "token": "test-token",
                }
            }
        )

        data = CatalogTableData(description="Test", columns={})
        enricher._cache_entry("bigquery.test1", data)
        enricher._cache_entry("bigquery.test2", data)

        assert len(enricher._cache) == 2

        enricher.clear_cache()
        assert len(enricher._cache) == 0


def test_enrich_table_http_error_graceful():
    """Test enrich_table gracefully handles HTTP errors."""
    mock_client = MagicMock()
    mock_client.get_table.side_effect = Exception("Connection refused")

    with patch("connectors.openmetadata.enricher.OpenMetadataClient", return_value=mock_client):
        enricher = CatalogEnricher(
            {
                "openmetadata": {
                    "url": "https://catalog.example.com",
                    "token": "test-token",
                }
            }
        )

        table_config = TableConfig(
            id="test.table",
            name="test",
        )

        # Should return None, not raise
        result = enricher.enrich_table(table_config)
        assert result is None
