"""Personal access token endpoints (#12)."""

import hashlib
import secrets
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, List

import duckdb
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.dependencies import require_session_token, require_role, _get_db
from src.rbac import Role
from src.repositories.access_tokens import AccessTokenRepository
from src.repositories.audit import AuditRepository
from app.auth.jwt import create_access_token

router = APIRouter(prefix="/auth/tokens", tags=["tokens"])
admin_router = APIRouter(prefix="/auth/admin/tokens", tags=["tokens-admin"])


class CreateTokenRequest(BaseModel):
    name: str
    expires_in_days: Optional[int] = 90  # null = no expiry


class CreateTokenResponse(BaseModel):
    id: str
    name: str
    prefix: str
    token: str  # raw token — returned exactly once
    expires_at: Optional[str]
    created_at: str


class TokenListItem(BaseModel):
    id: str
    name: str
    prefix: str
    created_at: str
    expires_at: Optional[str]
    last_used_at: Optional[str]
    revoked_at: Optional[str]


class AdminTokenItem(TokenListItem):
    """Admin list row: adds owner identity + last IP for incident response."""
    user_id: str
    user_email: Optional[str] = None
    last_used_ip: Optional[str] = None


def _audit(conn, actor: str, action: str, target: str, params=None):
    try:
        AuditRepository(conn).log(user_id=actor, action=action,
                                  resource=f"token:{target}", params=params)
    except Exception:
        pass


def _row_to_item(row: dict) -> TokenListItem:
    return TokenListItem(
        id=row["id"], name=row["name"], prefix=row["prefix"],
        created_at=str(row.get("created_at") or ""),
        expires_at=str(row["expires_at"]) if row.get("expires_at") else None,
        last_used_at=str(row["last_used_at"]) if row.get("last_used_at") else None,
        revoked_at=str(row["revoked_at"]) if row.get("revoked_at") else None,
    )


def _row_to_admin_item(row: dict) -> AdminTokenItem:
    return AdminTokenItem(
        id=row["id"], name=row["name"], prefix=row["prefix"],
        created_at=str(row.get("created_at") or ""),
        expires_at=str(row["expires_at"]) if row.get("expires_at") else None,
        last_used_at=str(row["last_used_at"]) if row.get("last_used_at") else None,
        revoked_at=str(row["revoked_at"]) if row.get("revoked_at") else None,
        user_id=row.get("user_id") or "",
        user_email=row.get("user_email"),
        last_used_ip=row.get("last_used_ip"),
    )


@router.post("", response_model=CreateTokenResponse, status_code=201)
async def create_token(
    payload: CreateTokenRequest,
    user: dict = Depends(require_session_token),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    if not payload.name.strip():
        raise HTTPException(status_code=400, detail="name is required")
    if payload.expires_in_days is not None and payload.expires_in_days <= 0:
        raise HTTPException(status_code=400, detail="expires_in_days must be a positive integer")
    repo = AccessTokenRepository(conn)
    token_id = str(uuid.uuid4())
    expires_at = None
    if payload.expires_in_days is not None:
        expires_delta = timedelta(days=payload.expires_in_days)
        expires_at = datetime.now(timezone.utc) + expires_delta
    else:
        # "No expiry" at the DB level, but the JWT still needs a bounded `exp`
        # claim — otherwise `create_access_token` falls back to the 24h session
        # default and the PAT silently dies. Use ~100 years; the DB-level
        # revocation/expiry check in verify_token is the real enforcement.
        expires_delta = timedelta(days=36500)
    # Build the JWT that embeds jti=token_id and typ=pat
    jwt_token = create_access_token(
        user_id=user["id"], email=user["email"], role=user["role"],
        token_id=token_id, typ="pat", expires_delta=expires_delta,
    )
    # Prefix: first 8 chars of the jti (UUID) — uniquely identifies the token in UI
    # without exposing JWT headers (which all start with "eyJhbGci…" and are useless
    # for identification). The JWT itself is returned ONCE in the response body.
    prefix = token_id.replace("-", "")[:8]
    # token_hash = sha256(raw JWT). Used in verify_token as defense-in-depth.
    token_hash = hashlib.sha256(jwt_token.encode()).hexdigest()
    repo.create(
        id=token_id, user_id=user["id"], name=payload.name.strip(),
        token_hash=token_hash, prefix=prefix, expires_at=expires_at,
    )
    _audit(conn, user["id"], "token.create", token_id, {"name": payload.name})
    return CreateTokenResponse(
        id=token_id, name=payload.name.strip(), prefix=prefix,
        token=jwt_token,  # returned EXACTLY ONCE; never retrievable again
        expires_at=str(expires_at) if expires_at else None,
        created_at=str(datetime.now(timezone.utc)),
    )


@router.get("", response_model=List[TokenListItem])
async def list_tokens(
    user: dict = Depends(require_session_token),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    rows = AccessTokenRepository(conn).list_for_user(user["id"])
    return [_row_to_item(r) for r in rows]


@router.get("/{token_id}", response_model=TokenListItem)
async def get_token(
    token_id: str,
    user: dict = Depends(require_session_token),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    row = AccessTokenRepository(conn).get_by_id(token_id)
    if not row or row["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Token not found")
    return _row_to_item(row)


@router.delete("/{token_id}", status_code=204)
async def revoke_token(
    token_id: str,
    user: dict = Depends(require_session_token),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = AccessTokenRepository(conn)
    row = repo.get_by_id(token_id)
    if not row or row["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Token not found")
    repo.revoke(token_id)
    _audit(conn, user["id"], "token.revoke", token_id)


# Admin — list & revoke tokens across users (for incident response)

@admin_router.get("", response_model=List[AdminTokenItem])
async def admin_list_tokens(
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    return [_row_to_admin_item(r) for r in AccessTokenRepository(conn).list_all_with_user()]


@admin_router.delete("/{token_id}", status_code=204)
async def admin_revoke_token(
    token_id: str,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = AccessTokenRepository(conn)
    row = repo.get_by_id(token_id)
    if not row:
        raise HTTPException(status_code=404, detail="Token not found")
    repo.revoke(token_id)
    _audit(conn, user["id"], "token.admin_revoke", token_id, {"owner_id": row["user_id"]})
