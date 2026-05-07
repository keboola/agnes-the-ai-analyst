"""Keboola metadata provider — populates `TableMetadata` for a Keboola
registry row via the Storage API.

Reuses `KeboolaClient(token=None, url=None)` to inherit the existing
env-var fallback path (`KEBOOLA_STACK_URL` + `KEBOOLA_STORAGE_TOKEN`),
which is the same hierarchy `connectors/keboola/extractor.py` and
`connectors/keboola/client.py` already use. **Does NOT introduce a third
token-resolution helper.**
"""

from __future__ import annotations

import logging
import os

from app.api._metadata_models import MetadataRequest, TableMetadata
from connectors.keboola.storage_api import (
    KeboolaStorageClient,
    StorageApiError,
)

logger = logging.getLogger(__name__)


def fetch(req: MetadataRequest) -> TableMetadata | None:
    """Return Keboola Storage API metadata for the given table, or None.

    Keboola has no BigQuery-style partition/cluster concept; primaryKey is
    conceptually different (uniqueness, not physical layout), so
    `partition_by` and `clustered_by` are left None.
    """
    # Read credentials the same way KeboolaClient does — avoids constructing
    # a KeboolaClient which raises ValueError when the token is absent.
    url = os.environ.get("KEBOOLA_STACK_URL", "")
    token = os.environ.get("KEBOOLA_STORAGE_TOKEN", "")
    if not url or not token:
        return None  # not configured — same posture as BQ sentinel

    table_id = f"{req.bucket}.{req.source_table}"
    try:
        storage = KeboolaStorageClient(url=url, token=token)
        info = storage.get_table_info(table_id)
    except (StorageApiError, ValueError) as e:
        logger.warning("Keboola metadata fetch failed for %s: %s", table_id, e)
        return None

    return TableMetadata(
        rows=info.get("rowsCount"),
        size_bytes=info.get("dataSizeBytes"),
        partition_by=None,
        clustered_by=None,
    )
