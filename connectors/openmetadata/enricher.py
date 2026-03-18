"""
OpenMetadata Catalog Data Enricher

High-level enrichment layer that:
1. Initializes from instance config (disabled gracefully if no token)
2. Caches table metadata with TTL (default 1 hour)
3. Parses OpenMetadata responses into typed data
4. Enriches table/column metadata at sync and query time
5. Gracefully degrades on errors (never crashes app)
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any

from src.config import TableConfig
from .client import OpenMetadataClient


logger = logging.getLogger(__name__)


@dataclass
class CatalogColumnData:
    """Column metadata enriched from OpenMetadata catalog."""

    description: str
    data_type: str
    tags: List[str] = field(default_factory=list)


@dataclass
class CatalogTableData:
    """Table metadata enriched from OpenMetadata catalog."""

    description: str
    columns: Dict[str, CatalogColumnData]  # key = lowercase column name
    tags: List[str] = field(default_factory=list)
    owners: List[str] = field(default_factory=list)  # owner names
    tier: Optional[str] = None  # "Tier1", "Tier2", etc.
    catalog_url: Optional[str] = None  # Direct link to catalog


class CatalogEnricher:
    """
    Enriches table and column metadata from OpenMetadata catalog.

    Usage:
        enricher = CatalogEnricher(instance_config)
        if enricher.enabled:
            catalog_data = enricher.enrich_table(table_config)
            # Use catalog_data.description, columns, tags, owners, tier
    """

    enabled: bool

    def __init__(self, instance_config: Dict[str, Any]):
        """
        Initialize enricher from instance config.

        Args:
            instance_config: Dictionary with optional "openmetadata" section:
                {
                    "openmetadata": {
                        "url": "https://catalog.example.com",
                        "token": "jwt-token-or-env-var",
                        "cache_ttl_seconds": 3600
                    }
                }

        Sets self.enabled = False if:
        - No "openmetadata" section in config
        - No token provided (returns gracefully, not an error)
        - URL is invalid
        """
        self.enabled = False
        self._client: Optional[OpenMetadataClient] = None
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._cache_ttl_seconds = 3600

        try:
            om_config = instance_config.get("openmetadata", {})
            if not om_config:
                logger.debug("OpenMetadata not configured (openmetadata section missing)")
                return

            url = om_config.get("url", "").strip()
            token = om_config.get("token", "").strip()
            cache_ttl = om_config.get("cache_ttl_seconds", 3600)

            if not url or not token:
                logger.debug(
                    f"OpenMetadata disabled: url={bool(url)}, token={bool(token)}"
                )
                return

            self._cache_ttl_seconds = cache_ttl
            self._client = OpenMetadataClient(base_url=url, token=token)
            self.enabled = True

            logger.info(
                f"OpenMetadata enricher initialized: {url} (cache TTL: {cache_ttl}s)"
            )

        except Exception as e:
            logger.warning(
                f"Failed to initialize OpenMetadata enricher: {e}. Continuing without catalog enrichment."
            )
            self.enabled = False

    def enrich_table(
        self, table_config: TableConfig
    ) -> Optional[CatalogTableData]:
        """
        Enrich table metadata from catalog.

        Args:
            table_config: Table configuration with id and optional catalog_fqn

        Returns:
            CatalogTableData with description, columns, tags, owners, tier
            or None if:
            - enricher is disabled
            - FQN derivation fails
            - HTTP request fails
            - Parsing fails
            Always returns None gracefully, never raises exception.
        """
        if not self.enabled or not self._client:
            return None

        try:
            fqn = self._derive_fqn(table_config)
            if not fqn:
                return None

            # Check cache
            cached = self._get_from_cache(fqn)
            if cached:
                logger.debug(f"Catalog cache hit: {fqn}")
                return cached

            # Fetch from API
            logger.debug(f"Fetching catalog metadata: {fqn}")
            raw_response = self._client.get_table(fqn)

            # Parse and cache
            parsed = self._parse_table_response(raw_response)
            if parsed:
                self._cache_entry(fqn, parsed)

            return parsed

        except Exception as e:
            logger.warning(f"Error enriching table {table_config.name}: {e}")
            return None

    def _derive_fqn(self, table_config: TableConfig) -> Optional[str]:
        """
        Derive OpenMetadata FQN from table config.

        Auto-derivation: bigquery.{table_config.id}
        Example: table_config.id = "prj-grp-dataview-prod-1ff9.marketing.roi_datamart_v2"
                 -> FQN = "bigquery.prj-grp-dataview-prod-1ff9.marketing.roi_datamart_v2"

        Args:
            table_config: Configuration with id and optional catalog_fqn

        Returns:
            FQN string or None if derivation fails
        """
        try:
            # Use explicit override if provided
            if hasattr(table_config, "catalog_fqn") and table_config.catalog_fqn:
                return table_config.catalog_fqn

            # Auto-derive: bigquery.{table_id}
            return f"bigquery.{table_config.id}"

        except Exception as e:
            logger.warning(f"FQN derivation failed for {table_config.name}: {e}")
            return None

    def _parse_table_response(self, raw: Dict[str, Any]) -> Optional[CatalogTableData]:
        """
        Parse OpenMetadata table response into typed CatalogTableData.

        Args:
            raw: Raw response from OpenMetadata /api/v1/tables/name/{fqn}

        Returns:
            CatalogTableData or None if parsing fails
        """
        try:
            description = raw.get("description", "") or ""

            # Parse columns
            columns = {}
            for col in raw.get("columns", []):
                col_name = col.get("name", "").lower()
                col_description = col.get("description", "") or ""
                col_type = col.get("dataType", "")

                columns[col_name] = CatalogColumnData(
                    description=col_description,
                    data_type=col_type,
                    tags=self._extract_column_tags(col),
                )

            # Parse tags
            tags = self._extract_tags(raw.get("tags", []))

            # Parse owners
            owners = self._extract_owners(raw.get("owners", []))

            # Parse tier from extension metadata
            tier = None
            extension = raw.get("extension", {})
            if extension:
                tier = extension.get("tier") or extension.get("Tier")

            # Log if we found tier or tags (for debugging)
            if tags or tier:
                logger.info(f"Found catalog enrichment: tags={tags}, tier={tier}")

            # Build catalog URL
            fqn = raw.get("fullyQualifiedName", "")
            catalog_url = None
            if fqn:
                # Link to table entity page in OpenMetadata
                catalog_url = f"{self._client.base_url}/table/{fqn}"

            return CatalogTableData(
                description=description,
                columns=columns,
                tags=tags,
                owners=owners,
                tier=tier,
                catalog_url=catalog_url,
            )

        except Exception as e:
            logger.warning(f"Failed to parse catalog response: {e}")
            return None

    def _extract_tags(self, tags_list: List[Dict[str, Any]]) -> List[str]:
        """Extract tag names from tags list."""
        try:
            return [tag.get("name") or tag.get("tagFQN", "").split(".")[-1] for tag in tags_list]
        except Exception:
            return []

    def _extract_column_tags(self, column: Dict[str, Any]) -> List[str]:
        """Extract tags for a single column."""
        return self._extract_tags(column.get("tags", []))

    def _extract_owners(self, owners_list: List[Dict[str, Any]]) -> List[str]:
        """Extract owner names from owners list."""
        try:
            names = []
            for owner in owners_list:
                name = owner.get("name") or owner.get("displayName", "")
                if name:
                    names.append(name)
            return names
        except Exception:
            return []

    def _get_from_cache(self, fqn: str) -> Optional[CatalogTableData]:
        """
        Check if cached entry exists and is still valid (TTL).

        Args:
            fqn: Fully qualified name

        Returns:
            CatalogTableData if valid, None if expired or missing
        """
        if fqn not in self._cache:
            return None

        entry = self._cache[fqn]
        fetched_at = entry.get("fetched_at")

        if not fetched_at:
            return None

        age = datetime.now() - fetched_at
        if age > timedelta(seconds=self._cache_ttl_seconds):
            del self._cache[fqn]
            return None

        return entry.get("data")

    def _cache_entry(self, fqn: str, data: CatalogTableData):
        """Cache an enriched table entry."""
        self._cache[fqn] = {
            "data": data,
            "fetched_at": datetime.now(),
        }

    def get_metrics(self, limit: int = 200) -> List[Dict[str, Any]]:
        """
        Fetch list of business metrics from OpenMetadata catalog.

        Args:
            limit: Maximum number of metrics to fetch (default: 200)

        Returns:
            List of metric dictionaries with id, name, fullyQualifiedName, description, etc.
            Returns empty list if:
            - enricher is disabled
            - catalog unavailable
            - HTTP request fails
            Never raises exception (graceful degradation).
        """
        if not self.enabled or not self._client:
            return []

        try:
            # Check cache first
            cached = self._get_from_cache("__metrics_list__")
            if cached is not None:
                logger.debug("Catalog cache hit: metrics list")
                return cached

            # Fetch from API
            logger.debug(f"Fetching {limit} metrics from catalog")
            metrics = self._client.get_metrics(limit=limit)

            # Cache the result (with TTL)
            self._cache_entry("__metrics_list__", metrics)

            logger.info(f"Loaded {len(metrics)} metrics from catalog")
            return metrics

        except Exception as e:
            logger.warning(f"Failed to fetch metrics from catalog: {e}")
            return []

    def get_metrics_by_data_product(self, data_product_name: str, limit: int = 200) -> List[Dict[str, Any]]:
        """
        Fetch metrics belonging to a specific data product.

        Preferred over get_metrics() + tag filter when data_product is configured,
        as it returns exactly the metrics in the data product regardless of tags.

        Returns empty list if enricher disabled, catalog unavailable, or on error.
        Never raises exception (graceful degradation).
        """
        if not self.enabled or not self._client:
            return []

        try:
            cache_key = f"__metrics_dp_{data_product_name}__"
            cached = self._get_from_cache(cache_key)
            if cached is not None:
                logger.debug(f"Catalog cache hit: metrics for data product '{data_product_name}'")
                return cached

            metrics = self._client.search_by_data_product(data_product_name, entity_type="metric", limit=limit)
            self._cache_entry(cache_key, metrics)
            logger.info(f"Loaded {len(metrics)} metrics from data product '{data_product_name}'")
            return metrics

        except Exception as e:
            logger.warning(f"Failed to fetch metrics for data product '{data_product_name}': {e}")
            return []

    def clear_cache(self):
        """Manually clear all cached entries."""
        self._cache.clear()
        logger.info("Catalog cache cleared")

    def __del__(self):
        """Cleanup HTTP client on deletion."""
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
