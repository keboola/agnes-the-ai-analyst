"""Keboola OAuth login provider.

Same shape as the Google provider: authlib redirect flow with session-backed
``state``, then a session cookie. Identity comes from verifying the OAuth
access token against the stack's /tokens/verify (see keboola_verify — the
master-token/project/role gates live there). First login auto-provisions via
the shared helper; membership in the configured project is the trust
boundary, so the allowed_domain filter is deliberately NOT applied here.
"""

import logging

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from app.auth._common import safe_next_path
from app.auth.jwt import SESSION_COOKIE_MAX_AGE_SECONDS, create_access_token
from app.auth.provider_registry import require_provider
from app.auth.providers import keboola_verify as kv
from app.auth.provisioning import UserDeactivatedError, ensure_user

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/auth/keboola",
    tags=["auth"],
    dependencies=[Depends(require_provider("keboola"))],
)

oauth = OAuth()

# KeboolaVerifyError.reason → /login?error=<code>. Every code has copy in
# login.html; anything unmapped falls back to the generic failure.
_ERROR_CODE_BY_REASON = {
    "project_mismatch": "keboola_project_mismatch",
    "not_master_token": "keboola_not_permitted",
    "role_forbidden": "keboola_not_permitted",
    "no_admin_identity": "keboola_not_permitted",
    "invalid_token": "keboola_oauth_failed",
    "verify_failed": "keboola_oauth_failed",
    "not_configured": "keboola_not_configured",
}


def is_available() -> bool:
    """Config-completeness only — the allowlist is a separate layer (spec)."""
    return bool(kv.client_id() and kv.client_secret() and kv.configured_project_id() and kv.stack_url())


def _oauth_client():
    """Lazily register the authlib client (config is instance.yaml, read at
    first use, unlike Google's import-time env vars). Safe to call repeatedly."""
    client = oauth.create_client("keboola")
    if client is not None:
        return client
    host = kv.oauth_host()
    oauth.register(
        name="keboola",
        client_id=kv.client_id(),
        client_secret=kv.client_secret(),
        authorize_url=f"{host}/oauth/authorize",
        access_token_url=f"{host}/oauth/token",
        client_kwargs={"scope": "email"},
    )
    return oauth.create_client("keboola")


@router.get("/login")
async def keboola_login(request: Request):
    """Redirect to the Keboola OAuth authorize endpoint (state in session)."""
    if not is_available():
        return RedirectResponse(url="/login?error=keboola_not_configured", status_code=302)
    next_path = safe_next_path(request.query_params.get("next"), default="")
    if next_path:
        request.session["login_next"] = next_path
    else:
        request.session.pop("login_next", None)
    redirect_uri = str(request.url_for("keboola_callback"))
    return await _oauth_client().authorize_redirect(request, redirect_uri)


@router.get("/callback")
async def keboola_callback(request: Request):
    """Exchange the code, verify the access token at the stack, sign in."""
    if not is_available():
        return RedirectResponse(url="/login?error=keboola_not_configured", status_code=302)
    try:
        # SSRF: oauth_host is re-validated at use time (not just when
        # stored), same posture as stack_url in
        # keboola_verify._fetch_verify — a DNS-rebind or config edit
        # between store and use must not point the token exchange at a
        # private/internal address.
        from app.api.admin import _validate_url_not_private

        _validate_url_not_private(kv.oauth_host(), "auth.keboola.oauth_host")
        token = await _oauth_client().authorize_access_token(request)
    except Exception:
        logger.exception("Keboola OAuth token exchange failed")
        return RedirectResponse(url="/login?error=keboola_oauth_failed", status_code=302)
    access_token = str(token.get("access_token") or "")
    # Backstop: any unexpected failure in the post-exchange flow (verify →
    # provisioning → JWT → cookie) must land on the friendly login banner,
    # never a raw 500. The specific except branches below return their own
    # redirects from inside the try, so the backstop never shadows them.
    try:
        try:
            # Sync HTTP verify off the event loop (same Tier-1 posture as auth).
            identity = await run_in_threadpool(kv.verify_oauth_access_token, access_token)
        except kv.KeboolaVerifyError as exc:
            logger.info("Keboola login rejected: %s", exc.reason)
            code = _ERROR_CODE_BY_REASON.get(exc.reason, "keboola_oauth_failed")
            return RedirectResponse(url=f"/login?error={code}", status_code=302)
        try:
            user = await run_in_threadpool(
                ensure_user, identity.email, identity.name, source="auth.keboola:first-signin"
            )
        except UserDeactivatedError:
            return RedirectResponse(url="/login?error=deactivated", status_code=302)

        jwt_token = create_access_token(user["id"], user["email"])
        target = safe_next_path(request.session.pop("login_next", None))

        from app.auth.public_url import cookie_secure
        from app.instance_config import session_cookie_domain

        response = RedirectResponse(url=target, status_code=302)
        response.set_cookie(
            key="access_token",
            value=jwt_token,
            httponly=True,
            max_age=SESSION_COOKIE_MAX_AGE_SECONDS,
            samesite="lax",
            secure=cookie_secure(request),
            domain=session_cookie_domain(),
        )
        return response
    except Exception:
        # Deliberately no token/identity details in the log record.
        logger.exception("Keboola OAuth callback failed after token exchange")
        return RedirectResponse(url="/login?error=keboola_oauth_failed", status_code=302)
