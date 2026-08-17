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
from typing import Any, Dict, Optional

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


def _safe_url(value: str) -> str:
    """Keep only http(s) URLs; anything else becomes '' (row gets skipped).

    The materialized table is UNTRUSTED upstream data (same trust class as
    connector output), and the stored ``external_url`` is rendered as a raw
    ``href`` on the web pages and returned verbatim by the API/MCP/CLI
    surfaces — Jinja2 autoescaping does not neutralize a ``javascript:``
    scheme inside an href. This ingest point is the ONLY writer of
    ``external_url``, so scheme-gating here covers every render path
    (review team on #1116; security playbook: validate untrusted input).
    """
    from urllib.parse import urlparse

    v = value.strip()
    if not v:
        return ""
    return v if urlparse(v).scheme.lower() in ("http", "https") else ""


def map_row(raw: Dict[str, Any], mapping: Optional[Dict[str, str]] = None) -> LinkedAppRecord:
    """Map one materialized lister row to a ``LinkedAppRecord``.

    ``mapping`` — ``{"id": col, "url": col, "name": col, "description": col}``,
    the admin's explicit choice of which columns carry what — wins whenever it
    names a key. Anything it leaves out falls back to the alias guesses below.

    The guesses exist because the first upstream happened to fit them; they are
    not a contract. A server naming its columns anything else has every row
    dropped for want of an id, and the projection reports "0 new, 0 updated" —
    indistinguishable from an upstream with nothing to offer. That is what the
    mapping is for; the aliases stay as the zero-configuration path for
    upstreams they already suit.
    """

    def pick(*keys: str) -> str:
        for k in keys:
            v = raw.get(k)
            if v not in (None, ""):
                return str(v)
        return ""

    def chosen(field: str, *fallback: str) -> str:
        col = (mapping or {}).get(field)
        if col:
            # An explicit choice is authoritative even when it comes back
            # empty: silently falling through to a guess would make the
            # admin's mapping look applied while another column supplied the
            # value.
            return pick(col)
        return pick(*fallback)

    return LinkedAppRecord(
        external_app_id=chosen("id", "external_app_id", "id", "app_id", "config_id"),
        name=chosen("name", "name", "app_name", "title"),
        description=chosen("description", "description", "desc"),
        external_url=_safe_url(chosen("url", "external_url", "url", "app_url", "deployment_url")),
    )
