"""User stack API — subscribe / unsubscribe / list (auto-membership model).

Three user-facing endpoints under ``/api/stack``:

  - ``GET    /api/stack?type=data_package|memory_domain`` — user's effective
    stack (auto: every grant on the caller's groups, required or available)
  - ``POST   /api/stack/subscribe``                       — download a local
    copy of an ``available`` resource already in the stack
  - ``DELETE /api/stack/subscription/{type}/{id}``        — remove the local
    copy (the resource stays in the stack, still queryable server-side)

Stack resolution is delegated to ``app/services/stack_resolver.py``. Required
grants are always both in-stack and materialized (downloaded); available
grants are auto-in-stack but only materialized once subscribed. The resolver
raises HTTPException directly for the two business-rule errors
(``already_required`` on subscribe, ``cannot_remove_required`` on
unsubscribe) — required resources are always downloaded, so there's nothing
to opt in/out of.

Server-side telemetry — ``stack.subscribe`` / ``stack.unsubscribe`` events
land in ``usage_events`` via ``UsageRepository.emit_server_event``.
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


def _reject_co_session(user: object) -> None:
    """Stack management is per-user — a co-session principal has no single
    identity to subscribe with, and ``user["id"]`` would blow up on the
    dataclass anyway. Every /api/stack endpoint must call this first."""
    from app.auth.session_principal import SessionPrincipal

    if isinstance(user, SessionPrincipal):
        raise HTTPException(403, "co_session cannot manage stack")


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

    Auto-membership: effective stack = required ∪ available — every grant on
    the caller's groups, no subscription needed. Every item is
    ``in_stack: true``; ``materialized`` additionally flags whether it's ALSO
    kept as a local copy (`agnes pull` downloads it) — always true for
    required, true for available only once subscribed.
    """
    _reject_co_session(user)
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
            "materialized": e.materialized,
        }
        for e in resolver.stack(user["id"], rt)
    ]
    return {"items": items}


@router.get("/browse")
async def browse_stack(
    type: str,
    user: dict = Depends(get_current_user),
):
    """List every resource of ``type`` the caller could see (RBAC-granted).

    Auto-membership means this is now equivalent in scope to ``GET
    /api/stack`` (required + available, no separate opt-in tier) — it's kept
    as a discovery surface so callers who want the full candidate set (issue
    #621) don't have to reason about the difference. Each item carries
    ``in_stack: true`` and a ``materialized`` flag so an analyst's Claude can
    tell what is already downloaded locally vs. only server-side queryable.

    Scoped per-user by ``StackResolver.browse`` (group grants only), so the
    only authorization gate is authentication — no extra RBAC dependency.
    """
    _reject_co_session(user)
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
            "materialized": e.materialized,
        }
        for e in resolver.browse(user["id"], rt)
    ]
    return {"items": items}


@router.post("/subscribe")
async def subscribe(
    payload: SubscribeRequest,
    user: dict = Depends(get_current_user),
):
    """Download a local copy of an ``available`` resource already in the
    stack. Refuses to subscribe if the resource is required (it's always
    downloaded automatically — clients shouldn't bother)."""
    _reject_co_session(user)
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
    """Remove the local copy of an ``available`` resource — the resource
    stays in the stack (still queryable server-side), only the download is
    dropped. Returns 400 ``cannot_remove_required`` when the resource is
    required for any of the user's groups (required is always downloaded,
    no opt-out).

    Returns 204 No Content on success — DELETE idempotency convention
    enforced by the API design rules test. Callers should treat 204 as
    "local copy removed", 400 + ``cannot_remove_required`` as "still
    downloaded because Required tier blocks opt-out".
    """
    _reject_co_session(user)
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
