"""Google OAuth provider for FastAPI.

Group memberships are sourced via Application Default Credentials in
``app.auth.group_sync.fetch_user_groups`` (no per-user OAuth scope needed for
that path), so the OAuth flow only handles authentication and returns a
session JWT. Membership writes go through ``app.auth.group_sync.apply_user_groups``,
shared with ``POST /auth/refresh-groups`` so the OAuth-only refresh limitation
is no longer a thing — CLI / PAT-driven users can re-sync without a browser
sign-in.
"""

import os
import logging

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.auth.jwt import create_access_token, SESSION_COOKIE_MAX_AGE_SECONDS
from app.auth._common import safe_next_path
from app.instance_config import get_allowed_domains

from src.repositories import users_repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/google", tags=["auth"])

oauth = OAuth()

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")


def is_available() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def _setup_oauth():
    if not is_available():
        return
    oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


_setup_oauth()


@router.get("/login")
async def google_login(request: Request):
    """Redirect to Google OAuth.

    Honors `?next=<path>` by stashing the sanitized value in the session so the
    callback can redirect there instead of the default /dashboard. The session
    is the right stash — OAuth flow is stateful and the `state` param is
    managed by Authlib.
    """
    if not is_available():
        return RedirectResponse(url="/login?error=google_not_configured")
    next_path = safe_next_path(request.query_params.get("next"), default="")
    if next_path:
        request.session["login_next"] = next_path
    else:
        # Clear any stale value from an earlier aborted attempt.
        request.session.pop("login_next", None)
    redirect_uri = str(request.url_for("google_callback"))
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/callback")
async def google_callback(request: Request):
    """Handle Google OAuth callback."""
    if not is_available():
        return RedirectResponse(url="/login?error=google_not_configured")

    try:
        token = await oauth.google.authorize_access_token(request)
        user_info = token.get("userinfo", {})
        email = user_info.get("email", "")
        name = user_info.get("name", "")

        if not email:
            return RedirectResponse(url="/login?error=no_email")

        # Domain check
        allowed = get_allowed_domains()
        if allowed:
            domain = email.split("@")[-1]
            if domain not in allowed:
                return RedirectResponse(url="/login?error=domain_not_allowed")

        # Find or create user, sync Workspace group memberships into
        # user_group_members.
        from app.auth.group_sync import apply_user_groups
        import uuid

        repo = users_repo()
        user = repo.get_by_email(email)
        if not user:
            user_id = str(uuid.uuid4())
            repo.create(id=user_id, email=email, name=name)
            # Issue #748: auto-grant Everyone at creation (source=
            # 'system_seed') unless AGNES_GROUP_EVERYONE_EMAIL maps
            # Everyone to a Workspace group — then apply_user_groups
            # below is the sole writer. Creation-time only: never
            # called again for a returning user, so an admin's manual
            # removal later sticks.
            try:
                from app.auth.group_sync import ensure_everyone_membership

                ensure_everyone_membership(user_id, added_by="auth.google:first-signin")
            except Exception:
                logger.exception(
                    "ensure_everyone_membership failed for new user %s",
                    email,
                )
            # v39: subscribe new user to every system plugin so the
            # mandatory tier reaches them on their first session
            # without an admin reconcile. Fail-soft — a transient
            # marketplace_plugins read failure doesn't block sign-in.
            try:
                from src.repositories import user_curated_subscriptions_repo

                user_curated_subscriptions_repo().fanout_system_for_user(user_id)
            except Exception:
                logger.exception(
                    "system-plugin fanout failed for new user %s",
                    email,
                )
            user = repo.get_by_email(email)
        if not bool(user.get("active", True)):
            return RedirectResponse(url="/login?error=deactivated")

        # Sync Workspace groups → user_group_members (source='google_sync').
        # Shared write path with /auth/refresh-groups so post-OAuth-callback
        # refreshes use the same logic. Fail-soft: ``apply_user_groups``
        # never raises; on transient API failure it returns
        # ``soft_failed=True`` and preserves the previous snapshot.
        # `conn` is `None` — `apply_user_groups` ignores it, routing every
        # repo lookup through the backend-aware factory instead (retained
        # only for signature stability, matching the app/auth/access.py
        # call site).
        sync_result = apply_user_groups(user["id"], email, None)

        # Login gate: ``denied=True`` means the prefix filter is configured
        # and the Admin SDK returned a non-empty fetch that contained zero
        # groups matching the prefix — i.e. the user is signed into Google
        # but is not a member of any group permitted to use this Agnes
        # instance. ``soft_failed`` (empty fetch / API error) does NOT
        # trigger the gate, so transient outages can't lock users out.
        if sync_result.denied:
            return RedirectResponse(url="/login?error=not_in_allowed_group")

        # Issue JWT — identity-only, authorization derives from
        # user_group_members at request time (see app.auth.access).
        jwt_token = create_access_token(user["id"], user["email"])

        # Redirect to the post-login target. Prefer the value stashed by
        # google_login() — re-sanitize defensively in case of session tampering.
        # default=None → safe_next_path resolves to the operator-configured
        # home route (AGNES_HOME_ROUTE / instance.home_route / /dashboard).
        target = safe_next_path(request.session.pop("login_next", None))

        # Redirect to target with token in cookie. Match password/email providers:
        # Secure only when DOMAIN is set (production with TLS), so the cookie is
        # actually sent over plain HTTP in dev.
        use_secure = os.environ.get("DOMAIN", "") != ""
        response = RedirectResponse(url=target, status_code=302)
        response.set_cookie(
            key="access_token",
            value=jwt_token,
            httponly=True,
            max_age=SESSION_COOKIE_MAX_AGE_SECONDS,
            samesite="lax",
            secure=use_secure,
        )
        return response

    except Exception as e:
        logger.error(f"Google OAuth error: {e}")
        return RedirectResponse(url="/login?error=oauth_failed")
