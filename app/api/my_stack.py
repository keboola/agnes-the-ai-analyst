"""Per-user composition view of the served Claude Code marketplace.

Provides:

  * ``GET  /api/my-stack``                                 — combined view
  * ``PUT  /api/my-stack/curated/{marketplace_id}/{plugin}`` — toggle opt-out

Backs the ``/my-ai-stack`` web page. Both endpoints touch the same caches as
the Store endpoints (ETag invalidation) so any change here propagates to
``/marketplace.zip`` + ``/marketplace.git/`` on the next request.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.dependencies import _get_db, get_current_user
from src.marketplace_filter import resolve_allowed_plugins
from src.repositories.audit import AuditRepository
from src.repositories.store_entities import StoreEntitiesRepository
from src.repositories.user_curated_subscriptions import (
    UserCuratedSubscriptionsRepository,
)
from src.repositories.user_store_installs import UserStoreInstallsRepository
from src.store_naming import suffixed_name

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/my-stack", tags=["my-stack"])


class CuratedPlugin(BaseModel):
    marketplace_id: str
    marketplace_slug: str
    plugin_name: str
    manifest_name: str
    description: Optional[str] = None
    version: Optional[str] = None
    enabled: bool


class StoreInstallEntry(BaseModel):
    entity_id: str
    type: str
    name: str
    description: Optional[str] = None
    category: Optional[str] = None
    version: str
    owner_user_id: str
    owner_username: str
    invocation_name: str
    install_count: int
    photo_url: Optional[str] = None
    installed_at: Optional[str] = None
    # v35: surface visibility so my_ai_stack.html can render an
    # "Archived by owner" badge on cards whose owner soft-deleted the
    # entity. Bundle still serves to existing installs (per
    # UserStoreInstallsRepository.list_for_user filter).
    visibility_status: Optional[str] = None


class MyStackResponse(BaseModel):
    curated: List[CuratedPlugin]
    store: List[StoreInstallEntry]


class ToggleRequest(BaseModel):
    enabled: bool


class OkResponse(BaseModel):
    ok: bool = True


def _to_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _audit(
    conn: duckdb.DuckDBPyConnection,
    actor_id: str,
    action: str,
    target: str,
    params: Optional[dict] = None,
) -> None:
    try:
        AuditRepository(conn).log(
            user_id=actor_id, action=action, resource=target, params=params
        )
    except Exception:
        pass


@router.get("", response_model=MyStackResponse)
async def get_my_stack(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Combined view of admin-curated plugins (with current opt-out state)
    and Store entities the caller has installed.
    """
    granted = resolve_allowed_plugins(conn, user)
    # Model B (v28+): explicit subscriptions decide what's enabled.
    # `enabled` mirrors the legacy "not opted_out" UX so the existing toggle
    # remains semantically intuitive in /my-ai-stack.
    subs = UserCuratedSubscriptionsRepository(conn).subscribed_set(user["id"])

    curated: List[CuratedPlugin] = []
    for p in granted:
        is_subscribed = (p["marketplace_id"], p["original_name"]) in subs
        curated.append(
            CuratedPlugin(
                marketplace_id=p["marketplace_id"],
                marketplace_slug=p["marketplace_slug"],
                plugin_name=p["original_name"],
                manifest_name=p["manifest_name"],
                description=p["raw"].get("description"),
                version=p.get("version"),
                enabled=is_subscribed,
            )
        )

    installs = UserStoreInstallsRepository(conn).list_for_user(user["id"])
    store_items: List[StoreInstallEntry] = []
    for row in installs:
        photo_url = (
            f"/api/store/entities/{row['id']}/photo" if row.get("photo_path") else None
        )
        store_items.append(
            StoreInstallEntry(
                entity_id=row["id"],
                type=row["type"],
                name=row["name"],
                description=row.get("description"),
                category=row.get("category"),
                version=row["version"],
                owner_user_id=row["owner_user_id"],
                owner_username=row["owner_username"],
                invocation_name=suffixed_name(row["name"], row["owner_username"]),
                install_count=int(row.get("install_count") or 0),
                photo_url=photo_url,
                installed_at=_to_iso(row.get("installed_at")),
                visibility_status=row.get("visibility_status") or "approved",
            )
        )

    return MyStackResponse(curated=curated, store=store_items)


@router.put(
    "/curated/{marketplace_id}/{plugin_name}",
    response_model=OkResponse,
)
async def toggle_curated(
    marketplace_id: str,
    plugin_name: str,
    body: ToggleRequest,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Toggle subscribe/unsubscribe for a single admin-granted plugin.

    UI thinks in terms of *enabled* (default off in Model B). v28+ the
    repository stores *subscribed* rows (presence = enabled in served set);
    ``enabled=true`` writes a row, ``enabled=false`` removes it.
    """
    # Sanity: caller must actually have the plugin granted (otherwise the
    # toggle is meaningless and would just leak rows for ungranted plugins).
    granted = resolve_allowed_plugins(conn, user)
    has_grant = any(
        p["marketplace_id"] == marketplace_id and p["original_name"] == plugin_name
        for p in granted
    )
    if not has_grant:
        raise HTTPException(status_code=404, detail="grant_not_found")

    repo = UserCuratedSubscriptionsRepository(conn)
    if body.enabled:
        repo.subscribe(user["id"], marketplace_id, plugin_name)
    else:
        repo.unsubscribe(user["id"], marketplace_id, plugin_name)
    _audit(
        conn,
        user["id"],
        "my_stack.curated.toggle",
        f"plugin:{marketplace_id}/{plugin_name}",
        {"enabled": body.enabled},
    )

    try:
        from app.marketplace_server import packager
        packager.invalidate_etag_cache()
    except Exception:
        logger.exception("failed to invalidate marketplace etag cache")

    return OkResponse()
