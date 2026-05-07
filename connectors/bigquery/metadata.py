"""BigQuery metadata provider — populates `TableMetadata` for a remote
BQ-backed registry row.

Stub: returns None pending the full implementation in Task 7.
"""

from __future__ import annotations

from app.api._metadata_models import MetadataRequest, TableMetadata


def fetch(req: MetadataRequest) -> TableMetadata | None:
    return None
