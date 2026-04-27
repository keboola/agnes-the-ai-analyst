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
# AND an admin-enabled Cloud Identity / Google Workspace tenant.
#
# We use `groups/-/memberships:searchTransitiveGroups` (the "what groups does
# THIS USER belong to" endpoint), NOT `groups:search` (admin "find groups in
# org" endpoint, which requires Groups Reader admin role + 400s otherwise).
# The `-` in the path is a wildcard meaning "search across all groups in the
# caller's organization". Returns transitive memberships (incl. nested groups).
# Reference: https://cloud.google.com/identity/docs/reference/rest/v1/groups.memberships/searchTransitiveGroups
GROUPS_SEARCH_URL = (
    "https://cloudidentity.googleapis.com/v1/groups/-/memberships:searchTransitiveGroups"
)


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

    searchTransitiveGroups query syntax (CEL) requires:
      - a `labels` membership predicate scoping the group type
      - `member_key_id == '<email>'` for the user
    Without `labels` Google returns 400 INVALID_ARGUMENT (silently — error
    body just says "invalid argument").
    Reference: https://cloud.google.com/identity/docs/reference/rest/v1/groups.memberships/searchTransitiveGroups

    Why `security` label and not `discussion_forum`:
        Empirically Keboola's Workspace lets a non-admin user read their own
        group memberships ONLY for groups labelled as security groups
        (`cloudidentity.googleapis.com/groups.security`). The same query with
        `groups.discussion_forum` returns 403 "Insufficient permissions to
        retrieve memberships" — the discussion_forum API needs admin scope.
        In practice every Workspace group at Keboola carries BOTH labels, so
        filtering on `security` returns the full membership list anyway.
        Confirmed via scripts/debug/probe_google_groups.py.
    """
    query = (
        f"member_key_id == '{email}' "
        f"&& 'cloudidentity.googleapis.com/groups.security' in labels"
    )
    params = {"query": query}
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

    # searchTransitiveGroups returns `memberships`, not `groups`. Each membership
    # carries the group identity in groupKey.id (email-shaped) + displayName.
    groups = []
    for m in data.get("memberships", []) or []:
        group_key = (m.get("groupKey") or {}).get("id", "")
        if not group_key:
            continue
        groups.append({
            "id": group_key,
            "name": m.get("displayName") or group_key,
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
        from src.repositories.plugin_access import UserGroupsRepository
        from app.auth.group_sync import fetch_user_groups
        import uuid

        conn = get_system_db()
        try:
            repo = UserRepository(conn)
            user = repo.get_by_email(email)
            if not user:
                user_id = str(uuid.uuid4())
                repo.create(id=user_id, email=email, name=name, role="analyst")
                user = repo.get_by_email(email)
            # v9: legacy users.role is NULL after migration; hydrate before
            # create_access_token reads it (writing role: null into the JWT
            # is non-crashing but semantically wrong; downstream hydration
            # in get_current_user would fix the per-request view, but the
            # token payload itself stays misleading).
            from app.auth.dependencies import _hydrate_legacy_role
            user = _hydrate_legacy_role(user, conn)
            if not bool(user.get("active", True)):
                return RedirectResponse(url="/login?error=deactivated")

            # Sync Workspace groups — fail-soft: any error leaves users.groups
            # as-is (stale from previous login) and the login still proceeds.
            try:
                groups = fetch_user_groups(email)
                if groups:
                    ug_repo = UserGroupsRepository(conn)
                    for group_name in groups:
                        ug_repo.ensure(group_name)
                    repo.set_groups(user["id"], groups)
            except Exception as sync_err:  # noqa: BLE001 - fail-soft by design
                logger.warning(
                    "Google group sync failed for %s: %s", email, sync_err
                )
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

        # Resolve external groups into internal role keys at sign-in. Cached
        # on the session for the lifetime of this login — refresh requires
        # re-login, same as the google_groups list itself. We pass user_id
        # so direct user_role_grants are also folded into the session cache
        # (otherwise the DB-fallback in require_internal_role would fire on
        # every admin-gated request, defeating the OAuth fast path).
        try:
            from app.auth.role_resolver import resolve_internal_roles
            from src.db import get_system_db
            conn = get_system_db()
            try:
                resolved = resolve_internal_roles(
                    request.session.get("google_groups", []) or [],
                    conn,
                    user_id=user["id"],
                )
            finally:
                conn.close()
            request.session["internal_roles"] = resolved
            # INFO-level audit so a wrong-role complaint is debuggable from
            # the log alone — admin can correlate "user X claims they lost
            # access" with the resolver output without replaying the request.
            logger.info(
                "Resolved %d internal role(s) for %s: %s",
                len(resolved), email, resolved or "<none>",
            )
        except Exception as e:
            # Resolver errors must not break login — fall back to no roles.
            logger.warning("Failed to resolve internal_roles for %s: %s", email, e)
            request.session["internal_roles"] = []

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
