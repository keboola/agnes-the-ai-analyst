"""Google OAuth provider for FastAPI."""

import os
import logging

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from starlette.config import Config as StarletteConfig

from app.auth.jwt import create_access_token
from app.auth._common import safe_next_path
from app.instance_config import get_allowed_domains

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

        # Find or create user
        from src.db import get_system_db
        from src.repositories.users import UserRepository
        import uuid

        conn = get_system_db()
        try:
            repo = UserRepository(conn)
            user = repo.get_by_email(email)
            if not user:
                user_id = str(uuid.uuid4())
                repo.create(id=user_id, email=email, name=name, role="analyst")
                user = repo.get_by_email(email)
            if not bool(user.get("active", True)):
                return RedirectResponse(url="/login?error=deactivated")
        finally:
            conn.close()

        # Issue JWT
        jwt_token = create_access_token(user["id"], user["email"], user["role"])

        # Redirect to the post-login target. Prefer the value stashed by
        # google_login() — re-sanitize defensively in case of session tampering.
        target = safe_next_path(
            request.session.pop("login_next", None), default="/dashboard"
        )

        # Redirect to target with token in cookie
        is_production = os.environ.get("TESTING", "").lower() not in ("1", "true")
        response = RedirectResponse(url=target, status_code=302)
        response.set_cookie(
            key="access_token", value=jwt_token,
            httponly=True, max_age=86400, samesite="lax",
            secure=is_production,
        )
        return response

    except Exception as e:
        logger.error(f"Google OAuth error: {e}")
        return RedirectResponse(url="/login?error=oauth_failed")
