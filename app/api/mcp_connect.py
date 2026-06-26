"""MCP headless-connect token endpoint.

Headless AI editors (Cursor, GitHub Copilot, CLI clients) cannot complete
the OAuth browser flow required by /api/mcp. This endpoint lets an
authenticated user obtain a fresh PAT pre-named "mcp-headless" so they
can paste ready-made MCP config snippets into their editor.

POST /api/mcp-connect/token
  - Requires an interactive session (not a PAT itself — blocks PAT-chains).
  - Revokes any existing non-revoked "mcp-headless" PAT for the user.
  - Creates a new PAT with a 365-day TTL.
  - Returns {"token": "<raw JWT>", "base_url": "<instance base URL>"}.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth.dependencies import require_session_token
from app.auth.jwt import create_access_token
from app.instance_config import get_public_url
from src.repositories import access_token_repo, audit_repo

router = APIRouter(prefix="/api/mcp-connect", tags=["mcp-connect"])

_PAT_NAME = "mcp-headless"
_TTL_DAYS = 365


class _TokenResponse(BaseModel):
    token: str
    base_url: str
    expires_at: Optional[str]


@router.post("/token", response_model=_TokenResponse)
async def create_mcp_headless_token(
    user: dict = Depends(require_session_token),
) -> _TokenResponse:
    """Create (or replace) a PAT for headless MCP editor access.

    If the user already has a non-revoked PAT named ``mcp-headless``,
    it is revoked first so each call to this page always produces
    exactly one active token. The raw JWT is returned once — it is not
    stored and cannot be retrieved again.
    """
    repo = access_token_repo()
    alog = audit_repo()
    user_id: str = user["id"]

    # Revoke any existing non-revoked "mcp-headless" tokens for this user.
    existing = repo.list_for_user(user_id, include_revoked=False)
    for row in existing:
        if row.get("name") == _PAT_NAME and not row.get("revoked_at"):
            repo.revoke(row["id"])
            try:
                alog.log(
                    user_id=user_id,
                    action="token.revoke",
                    resource=f"token:{row['id']}",
                    params={"name": _PAT_NAME, "reason": "replaced"},
                )
            except Exception:
                pass

    # Create a new PAT with a 1-year TTL.
    expires_delta = timedelta(days=_TTL_DAYS)
    expires_at = datetime.now(timezone.utc) + expires_delta
    token_id = str(uuid.uuid4())
    jwt_token = create_access_token(
        user_id=user_id,
        email=user["email"],
        token_id=token_id,
        typ="pat",
        expires_delta=expires_delta,
        omit_exp=False,
        extra_claims={"scope": "mcp-headless"},
    )
    prefix = token_id.replace("-", "")[:8]
    token_hash = hashlib.sha256(jwt_token.encode()).hexdigest()
    repo.create(
        id=token_id,
        user_id=user_id,
        name=_PAT_NAME,
        token_hash=token_hash,
        prefix=prefix,
        expires_at=expires_at,
    )
    try:
        alog.log(
            user_id=user_id,
            action="token.create",
            resource=f"token:{token_id}",
            params={"name": _PAT_NAME, "ttl_days": _TTL_DAYS},
        )
    except Exception:
        pass

    base_url = get_public_url() or ""
    return _TokenResponse(
        token=jwt_token,
        base_url=base_url,
        expires_at=str(expires_at),
    )
