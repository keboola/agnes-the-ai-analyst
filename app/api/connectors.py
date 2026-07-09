"""Connector manifest + per-tenant params — HTTP surface for the
seed-driven connector flow.

Two endpoints, two responsibilities:

* ``GET /api/connectors/manifest`` — returns the validated connector
  manifest (display_name, short_summary, vendor_url, etc.) sourced from
  the seed (IWT clone first, bundled snapshot fallback). Consumers: the
  install-prompt renderer in ``app/web/setup_instructions.py`` and any
  admin-UI surface that wants to list available connectors. Auth:
  authenticated user (same scope as ``/home``).

* ``GET /api/connectors/params`` — returns operator-provisioned per-tenant
  runtime params (Atlassian base URL, GWS OAuth client_id, etc.) sourced
  from the ``connectors:`` overlay in ``instance.yaml``. Written by
  ``agnes init`` into ``<workspace>/.claude/agnes/.env`` for seed skills
  to read at runtime. Secrets: the server-resolved GWS fallback only ever
  emits an ``*_ENV`` indirection pointer, but the overlay passes operator
  keys through verbatim — including secret values if the operator ships
  them that way (full nuance in the endpoint docstring below).

Cache invalidation for ``/manifest``: the underlying
``src.connectors_manifest.load_manifest()`` is cached by
``(source_signature, file_hash)``. Admin "Sync now" advances the commit
SHA → cache miss → re-scan on next request.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth.dependencies import get_current_user
from src.connectors_manifest import ConnectorEntry, load_manifest
from src.initial_workspace import is_configured

logger = logging.getLogger(__name__)

router = APIRouter(tags=["connectors"])


SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class ConnectorMeta(BaseModel):
    """One connector tile's metadata as it appears in the manifest API
    response. Field names match the seed's frontmatter shape so the
    contract documented in ``docs/seed-repo-contract.md`` lines up with
    the JSON callers see.
    """

    slug: str
    display_name: str
    short_summary: str
    estimated_minutes: int
    vendor_url: Optional[str] = None
    requires_oauth_app: bool = False


class ConnectorsManifestResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    connectors: list[ConnectorMeta] = []
    # ``"iwt"`` when the operator-configured Initial Workspace Template
    # provided the SKILL.md files; ``"bundled"`` when the Agnes wheel's
    # snapshot fallback rendered. ``"none"`` is never returned today —
    # bundled always exists in a healthy install — but the literal is
    # reserved for the case where a deployer strips the bundle (and we
    # want the API to say so loudly).
    source: str = "bundled"


class ConnectorsParamsResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    # Map of connector slug → flat dict of runtime params (string keys,
    # string values). Empty dict when the operator hasn't overlaid
    # anything; callers (``agnes init``) treat that as "use defaults".
    params: dict[str, dict[str, str]] = {}
    # Globals applied to every connector (e.g. ``AGNES_INSTANCE_BRAND``).
    globals: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def _entry_to_meta(entry: ConnectorEntry) -> ConnectorMeta:
    return ConnectorMeta(
        slug=entry.slug,
        display_name=entry.display_name,
        short_summary=entry.short_summary,
        estimated_minutes=entry.estimated_minutes,
        vendor_url=entry.vendor_url,
        requires_oauth_app=entry.requires_oauth_app,
    )


@router.get(
    "/api/connectors/manifest",
    response_model=ConnectorsManifestResponse,
)
async def get_manifest(
    user: dict = Depends(get_current_user),  # noqa: ARG001 — auth gate only
):
    """Return the seed-derived connector manifest.

    Always emits a 200 response; an empty ``connectors`` list means the
    seed had no ``connector-*/SKILL.md`` files (or they all failed
    validation — check ``audit_log`` for the warnings). Callers MUST
    handle the empty case gracefully (the install prompt simply omits
    the tile section).
    """
    entries = load_manifest()
    source = "iwt" if is_configured() else "bundled"
    return ConnectorsManifestResponse(
        connectors=[_entry_to_meta(e) for e in entries],
        source=source,
    )


@router.get(
    "/api/connectors/params",
    response_model=ConnectorsParamsResponse,
)
async def get_params(
    user: dict = Depends(get_current_user),  # noqa: ARG001 — auth gate only
):
    """Return per-tenant runtime params keyed by connector slug, plus
    instance-wide ``globals`` (e.g. ``AGNES_INSTANCE_BRAND``).

    Source: the ``connectors:`` section of ``instance.yaml`` overlay.
    Schema (documented in ``config/instance.yaml.example``):

        connectors:
          globals:
            AGNES_INSTANCE_BRAND: Acme Analytics
          connector-atlassian:
            ATLASSIAN_BASE_URL: https://acme.atlassian.net
          connector-gws:
            AGNES_GWS_CLIENT_ID: "..."
            AGNES_GWS_PROJECT_ID: "..."
            AGNES_GWS_CLIENT_SECRET_ENV: AGNES_GWS_CLIENT_SECRET

    All values are strings (YAML scalars coerced via ``str()``).

    Secrets contract — two different guarantees, don't conflate them:

    - The **server-resolved GWS fallback** (below) never emits a secret
      VALUE — only the ``*_ENV`` pointer naming the env var that holds it.
    - The **overlay passes through verbatim**: whatever keys the operator
      puts under ``connectors:`` in instance.yaml reach the analyst's
      ``.env`` as-is — including ``AGNES_GWS_CLIENT_SECRET: <value>`` if
      the operator chooses to ship the value that way. The endpoint does
      not police overlay content; seed skills must therefore check the
      ``.env`` file for the secret value first and fall back to the
      ``*_ENV`` shell-env pointer.

    Server-resolved GWS fallback: operators can provision the shared
    Google Workspace OAuth client outside the overlay — server env vars
    (``AGNES_GWS_CLIENT_ID``/``AGNES_GWS_CLIENT_SECRET``), the admin
    vault, or ``instance.gws.*`` in instance.yaml (resolution order in
    :func:`app.instance_config.get_gws_oauth_credentials`). When that
    resolves to a configured client, the equivalent ``connector-gws``
    params are merged into the response so `agnes init` writes them into
    the analyst's ``.env`` and the connector-gws seed skill takes its
    fast operator-provisioned branch. Overlay keys win — the overlay
    stays the per-connector source of truth; the merge only backfills.

    In the fallback the client_secret VALUE deliberately does NOT ride
    along, keeping the fallback's no-secrets guarantee intact even though
    a GWS Desktop-app client secret is closer to an app identifier than a
    credential (the analyst-side skill ultimately writes it into
    client_secret.json). Analysts get the ``*_ENV`` pointer; when neither
    the ``.env`` file (overlay-shipped value) nor their shell env yields
    the value, the skill asks the operator for it — one string, instead
    of the full manual GCP-project walkthrough.
    """
    from app.api.admin import _load_current_instance_yaml

    cfg = _load_current_instance_yaml()
    section = cfg.get("connectors") if isinstance(cfg, dict) else None
    if not isinstance(section, dict):
        # No overlay is NOT an early return: the server-resolved GWS
        # fallback below must still get a chance to populate params.
        section = {}

    globals_block: dict[str, str] = {}
    raw_globals = section.get("globals")
    if isinstance(raw_globals, dict):
        for k, v in raw_globals.items():
            if v is None:
                continue
            globals_block[str(k)] = str(v)

    per_connector: dict[str, dict[str, str]] = {}
    for key, value in section.items():
        if key == "globals":
            continue
        if not isinstance(value, dict):
            continue
        flat: dict[str, str] = {}
        for k, v in value.items():
            if v is None:
                continue
            flat[str(k)] = str(v)
        if flat:
            per_connector[str(key)] = flat

    # Allowlist filter: only emit params for slugs the seed manifest
    # actually advertises. The overlay accepts arbitrary keys (operator
    # types `connector-atlasian:` instead of `connector-atlassian:`),
    # and unfiltered we'd write that typo into the analyst's `.env`,
    # silently breaking the real connector while polluting their env.
    # The manifest is the source of truth for "what connectors exist";
    # anything outside that set is ignored AND logged at WARNING so the
    # operator notices.
    known_slugs = {entry.slug for entry in load_manifest()}
    unknown = sorted(set(per_connector) - known_slugs)
    if unknown:
        logger.warning(
            "connectors.params: ignoring unknown slugs in instance.yaml (not in manifest): %s",
            unknown,
        )
    filtered = {slug: params for slug, params in per_connector.items() if slug in known_slugs}

    # Server-resolved GWS fallback (see docstring). Gated on the manifest
    # advertising connector-gws so a fork that strips the connector from
    # its seed doesn't get phantom params in every analyst's .env. Runs
    # AFTER the allowlist filter on purpose — the injected slug is
    # manifest-known by construction, and injecting earlier would make
    # the unknown-slug warning above misattribute it to instance.yaml.
    if "connector-gws" in known_slugs:
        from app.instance_config import get_gws_oauth_credentials

        creds = get_gws_oauth_credentials()
        if creds.get("configured"):
            server_gws = {
                "AGNES_GWS_CLIENT_ID": creds["client_id"],
                # Pointer to the env var holding the secret — never the
                # secret value itself (endpoint contract above).
                "AGNES_GWS_CLIENT_SECRET_ENV": "AGNES_GWS_CLIENT_SECRET",
            }
            if creds.get("project_id"):
                server_gws["AGNES_GWS_PROJECT_ID"] = creds["project_id"]
            filtered["connector-gws"] = {
                **server_gws,
                **filtered.get("connector-gws", {}),
            }

    return ConnectorsParamsResponse(
        params=filtered,
        globals=globals_block,
    )
