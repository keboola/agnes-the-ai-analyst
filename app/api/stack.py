"""User stack API — subscribe / unsubscribe / list (v49 unified stack).

Section 6 of the design spec covers the three user-facing endpoints under
``/api/stack``:

  - ``GET    /api/stack?type=data_package|memory_domain`` — user's effective stack
  - ``POST   /api/stack/subscribe``                       — opt-in to an available grant
  - ``DELETE /api/stack/subscription/{type}/{id}``        — opt-out

Stack resolution is delegated to ``app/services/stack_resolver.py``. Required
grants beat available + subscription; the resolver raises HTTPException
directly for the two business-rule errors (``already_required`` on subscribe,
``cannot_remove_required`` on unsubscribe).

Server-side telemetry (Section 9.2) — ``stack.subscribe`` / ``stack.unsubscribe``
events land in ``usage_events`` via ``UsageRepository.emit_server_event``.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.access import can_access
from app.auth.dependencies import get_current_user
from app.resource_types import ResourceType
from app.services.stack_resolver import StackResolver

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stack", tags=["stack"])


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class SubscribeRequest(BaseModel):
    resource_type: str
    resource_id: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_type(value: str) -> ResourceType:
    """Resolve a string into the ResourceType enum, restricted to types the
    StackResolver supports. Marketplace plugins are explicitly excluded
    (design D1 — they keep their own resolver)."""
    try:
        rt = ResourceType(value)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"unknown_resource_type:{value}",
        )
    if rt not in (ResourceType.DATA_PACKAGE, ResourceType.MEMORY_DOMAIN):
        raise HTTPException(
            status_code=400,
            detail=f"unsupported_stack_type:{rt.value}",
        )
    return rt


def _emit_event(
    *,
    event_type: str,
    user: dict,
    props: dict,
) -> None:
    """Fire-and-forget — telemetry must never break the user's action."""
    try:
        from src.repositories import usage_repo
        usage_repo().emit_server_event(
            event_type=event_type,
            user_id=user["id"],
            username=user.get("email") or user["id"],
            props=props,
        )
    except Exception:
        logger.warning("usage_events emit failed for %s", event_type)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("")
async def list_stack(
    type: str,
    user: dict = Depends(get_current_user),
):
    """Return the user's effective stack for the given resource type.

    Effective stack = required ∪ (subscribed ∩ available). Required entries
    always count as in_stack; available entries only if the user has a
    subscription row.
    """
    rt = _validate_type(type)
    resolver = StackResolver()
    items = [
        {
            "id": e.id,
            "name": e.name,
            "description": e.description,
            "icon": e.icon,
            "color": e.color,
            "requirement": e.requirement,
            "in_stack": e.in_stack,
        }
        for e in resolver.stack(user["id"], rt)
    ]
    return {"items": items}


@router.post("/subscribe")
async def subscribe(
    payload: SubscribeRequest,
    user: dict = Depends(get_current_user),
):
    """Opt in to an ``available`` grant. Refuses to subscribe if the resource
    is required (it's already in the stack — clients shouldn't bother)."""
    rt = _validate_type(payload.resource_type)
    # The user must have *some* grant on the resource — otherwise this is a
    # 403 (you can't subscribe to something you can't access). can_access
    # short-circuits for admins, which is the intended behavior.
    if not can_access(user["id"], rt.value, payload.resource_id):
        raise HTTPException(status_code=403, detail="no_grant")
    resolver = StackResolver()
    try:
        resolver.add_to_stack(user["id"], rt, payload.resource_id)
    except HTTPException:
        raise
    _emit_event(
        event_type="stack.subscribe",
        user=user,
        props={
            "resource_type": rt.value,
            "resource_id": payload.resource_id,
        },
    )
    return {"subscribed": True}


@router.delete("/subscription/{resource_type}/{resource_id}", status_code=204)
async def unsubscribe(
    resource_type: str,
    resource_id: str,
    user: dict = Depends(get_current_user),
):
    """Opt out of an ``available`` grant. Returns 400 ``cannot_remove_required``
    when the resource is required for any of the user's groups.

    Returns 204 No Content on success — DELETE idempotency convention
    enforced by the API design rules test. Callers should treat 204 as
    "removed", 400 + ``cannot_remove_required`` as "still subscribed
    because Required tier blocks opt-out".
    """
    rt = _validate_type(resource_type)
    resolver = StackResolver()
    try:
        resolver.remove_from_stack(user["id"], rt, resource_id)
    except HTTPException:
        raise
    _emit_event(
        event_type="stack.unsubscribe",
        user=user,
        props={
            "resource_type": rt.value,
            "resource_id": resource_id,
        },
    )
