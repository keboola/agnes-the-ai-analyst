"""Google OAuth provider for FastAPI."""

import os
import logging

import httpx
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

# Cloud Identity Groups API — requires the cloud-identity.groups.readonly scope
# AND an admin-enabled Cloud Identity / Google Workspace tenant. A 403 here
# simply means the tenant isn't Workspace-enabled; we tolerate it.
GROUPS_SEARCH_URL = "https://cloudidentity.googleapis.com/v1/groups:search"


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
        client_kwargs={
            "scope": (
                "openid email profile "
                "https://www.googleapis.com/auth/cloud-identity.groups.readonly"
            ),
        },
    )


async def _fetch_google_groups(access_token: str, email: str) -> list[dict]:
    """Fetch Google Workspace groups the user belongs to.

    Best-effort: returns [] on any failure (403 non-Workspace tenant, 401 expired
    token, network error, etc.). Must never raise — callers rely on this to keep
    the login flow working even when Cloud Identity is unavailable.

    Cloud Identity Groups Search query syntax (CEL) requires:
      - `parent == 'customers/<id>'`  (use 'customers/my_customer' alias to mean
        the OAuth-authenticated user's own org — no need to know the customer ID)
      - a `labels` membership predicate scoping the group type
    plus optional `member_key_id`. Without `parent` + `labels` Google returns
    400 INVALID_ARGUMENT (silently — error body just says "invalid argument").
    Reference: https://cloud.google.com/identity/docs/reference/rest/v1/groups/search
    """
    # Workspace mailing-list-style "discussion_forum" groups (the common case).
    # Security groups would be `cloudidentity.googleapis.com/groups.security`
    # — handled in a follow-up if needed; one query at a time keeps the call
    # cheap and the error surface small.
    query = (
        f"parent == 'customers/my_customer' "
        f"&& member_key_id == '{email}' "
        f"&& 'cloudidentity.googleapis.com/groups.discussion_forum' in labels"
    )
    params = {"query": query, "view": "BASIC"}
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(GROUPS_SEARCH_URL, params=params, headers=headers)
        if resp.status_code >= 400:
            # Log full body (not truncated) so future query-syntax / scope /
            # tenant issues are diagnosable from one log line.
            logger.warning(
                "Google groups fetch returned %s for %s — query=%r — body=%s",
                resp.status_code, email, query, resp.text,
            )
            return []
        data = resp.json()
    except Exception as e:
        logger.warning("Google groups fetch failed for %s: %s", email, e)
        return []

    groups = []
    for g in data.get("groups", []) or []:
        group_key = (g.get("groupKey") or {}).get("id", "")
        if not group_key:
            continue
        groups.append({
            "id": group_key,
            "name": g.get("displayName") or group_key,
        })
    return groups


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

        # Fetch Google Workspace groups (best-effort — must not break login).
        access_token = token.get("access_token", "")
        if access_token:
            try:
                groups = await _fetch_google_groups(access_token, email)
                request.session["google_groups"] = groups
            except Exception as e:
                logger.warning("Failed to store google_groups in session: %s", e)
                request.session["google_groups"] = []
        else:
            request.session["google_groups"] = []

        # Issue JWT
        jwt_token = create_access_token(user["id"], user["email"], user["role"])

        # Redirect to the post-login target. Prefer the value stashed by
        # google_login() — re-sanitize defensively in case of session tampering.
        target = safe_next_path(
            request.session.pop("login_next", None), default="/dashboard"
        )

        # Redirect to target with token in cookie. Match password/email providers:
        # Secure only when DOMAIN is set (production with TLS), so the cookie is
        # actually sent over plain HTTP in dev.
        use_secure = os.environ.get("DOMAIN", "") != ""
        response = RedirectResponse(url=target, status_code=302)
        response.set_cookie(
            key="access_token", value=jwt_token,
            httponly=True, max_age=86400, samesite="lax",
            secure=use_secure,
        )
        return response

    except Exception as e:
        logger.error(f"Google OAuth error: {e}")
        return RedirectResponse(url="/login?error=oauth_failed")
