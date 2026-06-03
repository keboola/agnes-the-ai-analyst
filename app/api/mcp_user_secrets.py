"""User-facing REST for per-user MCP source secrets (RFC #461 §4 phase B).

Each analyst stores their own credential for upstream MCP sources whose
``scope='per_user'`` (Notion / Slack / Linear OAuth tokens). When the
caller invokes a passthrough tool on such a source, the server forwards
under the analyst's identity rather than a shared server-wide secret.

Endpoints (all under ``/api/mcp/sources/{source_id}/my-secret``):

* ``PUT``    — store / rotate the caller's secret for this source
* ``DELETE`` — drop the caller's secret (call falls back to shared)
* ``GET``    — booleans only — ``{"has_secret": bool}``. We never
               return the cleartext, even to its owner; rotation is
               write-only.

For ``scope='shared'`` sources we still accept the PUT (operators may
flip scope later) but warn the caller that the value won't be used
until scope flips.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.dependencies import get_current_user
from app.secrets_vault import VaultKeyNotConfiguredError
from src.repositories import mcp_sources_repo, per_user_secrets_repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mcp/sources", tags=["mcp-user-secrets"])


class MySecretBody(BaseModel):
    value: str


class HasSecretResponse(BaseModel):
    has_secret: bool
    source_scope: str  # 'shared' | 'per_user'


@router.put("/{source_id}/my-secret", status_code=204)
async def set_my_secret(
    source_id: str,
    body: MySecretBody,
    user: dict = Depends(get_current_user),
):
    """Store (or rotate) the caller's per-user secret for this source.

    The value is Fernet-encrypted at rest in ``mcp_user_secrets`` using
    the same vault key as the shared secrets table; if you wonder where
    your token lives, it's in there. Cleartext is never returned.
    """
    if not body.value:
        raise HTTPException(status_code=400, detail="secret value required")
    if not mcp_sources_repo().get(source_id):
        raise HTTPException(status_code=404, detail="mcp_source_not_found")
    try:
        per_user_secrets_repo().upsert(source_id, user["id"], body.value)
    except VaultKeyNotConfiguredError as exc:
        raise HTTPException(
            status_code=409,
            detail="vault_key_not_configured: set AGNES_VAULT_KEY on the server before storing secrets",
        ) from exc


@router.delete("/{source_id}/my-secret", status_code=204)
async def delete_my_secret(
    source_id: str,
    user: dict = Depends(get_current_user),
):
    """Drop the caller's per-user secret. For ``scope='per_user'``
    sources the next call falls through to the shared vault path."""
    if not mcp_sources_repo().get(source_id):
        raise HTTPException(status_code=404, detail="mcp_source_not_found")
    per_user_secrets_repo().delete(source_id, user["id"])


@router.get("/{source_id}/my-secret", response_model=HasSecretResponse)
async def get_my_secret_status(
    source_id: str,
    user: dict = Depends(get_current_user),
) -> HasSecretResponse:
    """Return ``has_secret: bool`` for the caller + the source's scope so
    a UI can show "Connect your <source>" or "Connected"."""
    source = mcp_sources_repo().get(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="mcp_source_not_found")
    return HasSecretResponse(
        has_secret=per_user_secrets_repo().has(source_id, user["id"]),
        source_scope=(source.get("scope") or "shared"),
    )
