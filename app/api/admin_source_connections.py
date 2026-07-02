"""Admin REST API for source connections (named multi-project data-source connections).

Surface (all gated by ``Depends(require_admin)``):

  GET    /api/admin/source-connections              — list (?source_type=)
  POST   /api/admin/source-connections              — create; 409 on duplicate name
  GET    /api/admin/source-connections/{id}         — detail; 404 if missing
  PUT    /api/admin/source-connections/{id}         — update config / token_env; 404 if missing
  DELETE /api/admin/source-connections/{id}         — delete; 404 if missing
  PUT    /api/admin/source-connections/{id}/secret  — store vault secret; 409 if AGNES_VAULT_KEY missing
  DELETE /api/admin/source-connections/{id}/secret  — clear vault secret
  POST   /api/admin/source-connections/{id}/test    — verify connectivity; timeout 10s
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.access import require_admin
from app.secrets_vault import VaultKeyNotConfiguredError
from src.repositories import connection_secrets_repo, source_connections_repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/source-connections", tags=["admin"])


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------


class CreateConnectionBody(BaseModel):
    name: str
    source_type: str
    config: Dict[str, Any]
    token_env: Optional[str] = None
    is_default: bool = False


class UpdateConnectionBody(BaseModel):
    config: Optional[Dict[str, Any]] = None
    token_env: Optional[str] = None


class SecretBody(BaseModel):
    value: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("")
async def list_connections(
    source_type: Optional[str] = None,
    _user: dict = Depends(require_admin),
):
    """List all named source connections, optionally filtered by source_type."""
    return source_connections_repo().list(source_type=source_type)


@router.post("", status_code=201)
async def create_connection(
    body: CreateConnectionBody,
    _user: dict = Depends(require_admin),
):
    """Create a named source connection. 409 if the name is already taken."""
    repo = source_connections_repo()
    if repo.get_by_name(body.name) is not None:
        raise HTTPException(status_code=409, detail="connection_name_exists")
    conn_id = str(uuid4())
    repo.create(
        id=conn_id,
        name=body.name,
        source_type=body.source_type,
        config=body.config,
        token_env=body.token_env,
        is_default=body.is_default,
        created_by=_user.get("id"),
    )
    row = repo.get(conn_id)
    return row


@router.get("/{connection_id}")
async def get_connection(
    connection_id: str,
    _user: dict = Depends(require_admin),
):
    """Return a single source connection. 404 if not found."""
    row = source_connections_repo().get(connection_id)
    if row is None:
        raise HTTPException(status_code=404, detail="connection_not_found")
    return row


@router.put("/{connection_id}")
async def update_connection(
    connection_id: str,
    body: UpdateConnectionBody,
    _user: dict = Depends(require_admin),
):
    """Update config and/or token_env of an existing connection. 404 if missing."""
    repo = source_connections_repo()
    if repo.get(connection_id) is None:
        raise HTTPException(status_code=404, detail="connection_not_found")
    repo.update(connection_id, config=body.config, token_env=body.token_env)
    return repo.get(connection_id)


@router.delete("/{connection_id}", status_code=204)
async def delete_connection(
    connection_id: str,
    _user: dict = Depends(require_admin),
):
    """Delete a source connection. 404 if not found."""
    repo = source_connections_repo()
    if repo.get(connection_id) is None:
        raise HTTPException(status_code=404, detail="connection_not_found")
    repo.delete(connection_id)
    # Best-effort: clear any vault secret — ignore if none exists.
    try:
        connection_secrets_repo().delete(connection_id)
    except Exception:
        logger.debug("no vault secret for connection %s (expected)", connection_id)


@router.put("/{connection_id}/secret", status_code=204)
async def set_connection_secret(
    connection_id: str,
    body: SecretBody,
    _user: dict = Depends(require_admin),
):
    """Store (or rotate) the vault secret for a connection token.

    409 if AGNES_VAULT_KEY is not configured on the server.
    """
    if source_connections_repo().get(connection_id) is None:
        raise HTTPException(status_code=404, detail="connection_not_found")
    if not body.value:
        raise HTTPException(status_code=400, detail="secret value required")
    try:
        connection_secrets_repo().upsert(connection_id, body.value)
    except VaultKeyNotConfiguredError as exc:
        raise HTTPException(
            status_code=409,
            detail="vault_key_not_configured: set AGNES_VAULT_KEY on the server before storing secrets",
        ) from exc


@router.delete("/{connection_id}/secret", status_code=204)
async def delete_connection_secret(
    connection_id: str,
    _user: dict = Depends(require_admin),
):
    """Clear the vault secret for a connection (idempotent)."""
    if source_connections_repo().get(connection_id) is None:
        raise HTTPException(status_code=404, detail="connection_not_found")
    connection_secrets_repo().delete(connection_id)


@router.post("/{connection_id}/test")
async def test_connection(
    connection_id: str,
    _user: dict = Depends(require_admin),
):
    """Verify connectivity for the connection.

    Resolves the stack URL and token from the connection row (token_env →
    environment lookup, or vault secret), then calls
    ``GET {stack_url}/v2/storage?exclude=components`` with a 10-second
    timeout.

    Returns ``{ok: true, project_name: "…"}`` on success or
    ``{ok: false, error: "…"}`` on failure.
    """
    row = source_connections_repo().get(connection_id)
    if row is None:
        raise HTTPException(status_code=404, detail="connection_not_found")

    config = row.get("config") or {}
    stack_url = (config.get("stack_url") or "").rstrip("/")
    if not stack_url:
        return {"ok": False, "error": "no stack_url in connection config"}

    # Resolve token: vault secret first, then token_env env-var fallback.
    token: Optional[str] = None
    try:
        secrets = connection_secrets_repo()
        if secrets.has(connection_id):
            token = secrets.get(connection_id)
    except Exception as exc:
        logger.debug("vault lookup failed for %s: %s", connection_id, exc)

    if not token:
        token_env = row.get("token_env") or ""
        if token_env:
            token = os.environ.get(token_env, "")

    if not token:
        return {"ok": False, "error": "no token available (vault empty, token_env unset)"}

    url = f"{stack_url}/v2/storage?exclude=components"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers={"X-StorageApi-Token": token})
        if resp.status_code == 200:
            data = resp.json()
            project_name = data.get("owner", {}).get("name") or data.get("name") or ""
            return {"ok": True, "project_name": project_name}
        return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}
