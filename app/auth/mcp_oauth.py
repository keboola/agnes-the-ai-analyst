"""OAuth 2.1 Authorization Server provider for the Agnes MCP connector.

Implements ``OAuthAuthorizationServerProvider`` from the MCP SDK so any
MCP-compatible AI agent (Claude Desktop, Claude.ai, Cursor, Cline, …) can
connect to Agnes using the standard browser-based OAuth 2.1 + PKCE flow
without needing a manually-issued PAT.

Flow summary
------------
1. MCP client POSTs to ``/api/mcp/oauth/register`` (RFC 7591 dynamic
   client registration) — client metadata is persisted in
   ``oauth_clients`` via the factory repo.
2. MCP client redirects user browser to ``/api/mcp/oauth/authorize``.
   The SDK's ``AuthorizationHandler`` calls ``provider.authorize()``,
   which:
   a. Checks for an active Agnes session cookie (``Authorization``
      header or ``session`` cookie set by the browser login flow).
   b. If no session: redirects to ``/auth/google/login?next=…`` (or
      the email-magic-link login page) so the user authenticates with
      the identity provider they already use for Agnes.
   c. After login the user is sent back to ``/api/mcp/oauth/consent``
      with the pending authorization parameters stashed in the session.
   d. On the consent page the user clicks "Allow" which POSTs back and
      triggers ``_complete_authorize``:  a short-lived authorization
      code is minted and stored, then the browser is redirected to the
      MCP client's ``redirect_uri?code=…&state=…``.
3. MCP client POSTs to ``/api/mcp/oauth/token`` with the authorization
   code + PKCE verifier.  ``exchange_authorization_code`` validates,
   deletes the code, mints a JWT token via ``create_access_token``, and
   persists it in ``oauth_access_tokens``.  The JWT is a standard Agnes
   session JWT, so ``resolve_token_to_user`` accepts it unchanged and
   ALL existing RBAC applies automatically via the self-call pattern in
   ``mcp_http.py``.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
import uuid

from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

logger = logging.getLogger(__name__)

# How long (seconds) an authorization code is valid before it expires.
_AUTH_CODE_TTL = 300  # 5 minutes
# Access-token lifetime in seconds (matches Agnes PAT "no expiry" pattern;
# keep short so revocation takes effect quickly).
_ACCESS_TOKEN_TTL = 3600 * 8  # 8 hours
# Refresh-token lifetime (optional rotation).
_REFRESH_TOKEN_TTL = 3600 * 24 * 30  # 30 days

# Session key used to stash pending authorization state during the
# login-redirect round-trip.
_SESSION_PENDING_AUTH_KEY = "mcp_oauth_pending"


class AgnesMCPOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    """Agnes implementation of the MCP SDK OAuth provider protocol.

    All token persistence goes through the ``oauth_clients_repo()``
    factory so the correct backend (DuckDB or Postgres) is selected at
    runtime — never instantiated directly here.
    """

    # ------------------------------------------------------------------
    # RFC 7591 dynamic client registration
    # ------------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        from src.repositories import oauth_clients_repo

        row = oauth_clients_repo().get_client(client_id)
        if row is None:
            return None
        return _row_to_client_info(row)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        from src.repositories import oauth_clients_repo

        meta = client_info.model_dump(exclude={"client_id", "client_secret"})
        # redirect_uris is stored separately for fast lookup; remove from meta
        meta.pop("redirect_uris", None)
        meta.pop("client_id_issued_at", None)
        meta.pop("client_secret_expires_at", None)

        oauth_clients_repo().upsert_client(
            client_id=client_info.client_id,
            client_secret=client_info.client_secret,
            redirect_uris=[str(u) for u in (client_info.redirect_uris or [])],
            client_name=getattr(client_info, "client_name", None),
            client_metadata=meta,
        )

    # ------------------------------------------------------------------
    # Authorization (step 2 — redirect to login/consent)
    # ------------------------------------------------------------------

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        """Return the URL the SDK should redirect the browser to.

        This is called by the SDK's AuthorizationHandler *before* we have
        access to the live Starlette ``Request`` object (the handler only
        gives us the parsed params).  We therefore encode the pending
        authorization state into a short-lived token stored in the
        ``oauth_auth_codes`` table under a ``_pending_`` prefix, and
        send the browser to our consent bridge at
        ``/api/mcp/oauth/consent?pending=<token>``.

        The consent bridge reads the pending state, checks / establishes
        the Agnes session, shows the consent page, and on confirmation
        calls ``_complete_authorize`` to write the real authorization code.
        """
        # Stash pending auth state as a temp record so we can retrieve it
        # after the login round-trip without relying on a cookie (some
        # clients open the authorize URL in a system browser with no shared
        # cookie jar).
        pending_token = "pending_" + secrets.token_urlsafe(32)
        from src.repositories import oauth_clients_repo

        oauth_clients_repo().save_auth_code(
            code=pending_token,
            client_id=client.client_id,
            scopes=list(params.scopes or []),
            code_challenge=params.code_challenge,
            redirect_uri=str(params.redirect_uri),
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            expires_at=time.time() + _AUTH_CODE_TTL,
            subject=None,  # not yet resolved — filled in at consent
            resource=params.resource,
        )

        base = _base_url()
        consent_url = f"{base}/api/mcp/oauth/consent?pending={pending_token}"
        if params.state:
            consent_url += f"&state={params.state}"
        return consent_url

    # ------------------------------------------------------------------
    # Authorization code exchange (step 3)
    # ------------------------------------------------------------------

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        from src.repositories import oauth_clients_repo

        row = oauth_clients_repo().get_auth_code(authorization_code)
        if row is None:
            return None
        if row["client_id"] != client.client_id:
            return None
        if row["expires_at"] < time.time():
            return None
        return AuthorizationCode(
            code=row["code"],
            scopes=row["scopes"],
            expires_at=row["expires_at"],
            client_id=row["client_id"],
            code_challenge=row["code_challenge"],
            redirect_uri=AnyUrl(row["redirect_uri"]),
            redirect_uri_provided_explicitly=row["redirect_uri_provided_explicitly"],
            resource=row.get("resource"),
            subject=row.get("subject"),
        )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        from datetime import timedelta

        from app.auth.jwt import create_access_token
        from src.repositories import oauth_clients_repo

        # Delete code immediately (one-time use).
        oauth_clients_repo().delete_auth_code(authorization_code.code)

        subject = authorization_code.subject
        if not subject:
            raise TokenError(error="invalid_grant", error_description="No subject on code")

        # Resolve user to get email for the JWT.
        from src.repositories import users_repo

        user = users_repo().get_by_id(subject)
        if not user:
            raise TokenError(error="invalid_grant", error_description="User not found")

        jti = uuid.uuid4().hex
        access_jwt = create_access_token(
            user_id=subject,
            email=user["email"],
            expires_delta=timedelta(seconds=_ACCESS_TOKEN_TTL),
            token_id=jti,
            typ="session",
        )

        oauth_clients_repo().save_access_token(
            token=access_jwt,
            client_id=client.client_id,
            scopes=list(authorization_code.scopes),
            expires_at=int(time.time()) + _ACCESS_TOKEN_TTL,
            subject=subject,
            resource=authorization_code.resource,
        )

        # Mint a refresh token so clients can renew without re-authorizing.
        refresh_token_str = secrets.token_urlsafe(48)
        oauth_clients_repo().save_refresh_token(
            token=refresh_token_str,
            client_id=client.client_id,
            scopes=list(authorization_code.scopes),
            subject=subject,
            expires_at=int(time.time()) + _REFRESH_TOKEN_TTL,
        )

        return OAuthToken(
            access_token=access_jwt,
            token_type="bearer",
            expires_in=_ACCESS_TOKEN_TTL,
            refresh_token=refresh_token_str,
            scope=" ".join(authorization_code.scopes) if authorization_code.scopes else None,
        )

    # ------------------------------------------------------------------
    # Refresh token exchange
    # ------------------------------------------------------------------

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        from src.repositories import oauth_clients_repo

        row = oauth_clients_repo().get_refresh_token(refresh_token)
        if row is None or row.get("revoked_at") is not None:
            return None
        if row["client_id"] != client.client_id:
            return None
        exp = row.get("expires_at")
        if exp is not None and exp < int(time.time()):
            return None
        return RefreshToken(
            token=row["token"],
            client_id=row["client_id"],
            scopes=row["scopes"],
            expires_at=row.get("expires_at"),
            subject=row.get("subject"),
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        from datetime import timedelta

        from app.auth.jwt import create_access_token
        from src.repositories import oauth_clients_repo, users_repo

        subject = refresh_token.subject
        if not subject:
            raise TokenError(error="invalid_grant", error_description="No subject")
        user = users_repo().get_by_id(subject)
        if not user:
            raise TokenError(error="invalid_grant", error_description="User not found")

        # Revoke old refresh token (rotation).
        oauth_clients_repo().revoke_refresh_token(refresh_token.token)

        effective_scopes = scopes or list(refresh_token.scopes)
        jti = uuid.uuid4().hex
        access_jwt = create_access_token(
            user_id=subject,
            email=user["email"],
            expires_delta=timedelta(seconds=_ACCESS_TOKEN_TTL),
            token_id=jti,
            typ="session",
        )
        oauth_clients_repo().save_access_token(
            token=access_jwt,
            client_id=client.client_id,
            scopes=effective_scopes,
            expires_at=int(time.time()) + _ACCESS_TOKEN_TTL,
            subject=subject,
        )

        new_refresh = secrets.token_urlsafe(48)
        oauth_clients_repo().save_refresh_token(
            token=new_refresh,
            client_id=client.client_id,
            scopes=effective_scopes,
            subject=subject,
            expires_at=int(time.time()) + _REFRESH_TOKEN_TTL,
        )

        return OAuthToken(
            access_token=access_jwt,
            token_type="bearer",
            expires_in=_ACCESS_TOKEN_TTL,
            refresh_token=new_refresh,
            scope=" ".join(effective_scopes) if effective_scopes else None,
        )

    # ------------------------------------------------------------------
    # Token verification (used by SDK's ProviderTokenVerifier)
    # ------------------------------------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        from src.repositories import oauth_clients_repo

        row = oauth_clients_repo().get_access_token(token)
        if row is None or row.get("revoked_at") is not None:
            return None
        exp = row.get("expires_at")
        if exp is not None and exp < int(time.time()):
            return None
        return AccessToken(
            token=row["token"],
            client_id=row["client_id"],
            scopes=row["scopes"],
            expires_at=row.get("expires_at"),
            subject=row.get("subject"),
            resource=row.get("resource"),
        )

    # ------------------------------------------------------------------
    # Revocation
    # ------------------------------------------------------------------

    async def revoke_token(
        self,
        token: AccessToken | RefreshToken,
    ) -> None:
        from src.repositories import oauth_clients_repo

        repo = oauth_clients_repo()
        if isinstance(token, AccessToken):
            repo.revoke_access_token(token.token)
        else:
            repo.revoke_refresh_token(token.token)


# ---------------------------------------------------------------------------
# Consent / login bridge — FastAPI router mounted by main.py
# ---------------------------------------------------------------------------


async def _consent_page(request: Request) -> Response:
    """GET handler: show the consent screen, or redirect to login first."""
    from src.repositories import oauth_clients_repo

    pending = request.query_params.get("pending", "")
    state = request.query_params.get("state", "")

    # Validate that the pending code exists and hasn't expired.
    pending_row = oauth_clients_repo().get_auth_code(pending)
    if pending_row is None or pending_row["expires_at"] < time.time():
        return HTMLResponse("<h2>Authorization request expired. Please try again.</h2>", status_code=400)

    # Check if the user is logged in (Agnes session cookie / header).
    user = _get_session_user(request)
    if user is None:
        # Not logged in — redirect to Google/email login then come back.
        login_url = _login_url(request, pending, state)
        return RedirectResponse(url=login_url, status_code=302)

    client_row = oauth_clients_repo().get_client(pending_row["client_id"])
    client_name = (client_row or {}).get("client_name") or pending_row["client_id"]
    scopes = pending_row["scopes"]
    html = _render_consent_page(
        user_email=user.get("email", ""),
        client_name=client_name,
        scopes=scopes,
        pending=pending,
        state=state,
    )
    return HTMLResponse(html)


async def _consent_submit(request: Request) -> Response:
    """POST handler: user clicked Allow/Deny — mint the code and redirect."""
    from src.repositories import oauth_clients_repo

    form = await request.form()
    pending = str(form.get("pending", ""))
    state = str(form.get("state", ""))
    action = str(form.get("action", "allow"))

    pending_row = oauth_clients_repo().get_auth_code(pending)
    if pending_row is None or pending_row["expires_at"] < time.time():
        return HTMLResponse("<h2>Authorization request expired.</h2>", status_code=400)

    redirect_uri = pending_row["redirect_uri"]

    if action != "allow":
        # User denied — redirect with error.
        sep = "&" if "?" in redirect_uri else "?"
        deny_url = f"{redirect_uri}{sep}error=access_denied"
        if state:
            deny_url += f"&state={state}"
        return RedirectResponse(url=deny_url, status_code=302)

    user = _get_session_user(request)
    if user is None:
        return HTMLResponse("<h2>Not authenticated.</h2>", status_code=401)

    # Replace pending code with the real authorization code.
    real_code = secrets.token_urlsafe(32)
    oauth_clients_repo().save_auth_code(
        code=real_code,
        client_id=pending_row["client_id"],
        scopes=pending_row["scopes"],
        code_challenge=pending_row["code_challenge"],
        redirect_uri=redirect_uri,
        redirect_uri_provided_explicitly=pending_row["redirect_uri_provided_explicitly"],
        expires_at=time.time() + _AUTH_CODE_TTL,
        subject=user["id"],
        resource=pending_row.get("resource"),
    )
    oauth_clients_repo().delete_auth_code(pending)

    sep = "&" if "?" in redirect_uri else "?"
    final_url = f"{redirect_uri}{sep}code={real_code}"
    if state:
        final_url += f"&state={state}"
    return RedirectResponse(url=final_url, status_code=302)


def make_consent_routes() -> list:
    """Return Starlette routes for the OAuth consent + login bridge.

    These are deliberately plain Starlette routes (not a FastAPI router) so
    the OAuth *browser* flow stays off the documented JSON-API surface —
    exactly like the SDK's own ``/authorize`` / ``/token`` / ``/register``
    endpoints, which live in the mounted streamable sub-app and never appear
    in ``app.openapi()``. Appended to ``app.router.routes`` in ``app/main.py``.
    """
    from starlette.routing import Route

    return [
        Route("/api/mcp/oauth/consent", _consent_page, methods=["GET"]),
        Route("/api/mcp/oauth/consent", _consent_submit, methods=["POST"]),
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_url() -> str:
    """Public base URL for this Agnes instance (no trailing slash)."""
    return os.environ.get("AGNES_BASE_URL", "http://localhost:8000").rstrip("/")


def _get_session_user(request: Request) -> dict | None:
    """Return the logged-in Agnes user from a session JWT / cookie, or None."""
    from app.auth.pat_resolver import resolve_token_to_user

    # Try Authorization header first (API clients).
    auth = request.headers.get("authorization", "")
    token = ""
    if auth.lower().startswith("bearer "):
        token = auth[7:]
    # Try session cookie set by Google/email login.
    if not token:
        token = request.cookies.get("agnes_session", "")
    if not token:
        return None
    user, _ = resolve_token_to_user(None, token, request)
    if user is None:
        return None
    # SessionPrincipal has no "id" key; exclude co-session tokens.
    if not isinstance(user, dict) or "id" not in user:
        return None
    return user


def _login_url(request: Request, pending: str, state: str) -> str:
    """Build the login redirect URL that returns to the consent page."""
    base = _base_url()
    consent_path = f"/api/mcp/oauth/consent?pending={pending}"
    if state:
        consent_path += f"&state={state}"
    # Prefer Google OAuth if available, fall back to email magic-link.
    from app.auth.providers.google import is_available as google_available

    if google_available():
        from urllib.parse import quote

        return f"{base}/auth/google/login?next={quote(consent_path)}"
    from urllib.parse import quote

    return f"{base}/login?next={quote(consent_path)}"


def _row_to_client_info(row: dict) -> OAuthClientInformationFull:
    """Convert a repo row to an ``OAuthClientInformationFull`` instance."""
    meta: dict = row.get("client_metadata") or {}
    redirect_uris = row.get("redirect_uris") or []
    return OAuthClientInformationFull(
        client_id=row["client_id"],
        client_secret=row.get("client_secret"),
        redirect_uris=[AnyUrl(u) for u in redirect_uris],
        client_name=row.get("client_name"),
        **{k: v for k, v in meta.items() if k not in {"client_id", "client_secret", "redirect_uris", "client_name"}},
    )


def _render_consent_page(
    user_email: str,
    client_name: str,
    scopes: list[str],
    pending: str,
    state: str,
) -> str:
    """Return the HTML consent page (extends base_ds.html via Jinja in production)."""
    scope_list = "".join(f"<li>{s}</li>" for s in scopes) if scopes else "<li>read access</li>"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Authorize {client_name} — Agnes</title>
  <style>
    :root {{
      --ds-primary: #6366f1;
      --ds-bg: #f8fafc;
      --ds-surface: #ffffff;
      --ds-text: #1e293b;
      --ds-muted: #64748b;
      --ds-border: #e2e8f0;
      --ds-danger: #ef4444;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: system-ui, sans-serif;
      background: var(--ds-bg);
      color: var(--ds-text);
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      padding: 1.5rem;
    }}
    .card {{
      background: var(--ds-surface);
      border: 1px solid var(--ds-border);
      border-radius: 0.75rem;
      padding: 2rem;
      max-width: 440px;
      width: 100%;
      box-shadow: 0 4px 24px rgba(0,0,0,.06);
    }}
    h1 {{ font-size: 1.25rem; font-weight: 600; margin-bottom: .5rem; }}
    .sub {{ color: var(--ds-muted); font-size: .875rem; margin-bottom: 1.5rem; }}
    .app-name {{ font-weight: 600; color: var(--ds-text); }}
    .scope-list {{ list-style: none; margin-bottom: 1.5rem; }}
    .scope-list li {{
      padding: .375rem .75rem;
      background: var(--ds-bg);
      border-radius: .375rem;
      font-size: .875rem;
      margin-bottom: .375rem;
      border: 1px solid var(--ds-border);
    }}
    .actions {{ display: flex; gap: .75rem; justify-content: flex-end; }}
    button {{
      cursor: pointer;
      border: none;
      border-radius: .5rem;
      padding: .5rem 1.25rem;
      font-size: .9rem;
      font-weight: 500;
    }}
    .btn-allow {{
      background: var(--ds-primary);
      color: #fff;
    }}
    .btn-deny {{
      background: var(--ds-bg);
      border: 1px solid var(--ds-border);
      color: var(--ds-text);
    }}
    .user {{ font-size: .8rem; color: var(--ds-muted); margin-top: 1.25rem; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>Authorize access</h1>
    <p class="sub">
      <span class="app-name">{client_name}</span>
      is requesting permission to access Agnes on your behalf.
    </p>
    <ul class="scope-list">
      {scope_list}
    </ul>
    <form method="post" action="/api/mcp/oauth/consent">
      <input type="hidden" name="pending" value="{pending}">
      <input type="hidden" name="state" value="{state}">
      <div class="actions">
        <button type="submit" name="action" value="deny" class="btn-deny">Deny</button>
        <button type="submit" name="action" value="allow" class="btn-allow">Allow</button>
      </div>
    </form>
    <p class="user">Signed in as {user_email}</p>
  </div>
</body>
</html>"""
