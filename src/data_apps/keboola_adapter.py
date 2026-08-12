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
        external_url=_safe_url(pick("external_url", "url", "app_url", "deployment_url")),
    )


# ---------------------------------------------------------------------------
# Storage/Data-Science API ingest — the path that actually works today
# ---------------------------------------------------------------------------
#
# The MCP lister (`map_row` above, fed by a materialized `keboola_data_apps`
# table) stays for sources that emit JSON rows. It does NOT work against the
# Keboola MCP server: `get_data_apps` returns a compact human-readable text
# block (`data_apps[0]: links[1]{...}`) rather than the list-of-dicts the
# materialize path requires, and it ships only `TextContent` blocks, so there
# is no `structuredContent` to fall back to. Verified against a live server
# (keboola-mcp-server 1.74.6): the materialize run fails with "did not return
# parseable JSON".
#
# So this path reads the two stable REST contracts directly. Neither alone is
# enough, which is why they are joined here rather than one being picked:
#
#   * `GET data-science.<stack>/apps`  — the deployment truth: numeric `id`,
#     the public `url`, `state`, and the `configId` that ties it to storage.
#     It carries NO usable name (`name` is null on every row observed).
#   * `GET connection.<stack>/v2/storage/components/keboola.data-apps/configs`
#     — the naming truth: `name` and `description` per config id. It carries
#     no URL: the config's `parameters.dataApp` holds `slug`/`type`/`git`, and
#     the public address is assigned at deploy time, not stored here.
#
# Both are token-scoped to the project the connection is bound to, so this
# inherits that binding rather than re-deriving it.

_DATA_APPS_COMPONENT = "keboola.data-apps"


def _data_science_base(stack_url: str) -> str:
    """`https://connection.<stack>` → `https://data-science.<stack>`.

    The Data Science service is a sibling host of the Storage endpoint on
    every Keboola stack, and the connection stores only the latter. The
    Keboola MCP server derives it the same way.
    """
    base = (stack_url or "").strip().rstrip("/")
    if not base:
        return ""
    return base.replace("://connection.", "://data-science.", 1)


def records_from_apis(apps: Any, configs: Any) -> tuple[list[LinkedAppRecord], list[str]]:
    """Join a Data-Science `/apps` payload with storage component configs.

    Returns ``(records, keep_external_ids)`` — the second list is app ids seen
    upstream that could not be turned into a record (no usable URL), which
    `linked_projection.project` treats as present-but-unlinkable so a metadata gap
    never reads as a deletion.

    Rows whose ``componentId`` is not ``keboola.data-apps`` are dropped: the
    same endpoint also returns ``keboola.sandboxes`` entries (Snowflake
    workspaces, whose ``url`` is a warehouse host) and linking those as "apps"
    would put a database endpoint behind an "Open" button.
    """
    names: Dict[str, tuple[str, str]] = {}
    for cfg in configs or []:
        if not isinstance(cfg, dict) or cfg.get("isDeleted"):
            continue
        names[str(cfg.get("id"))] = (str(cfg.get("name") or ""), str(cfg.get("description") or ""))

    records: list[LinkedAppRecord] = []
    keep: list[str] = []
    for app in apps or []:
        if not isinstance(app, dict) or str(app.get("componentId")) != _DATA_APPS_COMPONENT:
            continue
        app_id = str(app.get("id") or "")
        if not app_id:
            continue
        name, description = names.get(str(app.get("configId")), ("", ""))
        url = _safe_url(str(app.get("url") or ""))
        if not url:
            # Present upstream, not linkable — exempt from the prune.
            keep.append(app_id)
            continue
        records.append(
            LinkedAppRecord(
                external_app_id=app_id,
                # An app whose config was deleted out from under it still has a
                # deployment; name it by id rather than showing a blank row.
                name=name or f"Keboola app {app_id}",
                description=description,
                external_url=url,
            )
        )
    return records, keep


def fetch_records(stack_url: str, token: str, *, timeout: float = 30.0) -> tuple[list[LinkedAppRecord], list[str]]:
    """Fetch both payloads for one connection and join them.

    Network errors propagate — the caller decides whether a failed sync is
    worth surfacing. What must NOT happen is an empty list standing in for a
    failure: `linked_projection.project` would read that as "everything is gone",
    and its own empty-result valve is a backstop, not a licence to swallow
    exceptions here.
    """
    import httpx

    headers = {"X-StorageApi-Token": token}
    ds = _data_science_base(stack_url)
    storage = (stack_url or "").strip().rstrip("/")
    if not ds or not storage:
        raise ValueError("keboola linked apps: connection has no stack_url")

    with httpx.Client(timeout=timeout, headers=headers) as client:
        apps = client.get(f"{ds}/apps", params={"limit": 100, "offset": 0})
        apps.raise_for_status()
        configs = client.get(f"{storage}/v2/storage/components/{_DATA_APPS_COMPONENT}/configs")
        configs.raise_for_status()
    return records_from_apis(apps.json(), configs.json())
