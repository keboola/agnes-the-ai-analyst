"""Keboola-specific ingest adapter for linked data apps.

This is the ONLY Keboola-aware code in the linked-app pipeline: it names the
materialized table an MCP source's data-app lister produces and maps its rows to
the transport-neutral ``LinkedAppRecord`` the generic projection consumes
(``src/data_apps/linked_projection.py``). A future entity type gets its own
adapter with the same shape; the projection/registry contract does not change.

If the Keboola MCP tool returns only partial metadata (e.g. no URL), the
supplement (Keboola Storage API for ``keboola.data-apps`` component configs)
belongs HERE — the projection only needs ``{external_app_id, name, external_url}``.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Dict

# The table the Universal-MCP materialize path writes for the data-app lister
# tool (one row per Keboola data-app config). Kept as a constant so the sync
# wiring and the tests agree on the name.
MATERIALIZED_TABLE = "keboola_data_apps"


@dataclass(frozen=True)
class LinkedAppRecord:
    """One externally-hosted app, transport-neutral (what the projection needs)."""

    external_app_id: str
    name: str
    description: str
    external_url: str


def source_ref(connection_id: str, external_app_id: str) -> str:
    """Provenance key for one linked app: ``"<connection_id>:<external_app_id>"``."""
    return f"{connection_id}:{external_app_id}"


def source_ref_prefix(connection_id: str) -> str:
    """Prefix that scopes a reconcile to one connection's linked rows."""
    return f"{connection_id}:"


def _sanitize(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "app"


def slug_for(connection_id: str, external_app_id: str) -> str:
    """Deterministic, URL-safe, collision-free slug for a linked app.

    Stable across re-syncs (same inputs → same slug) and unique even if two
    connections share an ``external_app_id`` — a short hash of the full
    ``source_ref`` disambiguates, so an INSERT never trips the ``slug`` UNIQUE
    constraint.
    """
    ref = source_ref(connection_id, external_app_id)
    short = hashlib.sha1(ref.encode()).hexdigest()[:6]
    return f"kbc-{_sanitize(external_app_id)}-{short}"


def map_row(raw: Dict[str, Any]) -> LinkedAppRecord:
    """Map one materialized ``keboola_data_apps`` row to a ``LinkedAppRecord``.

    Tolerant of the actual column names the MCP tool emits (``id``/``app_id``/
    ``config_id`` for the identifier, ``url``/``app_url`` for the deployment URL).
    """

    def pick(*keys: str) -> str:
        for k in keys:
            v = raw.get(k)
            if v not in (None, ""):
                return str(v)
        return ""

    return LinkedAppRecord(
        external_app_id=pick("external_app_id", "id", "app_id", "config_id"),
        name=pick("name", "app_name", "title"),
        description=pick("description", "desc"),
        external_url=pick("external_url", "url", "app_url", "deployment_url"),
    )
