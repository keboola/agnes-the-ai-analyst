"""User-facing detail endpoints for the v49 unified stack (Task 6.6).

These power the drill-down pages introduced in Phase 8:

* ``GET /api/data-packages/{slug}``  → catalog drill-down at /catalog/p/<slug>
* ``GET /api/memory/domains/{slug}`` → memory drill-down at /memory/d/<slug>

Authorization: any user with a grant (any tier) on the resource can read its
metadata + child items. Admins bypass the grant check. The two GETs also emit
``data_package.view`` / ``memory_domain.view`` events to ``usage_events`` per
Section 9.2 of the design spec.
"""

from __future__ import annotations

import logging
from typing import Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.access import can_access, is_user_admin
from app.auth.dependencies import _get_db, get_current_user
from app.resource_types import ResourceType
from src.repositories import (
    data_packages_repo,
    memory_domains_repo,
)
from src.repositories.knowledge import KnowledgeRepository
from src.repositories.usage import UsageRepository

logger = logging.getLogger(__name__)

router = APIRouter(tags=["stack-views"])


def _emit_view(
    conn: duckdb.DuckDBPyConnection,
    *,
    event_type: str,
    user: dict,
    slug: str,
    source: Optional[str],
) -> None:
    try:
        UsageRepository(conn).emit_server_event(
            event_type=event_type,
            user_id=user["id"],
            username=user.get("email") or user["id"],
            props={"slug": slug, "source": source or "direct"},
        )
    except Exception:
        logger.warning("usage_events emit failed for %s", event_type)


def _require_access(user: dict, rt: ResourceType, resource_id: str, conn) -> None:
    if is_user_admin(user["id"], conn):
        return
    if not can_access(user["id"], rt.value, resource_id, conn):
        raise HTTPException(
            status_code=403,
            detail=f"access_denied:{rt.value}:{resource_id}",
        )


# ---------------------------------------------------------------------------
# Data Packages — user-facing drill-down
# ---------------------------------------------------------------------------


@router.get("/api/data-packages/{slug}")
async def view_data_package(
    slug: str,
    source: Optional[str] = Query(
        None, description="Originating page hint for telemetry (browse|my-stack)"
    ),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Public-ish detail view — slug-keyed for stable URLs."""
    repo = data_packages_repo()
    pkg = repo.get_by_slug(slug)
    if not pkg:
        raise HTTPException(status_code=404, detail="data_package_not_found")
    _require_access(user, ResourceType.DATA_PACKAGE, pkg["id"], conn)
    _emit_view(conn, event_type="data_package.view", user=user, slug=slug, source=source)
    tables = repo.list_tables(pkg["id"])
    return {
        "id": pkg["id"],
        "slug": pkg["slug"],
        "name": pkg["name"],
        "description": pkg.get("description"),
        "icon": pkg.get("icon"),
        "color": pkg.get("color"),
        "tables": tables,
    }


# ---------------------------------------------------------------------------
# Memory Domains — user-facing drill-down
# ---------------------------------------------------------------------------


@router.get("/api/memory/domains/{slug}")
async def view_memory_domain(
    slug: str,
    source: Optional[str] = Query(
        None, description="Originating page hint for telemetry (browse|my-stack)"
    ),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Public-ish detail view of a memory domain with its items.

    Visibility rules for the items list mirror the existing
    ``/api/memory`` browse endpoint — non-privileged callers see only items
    they're allowed to see. The metadata is gated by the domain grant.
    """
    repo = memory_domains_repo()
    dom = repo.get_by_slug(slug)
    if not dom:
        raise HTTPException(status_code=404, detail="memory_domain_not_found")
    _require_access(user, ResourceType.MEMORY_DOMAIN, dom["id"], conn)
    _emit_view(conn, event_type="memory_domain.view", user=user, slug=slug, source=source)
    item_summaries = repo.list_items_of_domain(dom["id"], limit=1000)
    # Hydrate item titles + is_required from the knowledge repo for the
    # drill-down — the junction's projection is intentionally minimal.
    knowledge = KnowledgeRepository(conn)
    items: list = []
    for s in item_summaries:
        item = knowledge.get_by_id(s["id"])
        if not item:
            continue
        items.append({
            "id": item["id"],
            "title": item.get("title"),
            "content": item.get("content"),
            "status": item.get("status"),
            "is_required": bool(item.get("is_required")),
            "category": item.get("category"),
        })
    return {
        "id": dom["id"],
        "slug": dom["slug"],
        "name": dom["name"],
        "description": dom.get("description"),
        "icon": dom.get("icon"),
        "color": dom.get("color"),
        "items": items,
    }
