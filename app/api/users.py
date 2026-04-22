"""User management endpoints (#11)."""

import uuid
from datetime import datetime, timezone
from typing import Optional, List

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from argon2 import PasswordHasher

from app.auth.dependencies import require_role, Role, _get_db
from src.repositories.users import UserRepository
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


class UserResponse(BaseModel):
    id: str
    email: str
    name: Optional[str]
    role: str
    active: bool = True
    created_at: Optional[str]
    deactivated_at: Optional[str] = None
    invite_url: Optional[str] = None
    invite_email_sent: Optional[bool] = None


def _to_response(u: dict, invite_url: Optional[str] = None, invite_email_sent: Optional[bool] = None) -> UserResponse:
    return UserResponse(
        id=u["id"],
        email=u["email"],
        name=u.get("name"),
        role=u["role"],
        active=bool(u.get("active", True)),
        created_at=str(u.get("created_at", "")),
        deactivated_at=str(u["deactivated_at"]) if u.get("deactivated_at") else None,
        invite_url=invite_url,
        invite_email_sent=invite_email_sent,
    )


@router.get("", response_model=List[UserResponse])
async def list_users(
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    return [_to_response(u) for u in UserRepository(conn).list_all()]


@router.post("", response_model=UserResponse, status_code=201)
async def create_user(
    payload: CreateUserRequest,
    request: Request,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = UserRepository(conn)
    if repo.get_by_email(payload.email):
        raise HTTPException(status_code=409, detail="User with this email already exists")
    try:
        Role(payload.role)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown role: {payload.role}")
    import secrets
    user_id = str(uuid.uuid4())
    repo.create(id=user_id, email=payload.email, name=payload.name, role=payload.role)
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
    return _to_response(created, invite_url=invite_url, invite_email_sent=invite_email_sent)


@router.patch("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: str,
    payload: UpdateUserRequest,
    request: Request,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = UserRepository(conn)
    target = repo.get_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    updates: dict = {}
    if payload.name is not None:
        updates["name"] = payload.name
    if payload.role is not None:
        # Validate role is a known value
        try:
            Role(payload.role)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Unknown role: {payload.role}")
        # Protect: don't let admin demote themselves if they are the last admin
        if (
            target["id"] == user["id"]
            and target["role"] == "admin"
            and payload.role != "admin"
            and repo.count_admins(active_only=True) <= 1
        ):
            raise HTTPException(status_code=409, detail="Cannot demote the last active admin")
        updates["role"] = payload.role
    if payload.active is not None:
        # Protect: cannot self-deactivate
        if target["id"] == user["id"] and payload.active is False:
            raise HTTPException(status_code=409, detail="Cannot deactivate yourself")
        # Protect: cannot deactivate the last active admin
        if (
            target.get("role") == "admin"
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
    return _to_response(repo.get_by_id(user_id))


@router.delete("/{user_id}", status_code=204)
async def delete_user(
    user_id: str,
    request: Request,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = UserRepository(conn)
    target = repo.get_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target["id"] == user["id"]:
        raise HTTPException(status_code=409, detail="Cannot delete yourself")
    if target.get("role") == "admin" and repo.count_admins(active_only=True) <= 1:
        raise HTTPException(status_code=409, detail="Cannot delete the last active admin")
    repo.delete(user_id)
    _audit(conn, user["id"], "user.delete", user_id, {"email": target["email"]})


@router.post("/{user_id}/reset-password")
async def reset_password(
    user_id: str,
    request: Request,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Generate a reset token and (best-effort) email it to the user."""
    import secrets
    repo = UserRepository(conn)
    target = repo.get_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
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
        "reset_token": token,
        "reset_url": reset_url,
        "email_sent": email_sent,
    }


@router.post("/{user_id}/set-password", status_code=204)
async def set_password(
    user_id: str,
    payload: SetPasswordRequest,
    request: Request,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    if not payload.password or len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    repo = UserRepository(conn)
    target = repo.get_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    ph = PasswordHasher()
    repo.update(id=user_id, password_hash=ph.hash(payload.password))
    _audit(conn, user["id"], "user.set_password", user_id, {"email": target["email"]})


@router.post("/{user_id}/deactivate", response_model=UserResponse)
async def deactivate_user(
    user_id: str,
    request: Request,
    user: dict = Depends(require_role(Role.ADMIN)),
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
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    return await update_user(
        user_id=user_id,
        payload=UpdateUserRequest(active=True),
        request=request, user=user, conn=conn,
    )
