"""Access request API — users request access, admins approve/deny."""

import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
import duckdb

from app.auth.access import require_admin
from app.auth.dependencies import get_current_user, _get_db
from src.repositories.access_requests import AccessRequestRepository

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/access-requests", tags=["access-requests"])


class AccessRequestCreate(BaseModel):
    table_id: str
    reason: Optional[str] = ""


@router.post("", status_code=201)
async def create_request(
    request: AccessRequestCreate,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Submit an access request for a table."""
    repo = AccessRequestRepository(conn)

    # Check for duplicate pending request
    if repo.has_pending_request(user["id"], request.table_id):
        raise HTTPException(status_code=409, detail="You already have a pending request for this table")

    req_id = repo.create(
        user_id=user["id"],
        user_email=user.get("email", ""),
        table_id=request.table_id,
        reason=request.reason or "",
    )
    return {"id": req_id, "status": "pending", "table_id": request.table_id}


@router.get("/my")
async def my_requests(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """List current user's access requests."""
    repo = AccessRequestRepository(conn)
    requests = repo.list_by_user(user["id"])
    # Serialize timestamps
    for r in requests:
        for k in ("created_at", "reviewed_at"):
            if r.get(k) and hasattr(r[k], "isoformat"):
                r[k] = r[k].isoformat()
    return {"requests": requests}


@router.get("/pending")
async def pending_requests(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """List all pending access requests (admin only)."""
    repo = AccessRequestRepository(conn)
    requests = repo.list_pending()
    for r in requests:
        for k in ("created_at", "reviewed_at"):
            if r.get(k) and hasattr(r[k], "isoformat"):
                r[k] = r[k].isoformat()
    return {"requests": requests, "count": len(requests)}


@router.post("/{request_id}/approve")
async def approve_request(
    request_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Approve an access request (admin only). Auto-grants permission."""
    repo = AccessRequestRepository(conn)
    if repo.approve(request_id, reviewed_by=user.get("email", "")):
        return {"status": "approved", "id": request_id}
    raise HTTPException(status_code=404, detail="Request not found or already processed")


@router.post("/{request_id}/deny")
async def deny_request(
    request_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Deny an access request (admin only)."""
    repo = AccessRequestRepository(conn)
    if repo.deny(request_id, reviewed_by=user.get("email", "")):
        return {"status": "denied", "id": request_id}
    raise HTTPException(status_code=404, detail="Request not found or already processed")
