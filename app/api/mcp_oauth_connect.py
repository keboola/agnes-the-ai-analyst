"""Outbound MCP OAuth connect flow — authorize + callback + disconnect
(2026-07-30 outbound MCP OAuth sources spec §3, PR 2).

Route prefix note: the callback lives under ``/api/mcp/oauth-client/`` —
deliberately distinct from the INBOUND issuer's ``/api/mcp/oauth/*``
(consent, token — Agnes acting as the authorization server, see
``app/auth/mcp_oauth.py``) so the OpenAPI surface keeps the two OAuth roles
visually separate.

Security posture (spec §6, all enforced below):

* **state** — signed (``app.auth.oauth_connect_state``), 10-minute max age,
  single-use (the backing ``mcp_oauth_flows`` row is deleted by
  ``consume()`` on first read), bound to the session user at callback
  (login-CSRF: the callback's authenticated session must match the state's
  ``user_id``, and the flow's stored ``user_id``/``source_id`` must both
  agree — three-way check).
* **PKCE** — the verifier is generated and stored server-side
  (``mcp_oauth_flows``, Fernet-encrypted) at authorize time; never exposed
  to the browser.
* **Mix-up defense (RFC 9700 §4.4)** — the token endpoint and client
  identity used at redemption come ONLY from
  ``mcp_source_oauth_clients[state.source_id]``, resolved server-side from
  the DB, never from callback query params or an AS response.
* **Grant gating** — ``_require_source_grant`` (shared with the my-secret
  endpoints) runs at authorize AND is re-checked at callback, since grants
  may have been revoked while the user was away at the AS. Both mutating
  endpoints are ``deny_principal`` — connect/disconnect are human-only.
* **RFC 9207** — schema v109 does not persist whether the AS advertises
  ``authorization_response_iss_parameter_supported`` (see the design doc's
  §1 table), so there is no stored flag to key an ``iss`` check off; this
  defense-in-depth layer is deferred to spec §7 PR 3 rather than guessing.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from urllib.parse import quote, urlencode, urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.auth.dependencies import get_current_user
from app.auth.oauth_connect_state import ConnectStateInvalid, sign_connect_state, verify_connect_state
from app.chat.session_principal_guard import deny_principal
from app.secrets_vault import VaultKeyNotConfiguredError
from connectors.mcp.client import exc_summary as _exc_summary
from src.repositories import (
    audit_repo,
    mcp_oauth_flows_repo,
    mcp_source_oauth_clients_repo,
    mcp_sources_repo,
    mcp_user_oauth_tokens_repo,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mcp", tags=["mcp-oauth-connect"])

#: Same cap family as ``mcp_user_secrets._TEST_CONNECTION_RATE_LIMIT_PM`` —
#: authorize doesn't itself make an outbound call, but it does mint a
#: DB-backed flow row per hit, and a client_id/redirect_uri leak from the
#: 302 target is worth throttling anyway.
_AUTHORIZE_RATE_LIMIT_PM = 6

_CONNECT_ERROR_MAX_LEN = 200


def _audit(actor_id: str, action: str, resource: str, params: Optional[Dict[str, Any]] = None) -> None:
    """Best-effort audit row. Mirrors ``app/api/admin_mcp._audit`` — never
    raises, and never carries token/code material (callers pass only
    booleans/ids)."""
    try:
        audit_repo().log(user_id=actor_id, action=action, resource=resource, params=params or {})
    except Exception:
        logger.warning("audit log failed for %s/%s", action, resource)


def _get_source_or_404(source_id: str) -> Dict[str, Any]:
    src = mcp_sources_repo().get(source_id)
    if not src:
        raise HTTPException(status_code=404, detail="mcp_source_not_found")
    return src


def _err_redirect(message: str) -> RedirectResponse:
    """Land the browser back on /me/connections with a short, redacted
    error — never a raw 500/400 page, and never token/code material in the
    query string (spec §3/§6)."""
    safe = (message or "connect failed")[:_CONNECT_ERROR_MAX_LEN]
    return RedirectResponse(url=f"/me/connections?connect_error={quote(safe)}", status_code=303)


@router.get("/sources/{source_id}/oauth/authorize")
async def authorize_oauth_connect(
    source_id: str,
    user: dict = Depends(get_current_user),
):
    """Kick off the browser authorization-code + PKCE flow for ``source_id``.

    ``deny_principal`` — connect is human-only, an agent/co-session token
    must never mint a token for its owner without the owner's live browser
    interaction. ``_require_source_grant`` — an ungranted caller must not be
    able to park a token for a source they cannot use. Responds ``302`` to
    the upstream authorization endpoint built from the stored
    ``mcp_source_oauth_clients`` row (never from request data).
    """
    from app.api.admin_mcp import _oauth_redirect_uri, _require_oauth_source
    from app.api.mcp_policy import RateLimited, check_rate_limit
    from app.api.mcp_user_secrets import _require_source_grant
    from connectors.mcp.oauth_client import generate_pkce_pair

    deny_principal(user)
    src = _get_source_or_404(source_id)
    _require_oauth_source(src)
    # Deliberate deviation from the sync-map's `Depends(require_resource_access)`
    # idiom (verify_syncmap WARNs here): per-source MCP access is governed by
    # tool_registry grants, not the generic ResourceType framework, and this
    # inline gate is the SAME one the sibling `my-secret` endpoints have used
    # since #919 — one mechanism, no drift. Swapping to ResourceType would mean
    # refactoring the whole passthrough authorization subsystem (out of scope).
    _require_source_grant(source_id, user)
    try:
        check_rate_limit(f"oauth-authorize:{source_id}", user["id"], _AUTHORIZE_RATE_LIMIT_PM)
    except RateLimited as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": str(int(exc.retry_after_seconds) + 1)},
        ) from exc

    client_row = mcp_source_oauth_clients_repo().get(source_id)
    if client_row is None:
        raise HTTPException(
            status_code=409,
            detail="oauth_client_not_registered: an admin must register an OAuth client for "
            "this source first (POST …/oauth/register or PUT …/oauth/client)",
        )

    flows_repo = mcp_oauth_flows_repo()
    flows_repo.sweep_expired()  # opportunistic housekeeping, not load-bearing

    verifier, challenge = generate_pkce_pair()
    nonce = uuid.uuid4().hex
    try:
        flows_repo.create(nonce, source_id, user["id"], verifier)
    except VaultKeyNotConfiguredError as exc:
        raise HTTPException(
            status_code=409,
            detail="vault_key_not_configured: set AGNES_VAULT_KEY on the server before storing secrets",
        ) from exc

    state = sign_connect_state(source_id, user["id"], nonce)
    params: Dict[str, str] = {
        "client_id": client_row["client_id"],
        "redirect_uri": _oauth_redirect_uri(),
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    scopes = client_row.get("scopes")
    if scopes:
        params["scope"] = scopes
    # The registered endpoint may already carry query parameters — join with
    # '&' in that case, never a second '?' (Devin Review on #1130).
    sep = "&" if urlparse(client_row["authorization_endpoint"]).query else "?"
    url = f"{client_row['authorization_endpoint']}{sep}{urlencode(params)}"
    return RedirectResponse(url=url, status_code=302)


@router.get("/oauth-client/callback")
async def oauth_connect_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
    user: dict = Depends(get_current_user),
):
    """Authorization-code redemption — the upstream AS redirects the user's
    browser here after they authorize (or decline).

    A browser landing page, not a JSON API: every failure mode redirects
    back to ``/me/connections?connect_error=…`` with a short, redacted
    message rather than raising a raw 4xx/5xx (spec §3). ``deny_principal``
    still applies — this route requires the caller's normal authenticated
    web session (cookie or bearer), never a restricted principal.
    """
    from app.api.admin_mcp import _require_oauth_source
    from app.api.mcp_user_secrets import _require_source_grant
    from connectors.mcp.oauth_client import (
        OAuthTokenError,
        build_oauth_http_client,
        exchange_code_for_token,
    )

    deny_principal(user)

    if error:
        return _err_redirect(error_description or error)
    if not code or not state:
        return _err_redirect("missing code or state")

    try:
        state_data = verify_connect_state(state)
    except ConnectStateInvalid as exc:
        return _err_redirect(f"invalid or expired connect request: {exc}")

    # Single-use: a second callback for the same nonce (replay, or a user
    # who double-clicks "authorize") gets None and a generic error.
    flow = mcp_oauth_flows_repo().consume(state_data["nonce"])
    if flow is None:
        return _err_redirect("connect request already used or expired")

    # Login-CSRF + mix-up guard: the signed state, the DB-backed flow row,
    # and the CURRENT session must all agree on who/what this flow is for.
    if flow["user_id"] != user.get("id") or flow["source_id"] != state_data["source_id"]:
        logger.warning(
            "mcp oauth callback state/session/flow mismatch (state.source=%s flow.source=%s)",
            state_data.get("source_id"),
            flow.get("source_id"),
        )
        return _err_redirect("connect request does not match your session")

    source_id = state_data["source_id"]
    src = mcp_sources_repo().get(source_id)
    if src is None:
        return _err_redirect("source no longer exists")
    try:
        _require_oauth_source(src)
        # Re-check the grant: it may have been revoked while the user was
        # away at the AS (spec §3).
        _require_source_grant(source_id, user)
    except HTTPException as exc:
        return _err_redirect(str(exc.detail))

    client_row = mcp_source_oauth_clients_repo().get(source_id)
    if client_row is None:
        return _err_redirect("oauth client registration is missing")

    from app.api.admin_mcp import _oauth_redirect_uri

    try:
        redirect_uri = _oauth_redirect_uri()
    except HTTPException as exc:
        # public_url unset raises HTTPException(409) — on the browser-facing
        # callback EVERY failure mode must land back on /me/connections, not
        # a raw error page (Devin Review on #1130).
        return _err_redirect(str(exc.detail))

    try:
        async with build_oauth_http_client() as http_client:
            token_set = await exchange_code_for_token(
                token_endpoint=client_row["token_endpoint"],
                client_id=client_row["client_id"],
                client_secret=client_row.get("client_secret"),
                code=code,
                redirect_uri=redirect_uri,
                code_verifier=flow["pkce_verifier"],
                client=http_client,
            )
    except OAuthTokenError as exc:
        # Operator-facing detail goes to the server log ONLY. The browser
        # redirect gets a fixed message: the exception text can carry the
        # AS's raw error body (`_raise_as_error`'s `resp.text` fallback) —
        # externally controlled content that must not be replayed into a
        # user-facing query string (RBAC review on PR 2).
        logger.warning("mcp oauth token exchange failed for source=%s: %s", source_id, _exc_summary(exc))
        return _err_redirect("token exchange with the authorization server failed — try again or contact your admin")
    except httpx.HTTPError as exc:
        logger.warning("mcp oauth token exchange transport error for source=%s: %s", source_id, _exc_summary(exc))
        return _err_redirect("could not reach the authorization server — try again or contact your admin")

    expires_at = None
    if token_set.expires_in:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=token_set.expires_in)
    try:
        mcp_user_oauth_tokens_repo().upsert(
            source_id,
            user["id"],
            token_set.access_token,
            refresh_token=token_set.refresh_token,
            expires_at=expires_at,
            scopes=token_set.scopes,
        )
    except VaultKeyNotConfiguredError:
        return _err_redirect("vault_key_not_configured: contact your admin")

    _audit(user["id"], "mcp_oauth.connect", f"mcp_source:{source_id}")
    return RedirectResponse(url=f"/me/connections?connected={quote(source_id)}", status_code=303)


@router.delete("/sources/{source_id}/oauth/connection", status_code=204)
async def disconnect_oauth(
    source_id: str,
    user: dict = Depends(get_current_user),
):
    """Drop the caller's OAuth connection for ``source_id``.

    ``deny_principal`` + ``_require_source_grant`` — same human-only,
    grant-gated contract as authorize. Best-effort RFC 7009 revocation is
    deferred to spec §7 PR 3: schema v109 does not persist a
    ``revocation_endpoint`` for the stored authorization server (only
    ``authorization_endpoint``/``token_endpoint`` — see the design doc's §1
    table), so there is no endpoint to safely call without guessing a URL
    shape. This mirrors ``…/my-secret`` DELETE, which also only ever drops
    the local credential row (the upstream token still needs to be revoked
    by the user in the upstream system if they want it fully dead).
    """
    from app.api.admin_mcp import _require_oauth_source
    from app.api.mcp_user_secrets import _require_source_grant

    deny_principal(user)
    src = _get_source_or_404(source_id)
    _require_oauth_source(src)
    _require_source_grant(source_id, user)
    mcp_user_oauth_tokens_repo().delete(source_id, user["id"])
    _audit(user["id"], "mcp_oauth.disconnect", f"mcp_source:{source_id}")
