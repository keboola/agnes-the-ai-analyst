"""Microsoft Entra ID (Azure AD) OAuth provider for FastAPI.

Single-tenant sign-in only — ``MICROSOFT_TENANT_ID`` scopes the discovery
document to one Entra tenant, never the multi-tenant `common`/`organizations`
endpoints. This module handles authentication only: on success the user is
created (or matched) via the shared ``ensure_user`` provisioning path and
granted Everyone membership. There is no Microsoft Graph group sync here —
unlike ``app.auth.providers.google``, which mirrors Workspace group
membership via ``apply_user_groups``. Group sync (Graph ``/me/memberOf``) is
deferred to a later change; see the TODO below.
"""

import os
import logging

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from app.auth.jwt import create_access_token, SESSION_COOKIE_MAX_AGE_SECONDS
from app.auth._common import safe_next_path
from app.auth.provider_registry import require_provider
from app.instance_config import get_allowed_domains


logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/auth/microsoft",
    tags=["auth"],
    dependencies=[Depends(require_provider("microsoft"))],
)

oauth = OAuth()

MICROSOFT_CLIENT_ID = os.environ.get("MICROSOFT_CLIENT_ID", "")
MICROSOFT_CLIENT_SECRET = os.environ.get("MICROSOFT_CLIENT_SECRET", "")
MICROSOFT_TENANT_ID = os.environ.get("MICROSOFT_TENANT_ID", "")


def is_available() -> bool:
    return bool(MICROSOFT_CLIENT_ID and MICROSOFT_CLIENT_SECRET and MICROSOFT_TENANT_ID)


def _setup_oauth():
    if not is_available():
        return
    oauth.register(
        name="microsoft",
        client_id=MICROSOFT_CLIENT_ID,
        client_secret=MICROSOFT_CLIENT_SECRET,
        server_metadata_url=(
            f"https://login.microsoftonline.com/{MICROSOFT_TENANT_ID}/v2.0/.well-known/openid-configuration"
        ),
        client_kwargs={"scope": "openid email profile"},
    )


_setup_oauth()


@router.get("/login")
async def microsoft_login(request: Request):
    """Redirect to Microsoft Entra ID OAuth.

    Honors `?next=<path>` by stashing the sanitized value in the session so the
    callback can redirect there instead of the default /dashboard. The session
    is the right stash — OAuth flow is stateful and the `state` param is
    managed by Authlib.
    """
    if not is_available():
        return RedirectResponse(url="/login?error=microsoft_not_configured")
    next_path = safe_next_path(request.query_params.get("next"), default="")
    if next_path:
        request.session["login_next"] = next_path
    else:
        # Clear any stale value from an earlier aborted attempt.
        request.session.pop("login_next", None)
    redirect_uri = str(request.url_for("microsoft_callback"))
    return await oauth.microsoft.authorize_redirect(request, redirect_uri)


@router.get("/callback")
async def microsoft_callback(request: Request):
    """Handle Microsoft Entra ID OAuth callback."""
    if not is_available():
        return RedirectResponse(url="/login?error=microsoft_not_configured")

    try:
        token = await oauth.microsoft.authorize_access_token(request)
        user_info = token.get("userinfo", {})
        # Entra work/school accounts often omit the `email` claim entirely;
        # `preferred_username` is the reliable identifier (usually a UPN,
        # which is email-shaped for most tenants).
        email = (user_info.get("email") or user_info.get("preferred_username") or "").strip().lower()
        name = user_info.get("name", "")

        if not email or "@" not in email:
            return RedirectResponse(url="/login?error=no_email")

        # Domain check
        allowed = get_allowed_domains()
        if allowed:
            domain = email.split("@")[-1]
            if domain not in allowed:
                return RedirectResponse(url="/login?error=domain_not_allowed")

        # Find or create user. ``ensure_user`` grants Everyone membership at
        # creation time only (once) — we deliberately do NOT re-assert it on
        # every login, so an admin who later removes the membership stays
        # removed. This matches google.py / keboola.py, which rely solely on
        # that one-time grant. No Microsoft Graph group sync in this provider
        # yet; every signed-in user gets Everyone membership and nothing else.
        # TODO(group-sync): mirror Entra ID group membership via Graph
        # `/me/memberOf`, the way google.py's apply_user_groups mirrors
        # Workspace groups.
        from app.auth.provisioning import UserDeactivatedError, ensure_user

        try:
            user = ensure_user(email, name, source="auth.microsoft:first-signin")
        except UserDeactivatedError:
            return RedirectResponse(url="/login?error=deactivated")

        # Issue JWT — identity-only, authorization derives from
        # user_group_members at request time (see app.auth.access).
        jwt_token = create_access_token(user["id"], user["email"])

        # Redirect to the post-login target. Prefer the value stashed by
        # microsoft_login() — re-sanitize defensively in case of session
        # tampering. default=None → safe_next_path resolves to the
        # operator-configured home route (AGNES_HOME_ROUTE /
        # instance.home_route / /dashboard).
        target = safe_next_path(request.session.pop("login_next", None))

        # Redirect to target with token in cookie. Secure whenever served over
        # HTTPS (proxy-aware via request scheme + resolved public origin), not
        # only when DOMAIN is set — see app.auth.public_url.cookie_secure.
        from app.auth.public_url import cookie_secure

        use_secure = cookie_secure(request)
        response = RedirectResponse(url=target, status_code=302)
        from app.instance_config import session_cookie_domain

        response.set_cookie(
            key="access_token",
            value=jwt_token,
            httponly=True,
            max_age=SESSION_COOKIE_MAX_AGE_SECONDS,
            samesite="lax",
            secure=use_secure,
            domain=session_cookie_domain(),
        )
        return response

    except Exception as e:
        logger.error(f"Microsoft OAuth error: {e}")
        return RedirectResponse(url="/login?error=oauth_failed")
