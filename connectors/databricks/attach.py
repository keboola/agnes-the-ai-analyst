"""Unity Catalog ATTACH — letting DuckDB resolve Databricks tables itself.

Phase 2 ships an analyst's whole statement to the SQL warehouse. That is the
fast path and the only one that can evaluate ``MEASURE()``, but it is
all-or-nothing: a statement that also touches a local parquet has no engine
that can see both halves, so ``/api/query`` refuses it.

This module is the other option. DuckDB's ``uc_catalog`` community extension
attaches a Unity Catalog catalog as a DuckDB catalog, reading Delta tables
through the ``delta`` extension. Once attached, a Databricks table is just
another relation: joins against Keboola parquets work, and nothing leaves the
server except the Delta file reads.

Opt-in, and deliberately so
---------------------------
Off unless ``data_source.databricks.attach_enabled`` is true, for three
reasons that are worth stating rather than burying:

1. ``uc_catalog`` and ``delta`` are community extensions installed from the
   DuckDB community repository at rebuild time — a supply-chain surface the
   operator should choose, which is why both had to be added to
   ``src/orchestrator_security.py``'s allowlist to work at all.
2. The ATTACH sends a live workspace PAT to the endpoint in the
   ``_remote_attach`` row. The host allowlist
   (``AGNES_REMOTE_ATTACH_HOST_ALLOWLIST``) governs where that may go, and it
   is enforced here exactly as it is for every other credentialed ATTACH.
3. Query pushdown through the extension is far weaker than the warehouse's:
   predicates that Databricks SQL would resolve in seconds become Delta file
   scans. The warehouse path stays the default for all-Databricks statements
   precisely because of this; ATTACH earns its keep on cross-source joins.

The ``url`` encoding
--------------------
``_remote_attach`` has four fixed columns (``alias``, ``extension``, ``url``,
``token_env``) and the Unity Catalog ATTACH needs two values: the workspace
endpoint and the catalog name. They are packed as
``https://<workspace-host>/<catalog>`` rather than adding a fifth column,
because that shape keeps ``is_attach_host_allowed(url)`` — the control that
decides where the PAT may be sent — reading the real host with no special
casing. A new column would have meant teaching every connector, the
orchestrator, and the read path about a field only one connector sets.
"""

from __future__ import annotations

import logging
import re
from typing import Tuple
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: DuckDB catalog alias the Databricks extract attaches under, and the name a
#: master view's body refers to (``dbx."<schema>"."<table>"``).
UC_ALIAS = "dbx"

#: The DuckDB community extension that speaks Unity Catalog. It pulls table
#: data through ``delta``; both are allowlisted in `src/orchestrator_security`.
#:
#: ``uc_catalog`` is the name used in ``INSTALL``/``LOAD``/``ATTACH (TYPE …)``;
#: DuckDB resolves it to the published artifact ``unity_catalog`` when fetching
#: from the community repository. Only ``uc_catalog`` is allowlisted, because
#: that is the name a connector writes into ``_remote_attach`` — the artifact
#: name is DuckDB's own internal resolution and never crosses the trust
#: boundary the allowlist guards.
UC_EXTENSION = "uc_catalog"

#: Env var holding the workspace PAT. Must also be in the orchestrator's
#: token-env allowlist or the ATTACH is refused before the secret is read.
UC_TOKEN_ENV = "DATABRICKS_TOKEN"

_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_]+$")


def build_remote_attach_url(host: str, catalog: str) -> str:
    """Pack ``(workspace host, catalog)`` into the single ``url`` column.

    Raises ``ValueError`` on a catalog name outside the safe alphabet: the
    value is spliced into an ``ATTACH '<…>'`` literal, and while the literal is
    escaped, a catalog containing a slash would also break the round-trip
    through :func:`parse_remote_attach_url`.
    """
    from connectors.databricks.client import validate_workspace_host

    normalized = validate_workspace_host(host)
    catalog = (catalog or "").strip()
    if not _SAFE_SEGMENT_RE.match(catalog):
        raise ValueError(f"unsafe Unity Catalog catalog name: {catalog!r}")
    return f"{normalized}/{catalog}"


def parse_remote_attach_url(url: str) -> Tuple[str, str]:
    """Unpack ``https://<host>/<catalog>`` into ``(endpoint, catalog)``.

    ``endpoint`` keeps the scheme (that is what the extension wants);
    ``catalog`` is the single path segment. Raises ``ValueError`` when the URL
    is not in that shape — a malformed row must fail loudly rather than ATTACH
    something unintended.
    """
    parts = urlsplit((url or "").strip())
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError(f"databricks _remote_attach url must be https://<host>/<catalog>, got {url!r}")
    catalog = parts.path.strip("/")
    if not _SAFE_SEGMENT_RE.match(catalog):
        raise ValueError(f"databricks _remote_attach url has no usable catalog segment: {url!r}")
    netloc = parts.hostname if parts.port is None else f"{parts.hostname}:{parts.port}"
    return f"https://{netloc}", catalog


def attach_unity_catalog(conn, *, alias: str, url: str, token: str) -> None:
    """``CREATE SECRET`` + ``ATTACH`` a Unity Catalog catalog onto ``conn``.

    Split from the orchestrator's ATTACH loop so the rebuild path and the
    read-only query path run byte-identical SQL — the BigQuery branch above it
    proved how easily two copies of "the same" ATTACH drift (one of them spent
    a release without the session settings the other applied).

    The token reaches DuckDB through a secret rather than an ``ATTACH``
    parameter so it does not appear in the ATTACH statement text that DuckDB
    keeps in its catalog and error messages.
    """
    from src.orchestrator_security import escape_sql_string_literal

    endpoint, catalog = parse_remote_attach_url(url)
    secret_name = f"uc_secret_{alias}"
    conn.execute(
        f"CREATE OR REPLACE SECRET {secret_name} ("
        f"TYPE UC, TOKEN '{escape_sql_string_literal(token)}', "
        f"ENDPOINT '{escape_sql_string_literal(endpoint)}')"
    )
    conn.execute(f"ATTACH '{escape_sql_string_literal(catalog)}' AS {alias} (TYPE {UC_EXTENSION}, READ_ONLY)")
    logger.info("Attached Unity Catalog catalog %r as %s via %s", catalog, alias, UC_EXTENSION)


def attach_enabled() -> bool:
    """Whether this instance opted into the Unity Catalog ATTACH path.

    Default False. See the module docstring for why this is a choice an
    operator makes rather than a capability that appears on upgrade.
    """
    from app.instance_config import get_value

    return bool(get_value("data_source", "databricks", "attach_enabled", default=False))
