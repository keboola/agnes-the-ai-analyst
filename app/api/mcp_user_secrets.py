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
    updated_at: str | None = None  # ISO-8601 of last set; None when not connected


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
        updated_at=per_user_secrets_repo().get_updated_at(source_id, user["id"]),
    )


# Explicit positive per-minute cap for the connectivity test. check_rate_limit
# treats None/<=0 as *disabled*, and mcp_sources has no rate_limit_pm column, so
# this must be a literal or the gate silently no-ops. Each test opens a fresh
# upstream connection (a subprocess for stdio transports), so keep it low.
_TEST_CONNECTION_RATE_LIMIT_PM = 6


def _redact_then_truncate(text: str, token: str, limit: int = 300) -> str:
    """Redact the caller's own token from the FULL string first, then truncate.
    Order matters: truncating first could split the token across the boundary so
    the substring match misses it and a fragment leaks."""
    if token:
        text = text.replace(token, "***")
    return text[:limit]


class TestResult(BaseModel):
    ok: bool
    tool_count: int | None = None
    message: str


@router.post("/{source_id}/my-secret/test", response_model=TestResult)
async def test_my_secret(source_id: str, user: dict = Depends(get_current_user)) -> TestResult:
    """Verify the caller's own stored credential works against the upstream.

    Gated in order, all before any upstream call: unknown source → 404; a
    non-per_user (shared) source → 400 (its introspection would run under the
    operator's shared credential — nothing personal to test); no grant on the
    source → 403; over the rate limit → 429; no personal credential → 403 with
    the connect remedy. Only then does it introspect under the caller's token.
    """
    from app.api.mcp_passthrough import _visible_passthrough_tools
    from app.api.mcp_policy import (
        PerUserCredentialMissing,
        RateLimited,
        check_rate_limit,
        enforce_per_user_credential,
    )
    from connectors.mcp.client import list_tools_async

    source = mcp_sources_repo().get(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="mcp_source_not_found")
    if (source.get("scope") or "shared").lower() != "per_user":
        raise HTTPException(status_code=400, detail="source_scope_not_per_user")
    # Grant check via the same helper the connect page uses — no second
    # hand-rolled intersection (there is no source-level grant method).
    granted_source_ids = {t["source_id"] for t in _visible_passthrough_tools(user)}
    if source_id not in granted_source_ids:
        raise HTTPException(status_code=403, detail="not_granted")
    try:
        check_rate_limit(source_id, user["id"], _TEST_CONNECTION_RATE_LIMIT_PM)
    except RateLimited as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": str(int(exc.retry_after_seconds) + 1)},
        ) from exc
    try:
        enforce_per_user_credential(source, user["id"])
    except PerUserCredentialMissing as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    token = per_user_secrets_repo().get(source_id, user["id"]) or ""
    try:
        tools = await list_tools_async(source, caller_user_id=user["id"])
    except Exception as exc:  # upstream unreachable / bad token
        return TestResult(ok=False, tool_count=None, message=_redact_then_truncate(str(exc), token))
    return TestResult(ok=True, tool_count=len(tools), message="ok")
