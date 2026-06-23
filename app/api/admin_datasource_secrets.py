"""Admin REST API for datasource credentials (vault-backed).

  - GET    /api/admin/datasource-secrets             — presence/source status (no values)
  - PUT    /api/admin/datasource-secrets/{name}      — set / rotate (write-only)
  - DELETE /api/admin/datasource-secrets/{name}      — clear vault row
  - POST   /api/admin/validate-gws-credentials       — validate GWS client_id format

All gated by ``require_admin``. Secret values are never returned by any
endpoint and never placed in audit records (params are empty, mirroring
the Slack secrets endpoints).
"""

from __future__ import annotations

import logging
import os
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.access import require_admin
from app.datasource_secrets import DATA_SOURCE_SECRET_NAMES
from app.secrets_vault import VaultKeyNotConfiguredError
from src.repositories import audit_repo, system_secrets_repo

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin-datasource-secrets"])

_GWS_CLIENT_ID_RE = re.compile(r"^\d+-[a-z0-9]+\.apps\.googleusercontent\.com$")


class DatasourceSecretBody(BaseModel):
    value: str


def _audit(actor_id: str, action: str, resource: str) -> None:
    try:
        audit_repo().log(user_id=actor_id, action=action, resource=resource, params={})
    except Exception:
        logger.warning("audit log failed for %s/%s", action, resource)


@router.get("/datasource-secrets")
async def list_datasource_secrets(user: dict = Depends(require_admin)):
    """Presence/source status for known datasource secrets. Never leaks values."""
    repo = system_secrets_repo()
    out = []
    for name in DATA_SOURCE_SECRET_NAMES:
        if os.environ.get(name):
            source, has_value = "env", True
        elif repo.has(name):
            source, has_value = "vault", True
        else:
            source, has_value = "unset", False
        out.append({"name": name, "source": source, "has_value": has_value})
    return {"secrets": out}


@router.put("/datasource-secrets/{name}", status_code=204)
async def set_datasource_secret(
    name: str,
    body: DatasourceSecretBody,
    user: dict = Depends(require_admin),
):
    """Store (or rotate) the vault secret for ``name``. Write-only."""
    if name not in DATA_SOURCE_SECRET_NAMES:
        raise HTTPException(status_code=400, detail="unknown_datasource_secret")
    if not body.value.strip():
        raise HTTPException(status_code=400, detail="secret value required")
    try:
        system_secrets_repo().upsert(name, body.value.strip())
    except VaultKeyNotConfiguredError as exc:
        raise HTTPException(
            status_code=409,
            detail="vault_key_not_configured: set AGNES_VAULT_KEY on the server before storing secrets",
        ) from exc
    _audit(user["id"], "datasource.secret.set", f"datasource_secret:{name}")


@router.delete("/datasource-secrets/{name}", status_code=204)
async def delete_datasource_secret(
    name: str,
    user: dict = Depends(require_admin),
):
    """Drop the vault row for ``name``. Resolution falls back to env / unset."""
    if name not in DATA_SOURCE_SECRET_NAMES:
        raise HTTPException(status_code=400, detail="unknown_datasource_secret")
    system_secrets_repo().delete(name)
    _audit(user["id"], "datasource.secret.clear", f"datasource_secret:{name}")


@router.post("/validate-gws-credentials")
async def validate_gws_credentials(user: dict = Depends(require_admin)):
    """Validate the currently configured GWS OAuth client credentials.

    Checks format only — does not make any network call to Google. A
    well-formed client_id proves the GCP project number is present, which
    is a prerequisite for the gws CLI's JSON schema.
    """
    from app.instance_config import get_gws_oauth_credentials

    creds = get_gws_oauth_credentials()
    issues: list[str] = []

    if not creds["configured"]:
        if not creds["client_id"]:
            issues.append("AGNES_GWS_CLIENT_ID is not set")
        if not creds["client_secret"]:
            issues.append("AGNES_GWS_CLIENT_SECRET is not set")

    if creds["client_id"] and not _GWS_CLIENT_ID_RE.match(creds["client_id"]):
        issues.append(
            "Client ID format is invalid — expected '<numeric-project-number>-<random>.apps.googleusercontent.com'"
        )

    return {"ok": len(issues) == 0, "configured": creds["configured"], "issues": issues}
