"""Shared data shapes for source-agnostic table-metadata providers.

Lives under `app/api/` because the primary consumer is
`app/api/v2_catalog.py`. Connector-side providers in `connectors/<source>/`
import upward into this module — the inverse layering would force
`v2_catalog.py` to depend on `connectors/__init__.py`, which is the
wrong direction.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MetadataRequest:
    """Narrow input passed to a metadata provider's `fetch()`.

    `bucket` and `source_table` are pre-validated by the dispatcher
    (`validate_quoted_identifier`) before construction, so the provider
    can interpolate them into SQL/URL paths without re-checking. Frozen
    so the (provider, request)-keyed cache lookup is stable.
    """
    table_id: str
    bucket: str
    source_table: str


@dataclass
class TableMetadata:
    """Source-agnostic metadata bundle. Every field optional — providers
    fill what they can cheaply get; callers tolerate `None`. Adding a new
    field here is a non-breaking change: existing CLI consumers don't
    even render `rough_size_hint` (verified `grep -rn rough_size_hint cli/`
    is empty), let alone the new fields.
    """
    rows: int | None = None
    size_bytes: int | None = None
    partition_by: str | None = None
    clustered_by: list[str] | None = None
