"""User management endpoints (#11)."""

import os
import uuid
from datetime import datetime, timezone
from typing import Optional, List

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from argon2 import PasswordHasher

from app.auth.access import is_user_admin, require_admin
from app.auth.dependencies import _get_db
from src.db import SYSTEM_ADMIN_GROUP, SYSTEM_EVERYONE_GROUP
from src.repositories.users import UserRepository
from src.repositories.user_group_members import UserGroupMembersRepository
from src.repositories.audit import AuditRepository

router = APIRouter(prefix="/api/users", tags=["users"])


def _audit(conn: duckdb.DuckDBPyConnection, actor_id: str, action: str, target_id: str, params: Optional[dict] = None) -> None:
    try:
        # Convert non-JSON-serializable values (datetime) to strings first
        safe_params = None
        if params:
            safe_params = {}
            for k, v in params.items():
                if isinstance(v, datetime):
                    safe_params[k] = v.isoformat()
                else:
                    safe_params[k] = v
        AuditRepository(conn).log(
            user_id=actor_id,
            action=action,
            resource=f"user:{target_id}",
            params=safe_params,
        )
    except Exception:
        pass  # never block the endpoint on audit failure


class CreateUserRequest(BaseModel):
    email: str
    name: str
    role: str = "analyst"
    send_invite: bool = False


class UpdateUserRequest(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    active: Optional[bool] = None


class SetPasswordRequest(BaseModel):
    password: str


class GroupBrief(BaseModel):
    id: str
    name: str
    is_system: bool = False
    # Same 'system' | 'custom' | 'google_sync' tag as /api/admin/groups —
    # the user list renders membership chips with color-coded backgrounds
    # (Admin yellow, Everyone gray, google_sync green, custom purple) and
    # needs the origin to pick the right swatch.
    origin: str = "custom"


class UserResponse(BaseModel):
    id: str
    email: str
    name: Optional[str]
    role: str
    is_admin: bool = False
    is_sso_user: bool = False
    groups: List[GroupBrief] = []
    active: bool = True
    created_at: Optional[str]
    deactivated_at: Optional[str] = None
    invite_url: Optional[str] = None
    invite_email_sent: Optional[bool] = None


def _resolve_role(u: dict, conn: duckdb.DuckDBPyConnection) -> str:
    """Derive a label for the response. ``admin`` if the user is in the Admin
    system group, otherwise ``user`` — the legacy 4-value enum collapsed to
    a binary in v12 (admin / non-admin). The DB column ``users.role`` is a
    deprecated artifact; we ignore it."""
    return "admin" if is_user_admin(u["id"], conn) else "user"


def _user_groups(user_id: str, conn: duckdb.DuckDBPyConnection) -> List[GroupBrief]:
    """Groups the user is a member of, sorted with system groups first.

    Inlined into ``/api/users`` responses so the admin list view can show
    membership chips per row without an N+1 fetch. ``origin`` is computed
    via the same ``_derive_origin`` helper /api/admin/groups uses, so
    chip colors stay in lock-step across the two surfaces.
    """
    from app.api.access import _derive_origin
    rows = conn.execute(
        """SELECT g.id, g.name, g.is_system, g.created_by
           FROM user_group_members m
           JOIN user_groups g ON g.id = m.group_id
           WHERE m.user_id = ?
           ORDER BY g.is_system DESC, g.name""",
        [user_id],
    ).fetchall()
    return [
        GroupBrief(
            id=r[0],
            name=r[1],
            is_system=bool(r[2]),
            origin=_derive_origin(
                {"is_system": bool(r[2]), "name": r[1], "created_by": r[3]}
            ),
        )
        for r in rows
    ]


def _is_sso_user(user_id: str, conn: duckdb.DuckDBPyConnection) -> bool:
    """Whether the user is sourced from an external SSO provider.

    Today the only SSO provider is Google Workspace, but the name is kept
    generic so a future provider (Cloudflare Access, Okta, …) can plug into
    the same flag without churning the API surface. The admin UI hides the
    password-reset / set-password / delete affordances when this is True —
    those accounts are managed upstream and editing them here would either
    be no-ops (password) or get reverted on next sync (delete).

    A user counts as SSO-managed if they are a member of any group where:

      1. ``user_groups.created_by = 'system:google-sync'`` — the OAuth
         callback auto-created this group from a Workspace claim, OR
      2. the group is the seeded ``Admin`` system row AND
         ``AGNES_GROUP_ADMIN_EMAIL`` is set (env-mapped to a Workspace
         admin group), OR
      3. the group is the seeded ``Everyone`` system row AND
         ``AGNES_GROUP_EVERYONE_EMAIL`` is set (env-mapped to a Workspace
         everyone group).

    Users with no groups, or only admin-created custom groups, are NOT
    SSO users — local accounts are unaffected.

    Env values are read per-request so operators flipping the mapping
    don't have to restart the process.
    """
    rows = conn.execute(
        """SELECT g.name, g.is_system, g.created_by, m.source
           FROM user_group_members m
           JOIN user_groups g ON g.id = m.group_id
           WHERE m.user_id = ?""",
        [user_id],
    ).fetchall()
    if not rows:
        return False
    admin_mapped = bool(os.environ.get("AGNES_GROUP_ADMIN_EMAIL", "").strip())
    everyone_mapped = bool(os.environ.get("AGNES_GROUP_EVERYONE_EMAIL", "").strip())
    for name, is_system, created_by, source in rows:
        if created_by == "system:google-sync":
            # google-sync groups are always SSO-managed regardless of how
            # the individual membership was created — the group itself
            # only exists because of Google sync.
            return True
        # System-group branches (Admin / Everyone): the group accepts
        # memberships from MULTIPLE sources (system_seed for v13 backfill,
        # admin for manual adds, google_sync from OAuth callback). The
        # group being env-mapped to Workspace tells us SSO is *configured*,
        # but only memberships whose source is 'google_sync' are actually
        # owned by the upstream IdP. system_seed / admin memberships in
        # the same group are local-only and must stay locally manageable.
        # (Devin BUG_0002 on PR #142: without this check, the v13 migration's
        # blanket Everyone backfill flips every local user to SSO the moment
        # AGNES_GROUP_EVERYONE_EMAIL is set, locking admins out of password
        # reset / delete on accounts the IdP doesn't actually own.)
        if is_system and name == SYSTEM_ADMIN_GROUP and admin_mapped and source == "google_sync":
            return True
        if is_system and name == SYSTEM_EVERYONE_GROUP and everyone_mapped and source == "google_sync":
            return True
    return False


def _to_response(
    u: dict,
    conn: duckdb.DuckDBPyConnection,
    invite_url: Optional[str] = None,
    invite_email_sent: Optional[bool] = None,
) -> UserResponse:
    groups = _user_groups(u["id"], conn)
    return UserResponse(
        id=u["id"],
        email=u["email"],
        name=u.get("name"),
        role=_resolve_role(u, conn),
        is_admin=any(g.name == SYSTEM_ADMIN_GROUP for g in groups),
        is_sso_user=_is_sso_user(u["id"], conn),
        groups=groups,
        active=bool(u.get("active", True)),
        created_at=str(u.get("created_at", "")),
        deactivated_at=str(u["deactivated_at"]) if u.get("deactivated_at") else None,
        invite_url=invite_url,
        invite_email_sent=invite_email_sent,
    )


def _set_admin_membership(
    user_id: str,
    is_admin: bool,
    actor_email: Optional[str],
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Add or remove the user's Admin group membership. Idempotent."""
    admin_group = conn.execute(
        "SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP],
    ).fetchone()
    if not admin_group:
        return
    members = UserGroupMembersRepository(conn)
    if is_admin:
        members.add_member(user_id, admin_group[0], "admin", actor_email)
    else:
        members.remove_member(user_id, admin_group[0])


@router.get("", response_model=List[UserResponse])
async def list_users(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    return [_to_response(u, conn) for u in UserRepository(conn).list_all()]


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Single-user payload used by the /admin/users/{id} detail page header
    and the account-status block. Same shape as the list endpoint, so the
    page can reuse the same response shape."""
    target = UserRepository(conn).get_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    return _to_response(target, conn)


@router.post("", response_model=UserResponse, status_code=201)
async def create_user(
    payload: CreateUserRequest,
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = UserRepository(conn)
    if repo.get_by_email(payload.email):
        raise HTTPException(status_code=409, detail="User with this email already exists")
    import secrets
    user_id = str(uuid.uuid4())
    repo.create(id=user_id, email=payload.email, name=payload.name, role=payload.role)
    # If the requested role is admin, add to Admin group. Non-admin users start
    # with no group memberships — admin-managed grants must be explicit.
    if (payload.role or "").lower() == "admin":
        _set_admin_membership(user_id, True, user.get("email"), conn)
    _audit(conn, user["id"], "user.create", user_id, {"email": payload.email, "role": payload.role})

    invite_url: Optional[str] = None
    invite_email_sent: Optional[bool] = None
    if payload.send_invite:
        token = secrets.token_urlsafe(32)
        repo.update(
            id=user_id,
            setup_token=token,
            setup_token_created=datetime.now(timezone.utc),
        )
        from app.auth.providers.password import build_setup_url, send_setup_email
        invite_url = build_setup_url(request, payload.email, token)
        invite_email_sent = send_setup_email(request, payload.email, token)
        _audit(conn, user["id"], "user.invite", user_id, {"email": payload.email, "email_sent": invite_email_sent})

    created = repo.get_by_id(user_id)
    return _to_response(created, conn, invite_url=invite_url, invite_email_sent=invite_email_sent)


@router.patch("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: str,
    payload: UpdateUserRequest,
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = UserRepository(conn)
    target = repo.get_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    target_is_admin = is_user_admin(target["id"], conn)

    updates: dict = {}
    if payload.name is not None:
        updates["name"] = payload.name

    role_change: Optional[bool] = None  # None = no change; True = make admin; False = demote
    if payload.role is not None:
        wants_admin = payload.role.lower() == "admin"
        if (
            target["id"] == user["id"]
            and target_is_admin
            and not wants_admin
            and repo.count_admins(active_only=True) <= 1
        ):
            raise HTTPException(status_code=409, detail="Cannot demote the last active admin")
        if wants_admin != target_is_admin:
            role_change = wants_admin
        # Persist the legacy label on users.role for any reader still inspecting it.
        updates["role"] = payload.role

    if payload.active is not None:
        if target["id"] == user["id"] and payload.active is False:
            raise HTTPException(status_code=409, detail="Cannot deactivate yourself")
        if (
            target_is_admin
            and payload.active is False
            and repo.count_admins(active_only=True) <= 1
        ):
            raise HTTPException(status_code=409, detail="Cannot deactivate the last active admin")
        updates["active"] = payload.active
        if payload.active is False:
            updates["deactivated_at"] = datetime.now(timezone.utc)
            updates["deactivated_by"] = user["id"]
        else:
            updates["deactivated_at"] = None
            updates["deactivated_by"] = None

    if updates:
        repo.update(id=user_id, **updates)
        _audit(conn, user["id"], "user.update", user_id, {k: v for k, v in updates.items() if k != "deactivated_at"})
    if role_change is not None:
        _set_admin_membership(user_id, role_change, user.get("email"), conn)
    return _to_response(repo.get_by_id(user_id), conn)


_SSO_LOCKED_DETAIL = (
    "User is managed by an external SSO provider; "
    "this operation must be performed in the upstream system"
)


def _reject_if_sso(target_id: str, conn: duckdb.DuckDBPyConnection) -> None:
    """409 if the target is SSO-managed.

    The admin UI hides the password / delete affordances for SSO users, but
    the UI-only guard is bypassable by anyone who calls /api/users/...
    directly with a valid admin token. This is the server-side enforcement
    that backs the UI: admins cannot reset / set / wipe a Google-Workspace
    account through Agnes — those mutations belong upstream.
    """
    if _is_sso_user(target_id, conn):
        raise HTTPException(status_code=409, detail=_SSO_LOCKED_DETAIL)


@router.delete("/{user_id}", status_code=204)
async def delete_user(
    user_id: str,
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = UserRepository(conn)
    target = repo.get_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target["id"] == user["id"]:
        raise HTTPException(status_code=409, detail="Cannot delete yourself")
    _reject_if_sso(target["id"], conn)
    if is_user_admin(target["id"], conn) and repo.count_admins(active_only=True) <= 1:
        raise HTTPException(status_code=409, detail="Cannot delete the last active admin")
    repo.delete(user_id)
    _audit(conn, user["id"], "user.delete", user_id, {"email": target["email"]})


@router.post("/{user_id}/reset-password")
async def reset_password(
    user_id: str,
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Generate a reset token and (best-effort) email it to the user."""
    import secrets
    repo = UserRepository(conn)
    target = repo.get_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    _reject_if_sso(target["id"], conn)
    token = secrets.token_urlsafe(32)
    repo.update(
        id=user_id,
        reset_token=token,
        reset_token_created=datetime.now(timezone.utc),
    )
    _audit(conn, user["id"], "user.reset_password", user_id, {"email": target["email"]})
    # Dedicated password-reset email/URL — points to /auth/password/reset where the
    # user sets a new password, NOT to the magic-link verify endpoint (which would
    # log them in without prompting for a new password).
    from app.auth.providers.password import build_reset_url, send_reset_email
    reset_url = build_reset_url(request, target["email"], token)
    email_sent = send_reset_email(request, target["email"], token)
    return {
        "reset_url": reset_url,
        "email_sent": email_sent,
    }


@router.post("/{user_id}/set-password", status_code=204)
async def set_password(
    user_id: str,
    payload: SetPasswordRequest,
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    if not payload.password or len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    repo = UserRepository(conn)
    target = repo.get_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    _reject_if_sso(target["id"], conn)
    ph = PasswordHasher()
    repo.update(id=user_id, password_hash=ph.hash(payload.password))
    _audit(conn, user["id"], "user.set_password", user_id, {"email": target["email"]})


@router.post("/{user_id}/deactivate", response_model=UserResponse)
async def deactivate_user(
    user_id: str,
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    return await update_user(
        user_id=user_id,
        payload=UpdateUserRequest(active=False),
        request=request, user=user, conn=conn,
    )


@router.post("/{user_id}/activate", response_model=UserResponse)
async def activate_user(
    user_id: str,
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    return await update_user(
        user_id=user_id,
        payload=UpdateUserRequest(active=True),
        request=request, user=user, conn=conn,
    )
