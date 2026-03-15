"""
OpenMetadata REST API Client

Low-level HTTP wrapper for OpenMetadata REST API with these functions:
1. Authentication using JWT bearer token
2. Get table metadata (description, columns, tags, owners)
3. Get metrics (for Phase 2)
4. Proper error handling and logging
"""

import logging
from typing import Dict, List, Optional, Any
import warnings

import httpx

# Suppress SSL warnings for self-signed certificates
warnings.filterwarnings("ignore", message="Unverified HTTPS request")


logger = logging.getLogger(__name__)


class OpenMetadataClient:
    """
    HTTP client for OpenMetadata REST API.

    Provides methods for querying table metadata:
    - get_table(fqn) -> table metadata with columns, owners, tags
    - get_metrics() -> list of available business metrics
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: int = 30,
    ):
        """
        Initialize OpenMetadata API client.

        Args:
            base_url: Base URL of OpenMetadata instance (e.g., "https://catalog.example.com")
            token: JWT bearer token for authentication
            timeout: HTTP request timeout in seconds
        """
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
            verify=False,  # Allow self-signed certificates (internal networks)
        )

    def get_table(self, fqn: str) -> Dict[str, Any]:
        """
        Fetch table metadata from OpenMetadata.

        Args:
            fqn: Fully qualified name (e.g., "bigquery.project.dataset.table")

        Returns:
            Dictionary with table metadata including:
            - id, name, fullyQualifiedName
            - description
            - columns: list of column dicts with name, dataType, description
            - tags: list of tag dicts
            - owners: list of owner dicts with name, email
            - extension: custom metadata (e.g., tier)

        Raises:
            httpx.HTTPStatusError: If request fails (non-2xx status)
        """
        url = f"/api/v1/tables/name/{fqn}"
        params = {
            "fields": "columns,owners,tags,extension",
            "include": "all",
        }

        response = self._client.get(url, params=params)
        response.raise_for_status()

        return response.json()

    def get_metrics(self, limit: int = 100, fields: str = "tags,owners") -> List[Dict[str, Any]]:
        """
        Fetch list of available metrics from OpenMetadata.

        Args:
            limit: Maximum number of metrics to return
            fields: Comma-separated list of fields to include (e.g., "tags,owners")

        Returns:
            List of metric dictionaries with:
            - id, name, fullyQualifiedName
            - description
            - metricExpression: metric calculation SQL/formula
            - owners, tags (when requested via fields)
        """
        params = {
            "limit": limit,
            "fields": fields,
        }

        response = self._client.get("/api/v1/metrics", params=params)
        response.raise_for_status()

        data = response.json()
        return data.get("data", [])

    def get_metric_by_fqn(self, fqn: str, fields: str = "tags,owners") -> Dict[str, Any]:
        """
        Fetch a specific metric by FQN from OpenMetadata.

        Args:
            fqn: Fully qualified name (e.g., "Active2 Customers")
            fields: Comma-separated list of fields to include

        Returns:
            Dictionary with metric metadata:
            - id, name, fullyQualifiedName
            - description, metricExpression
            - owners, tags (when requested via fields)

        Raises:
            httpx.HTTPStatusError: If request fails (non-2xx status)
        """
        url = f"/api/v1/metrics/name/{fqn}"
        params = {"fields": fields}

        response = self._client.get(url, params=params)
        response.raise_for_status()

        return response.json()

    def close(self):
        """Close HTTP client session."""
        self._client.close()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
