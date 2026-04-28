"""Google OAuth provider for FastAPI.

Group memberships are sourced via Application Default Credentials in
``app.auth.group_sync.fetch_user_groups`` (no per-user OAuth scope needed for
that path), so the OAuth flow only handles authentication and returns a
session JWT. Membership writes go to ``user_group_members``.
"""

import os
import logging

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

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

        # Find or create user, sync Workspace group memberships into
        # user_group_members. Prefix filter and login gate are driven by
        # AGNES_GOOGLE_GROUP_PREFIX — empty value preserves legacy behavior
        # (mirror everything, no gate).
        from src.db import get_system_db
        from src.repositories.users import UserRepository
        from src.repositories.user_groups import (
            UserGroupsRepository, ExternalIdConflict,
        )
        from src.repositories.user_group_members import UserGroupMembersRepository
        from app.auth.group_sync import fetch_user_groups
        import uuid

        prefix = os.environ.get("AGNES_GOOGLE_GROUP_PREFIX", "").strip().lower()

        conn = get_system_db()
        try:
            repo = UserRepository(conn)
            user = repo.get_by_email(email)
            user_is_new = user is None
            if user_is_new:
                user_id = str(uuid.uuid4())
                repo.create(id=user_id, email=email, name=name)
                user = repo.get_by_email(email)
            if not bool(user.get("active", True)):
                return RedirectResponse(url="/login?error=deactivated")

            ug_repo = UserGroupsRepository(conn)
            members_repo = UserGroupMembersRepository(conn)

            fetched = fetch_user_groups(email)
            if fetched is None:
                # Soft fail — pass-through if user has prior google_sync rows.
                # First-timers can't be verified so they're denied; this also
                # means a misconfigured Google client on a fresh deploy fails
                # closed.
                if not members_repo.has_any_google_sync_membership(user["id"]):
                    logger.warning(
                        "Google group fetch soft-failed for %s and no cached "
                        "google_sync membership exists; denying login.",
                        email,
                    )
                    return RedirectResponse(
                        url="/login?error=group_check_unavailable",
                        status_code=302,
                    )
                logger.warning(
                    "Google group fetch soft-failed for %s; passing through "
                    "on cached membership snapshot.",
                    email,
                )
            else:
                if prefix:
                    relevant = [g.lower() for g in fetched if g.lower().startswith(prefix)]
                else:
                    relevant = [g.lower() for g in fetched]
                if prefix and not relevant:
                    logger.info(
                        "Login denied for %s: no membership in any group "
                        "starting with %r (fetched %d non-matching groups).",
                        email, prefix, len(fetched),
                    )
                    return RedirectResponse(
                        url="/login?error=not_in_foundryai_group",
                        status_code=302,
                    )

                group_ids: list[str] = []
                for email_addr in relevant:
                    try:
                        grp = ug_repo.resolve_or_create_for_external(
                            email_addr, prefix
                        )
                        group_ids.append(grp["id"])
                    except ExternalIdConflict as e:
                        logger.warning(
                            "Skipping group %s due to external_id collision: %s",
                            email_addr, e,
                        )
                members_repo.replace_google_sync_groups(
                    user["id"], group_ids, added_by="system:google-sync",
                )
                logger.info(
                    "Google group sync for %s: fetched=%d, prefix-matched=%d, "
                    "applied=%d",
                    email, len(fetched), len(relevant), len(group_ids),
                )
        finally:
            conn.close()

        # Issue JWT — role field is legacy; pass empty string so callers
        # that still inspect it during the transition don't NPE on None.
        jwt_token = create_access_token(user["id"], user["email"], user.get("role") or "")

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
