"""Keboola metadata provider — populates `TableMetadata` for a Keboola
registry row via the Storage API.

Stub: returns None pending the full implementation in Task 6.
"""

from __future__ import annotations

from app.api._metadata_models import MetadataRequest, TableMetadata


def fetch(req: MetadataRequest) -> TableMetadata | None:
    return None
