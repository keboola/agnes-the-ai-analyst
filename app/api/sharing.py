"""Sharing API — owner-initiated sharing of Library items.

Endpoints:

  GET  /api/sharing/groups                                auth
  GET  /api/sharing/{resource_type}/{resource_id}         owner or admin
  PUT  /api/sharing/{resource_type}/{resource_id}         owner or admin

RBAC model: this is the **owner-scoped** counterpart to ``app/api/access.py``
(which is entirely ``require_admin``). The creator of a Library item may grant
it to groups they themselves belong to, plus ``Everyone`` (workspace-wide).
The invariants — ownership, and group containment so an owner can neither push
an item into a team they aren't in nor revoke an admin's grant — live in
``app/services/library_sharing.py``.

Fail-closed: a resource the caller doesn't own returns 404 (not 403), matching
the collections contract, so callers cannot probe for the existence of items
they have no rights over.

Shareable types are ``collection`` (artefacts: files, images, documents) and
``agent``. Skills are excluded on purpose — an approved store entity is
already visible to every authenticated user, so a grant row on one would be
read by nothing.
"""

from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth.access import is_user_admin
from app.auth.dependencies import get_current_user
from app.services.journey import mark_journey
from app.services.library_sharing import (
    SHAREABLE_TYPES,
    current_share_group_ids,
    is_shareable,
    resolve_owner,
    set_shares,
    share_targets,
    visibility_for,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sharing", tags=["sharing"])


class ShareTargetResponse(BaseModel):
    id: str
    name: str
    is_everyone: bool


class ShareStateResponse(BaseModel):
    resource_type: str
    resource_id: str
    visibility: str = Field(description="private | shared | workspace")
    group_ids: List[str] = Field(default_factory=list)


class SetSharesRequest(BaseModel):
    group_ids: List[str] = Field(
        default_factory=list,
        description="Desired end state: the groups this item should be shared with. Empty = private.",
    )


def _require_owned(resource_type: str, resource_id: str, user: dict) -> None:
    """404 unless the caller owns this resource (admins exempt).

    Also 404s an unshareable resource type, so the endpoint surface never
    reveals which types exist but aren't grant-backed.
    """
    if not is_shareable(resource_type):
        raise HTTPException(status_code=404, detail="resource_not_shareable")
    owner = resolve_owner(resource_type, resource_id)
    if owner is None:
        raise HTTPException(status_code=404, detail="resource_not_found")
    if owner != user["id"] and not is_user_admin(user["id"]):
        raise HTTPException(status_code=404, detail="resource_not_found")


@router.get("/groups", response_model=List[ShareTargetResponse])
async def list_share_targets(user: dict = Depends(get_current_user)):
    """Groups the caller may share into — their own memberships + Everyone.

    Drives the Library's Share dialog. An admin sees every group.
    """
    admin = is_user_admin(user["id"])
    return [t.as_dict() for t in share_targets(user["id"], is_admin=admin)]


@router.get("/{resource_type}/{resource_id}", response_model=ShareStateResponse)
async def get_share_state(
    resource_type: str,
    resource_id: str,
    user: dict = Depends(get_current_user),
):
    """Current sharing state of one Library item."""
    _require_owned(resource_type, resource_id, user)
    return {
        "resource_type": resource_type,
        "resource_id": resource_id,
        "visibility": visibility_for(resource_type, resource_id),
        "group_ids": sorted(current_share_group_ids(resource_type, resource_id)),
    }


@router.put("/{resource_type}/{resource_id}", response_model=ShareStateResponse)
async def put_share_state(
    resource_type: str,
    resource_id: str,
    payload: SetSharesRequest,
    user: dict = Depends(get_current_user),
):
    """Set which groups a Library item is shared with (idempotent).

    ``group_ids: []`` makes the item private again. Grants to groups outside
    the caller's shareable set are preserved — see ``set_shares``.
    """
    _require_owned(resource_type, resource_id, user)
    admin = is_user_admin(user["id"])
    try:
        result = set_shares(
            resource_type=resource_type,
            resource_id=resource_id,
            group_ids=payload.group_ids,
            actor_id=user["id"],
            is_admin=admin,
        )
    except ValueError as e:
        # Stable machine token: the caller asked for a group they may not
        # share into. 403 (not 404) — the item itself is theirs.
        raise HTTPException(status_code=403, detail=str(e)) from e
    logger.info(
        "sharing: %s %s/%s -> %s (added=%s removed=%s) by %s",
        SHAREABLE_TYPES.get(resource_type, resource_type),
        resource_type,
        resource_id,
        result["visibility"],
        result["added"],
        result["removed"],
        user["id"],
    )
    # Onboarding step "Add or share something" — the SHARE half. Only when the
    # item actually ends up shared: turning sharing back off (group_ids: []) puts
    # it at "private", which is the opposite of the milestone.
    if result["visibility"] != "private":
        mark_journey(user.get("id"), catalog_discovered=True)
    return {
        "resource_type": resource_type,
        "resource_id": resource_id,
        "visibility": result["visibility"],
        "group_ids": result["group_ids"],
    }
