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
        # user_group_members.
        from src.db import (
            get_system_db,
            SYSTEM_ADMIN_GROUP,
            SYSTEM_EVERYONE_GROUP,
        )
        from src.repositories.users import UserRepository
        from src.repositories.user_groups import UserGroupsRepository
        from src.repositories.user_group_members import UserGroupMembersRepository
        from app.auth.group_sync import fetch_user_groups
        import uuid

        # Optional Workspace-group prefix filter + system-group mapping. Read
        # per-request so test fixtures and operators can flip via env without
        # restarting the process. Empty prefix = legacy behavior (mirror all).
        prefix = os.environ.get(
            "AGNES_GOOGLE_GROUP_PREFIX", ""
        ).strip().lower()
        admin_email = os.environ.get(
            "AGNES_GROUP_ADMIN_EMAIL", ""
        ).strip().lower()
        everyone_email = os.environ.get(
            "AGNES_GROUP_EVERYONE_EMAIL", ""
        ).strip().lower()

        conn = get_system_db()
        try:
            repo = UserRepository(conn)
            user = repo.get_by_email(email)
            if not user:
                user_id = str(uuid.uuid4())
                repo.create(id=user_id, email=email, name=name)
                user = repo.get_by_email(email)
            if not bool(user.get("active", True)):
                return RedirectResponse(url="/login?error=deactivated")

            # Sync Workspace groups → user_group_members (source='google_sync').
            # Fail-soft: any error leaves the previous membership snapshot in
            # place; admin-added rows survive regardless.
            members_repo = UserGroupMembersRepository(conn)
            try:
                group_names = fetch_user_groups(email)
                # `fetch_user_groups` is fail-soft and returns [] for both
                # "user genuinely has no groups" and "transient API failure".
                # Empty result is treated as "no change": preserve the
                # previous snapshot rather than wiping it on a transient
                # hiccup. Admin-added rows survive regardless.
                if not group_names:
                    logger.info(
                        "Google group sync for %s: empty result, "
                        "preserving existing memberships",
                        email,
                    )
                else:
                    # Lower-cased Workspace email of each group; comparisons
                    # against admin_email/everyone_email/prefix are all
                    # case-insensitive.
                    fetched = [g.lower() for g in group_names]

                    if prefix:
                        relevant = [g for g in fetched if g.startswith(prefix)]
                    else:
                        relevant = list(fetched)

                    # Login gate: prefix is set AND fetch returned a
                    # non-empty list AND none of those groups match the
                    # prefix → user is signed into Google but is not a
                    # member of any group permitted to use this Agnes
                    # instance. Pass-through-on-empty-fetch is preserved
                    # above (transient API failures must not lock users
                    # out), so this branch fires only when we got a real
                    # answer that excluded them.
                    if prefix and not relevant:
                        logger.info(
                            "Google login denied for %s: no group with "
                            "prefix %r in %s",
                            email, prefix, fetched,
                        )
                        return RedirectResponse(
                            url="/login?error=not_in_allowed_group"
                        )

                    ug_repo = UserGroupsRepository(conn)
                    group_ids: list[str] = []
                    for email_addr in relevant:
                        if admin_email and email_addr == admin_email:
                            sys_admin = ug_repo.get_by_name(
                                SYSTEM_ADMIN_GROUP
                            )
                            if sys_admin:
                                group_ids.append(sys_admin["id"])
                            continue
                        if everyone_email and email_addr == everyone_email:
                            sys_everyone = ug_repo.get_by_name(
                                SYSTEM_EVERYONE_GROUP
                            )
                            if sys_everyone:
                                group_ids.append(sys_everyone["id"])
                            continue
                        # Regular synced group: name = full email. ensure()
                        # is get-or-create-by-name and stamps
                        # created_by='system:google-sync' on first create.
                        g = ug_repo.ensure(email_addr)
                        group_ids.append(g["id"])

                    members_repo.replace_google_sync_groups(
                        user["id"], group_ids, added_by="system:google-sync",
                    )
                    logger.info(
                        "Google group sync for %s: %d group(s) "
                        "(filtered from %d fetched, prefix=%r) [%s]",
                        email, len(group_ids), len(fetched), prefix,
                        ", ".join(relevant),
                    )
            except Exception as sync_err:  # noqa: BLE001 - fail-soft by design
                logger.warning(
                    "Google group sync failed for %s: %s", email, sync_err
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
