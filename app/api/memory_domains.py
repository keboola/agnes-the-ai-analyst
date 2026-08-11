"""Admin REST API for Memory Domains (v49 unified stack).

Mirrors ``app/api/data_packages.py`` for ``memory_domains`` +
``knowledge_item_domains``. Section 6 of the design spec lists the endpoints:

  - ``GET    /api/admin/memory-domains``           — list + search
  - ``POST   /api/admin/memory-domains``           — create
  - ``GET    /api/admin/memory-domains/{id}``      — detail with items
  - ``PUT    /api/admin/memory-domains/{id}``      — update metadata
  - ``DELETE /api/admin/memory-domains/{id}``      — delete
  - ``POST   /api/admin/memory-domains/{id}/items``        — add item
  - ``DELETE /api/admin/memory-domains/{id}/items/{item}`` — remove item

Audit actions are ``memory_domain.create/update/delete/add_item/remove_item``
per Section 9.1.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from app.api.data_packages import _validate_color
from app.auth.access import require_admin
from app.auth.dependencies import _get_db
from src.repositories import audit_repo, knowledge_repo, memory_domains_repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/memory-domains", tags=["memory-domains"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CreateMemoryDomainRequest(BaseModel):
    name: str
    slug: str
    description: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    cover_image_url: Optional[str] = None
    status: Optional[str] = None  # v51
    # Optional first knowledge item, seeded into the new domain in the same
    # call. Without it this endpoint only ever produced an empty container —
    # which is what the Studio "Corporate Memory Builder" posts to, so a page
    # whose subtitle promises "Distill reusable knowledge into a memory
    # domain" had nowhere to put the knowledge. The item is created through
    # the same repository the regular knowledge endpoint uses, carrying this
    # domain's slug, so it is indistinguishable from one added later.
    content: Optional[str] = None
    content_title: Optional[str] = None

    @field_validator("color")
    @classmethod
    def _check_color(cls, v: Optional[str]) -> Optional[str]:
        return _validate_color(v)

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: Optional[str]) -> Optional[str]:
        from app.api.data_packages import _validate_status

        return _validate_status(v)


class UpdateMemoryDomainRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    # v50: see app/api/data_packages.py for the empty-string-means-clear
    # contract; same semantics here.
    cover_image_url: Optional[str] = None
    status: Optional[str] = None  # v51

    @field_validator("color")
    @classmethod
    def _check_color(cls, v: Optional[str]) -> Optional[str]:
        return _validate_color(v)

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: Optional[str]) -> Optional[str]:
        from app.api.data_packages import _validate_status

        return _validate_status(v)


class AddItemRequest(BaseModel):
    item_id: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _audit(
    conn: duckdb.DuckDBPyConnection,
    actor_id: str,
    action: str,
    resource: str,
    params: Optional[Dict[str, Any]] = None,
    params_before: Optional[Dict[str, Any]] = None,
) -> None:
    try:
        audit_repo().log(
            user_id=actor_id,
            action=action,
            resource=resource,
            params=params,
            params_before=params_before,
        )
    except Exception:
        logger.warning("audit log failed for %s/%s", action, resource)


def _serialize(d: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": d["id"],
        "slug": d["slug"],
        "name": d["name"],
        "description": d.get("description"),
        "icon": d.get("icon"),
        "color": d.get("color"),
        "cover_image_url": d.get("cover_image_url"),
        "status": d.get("status") or "prod",  # v51 default for legacy rows
        "created_by": d.get("created_by"),
        "created_at": d["created_at"].isoformat() if d.get("created_at") else None,
        "updated_at": d["updated_at"].isoformat() if d.get("updated_at") else None,
    }


# ---------------------------------------------------------------------------
# CRUD endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=List[Dict[str, Any]])
async def list_memory_domains_admin(
    search: Optional[str] = None,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """List all domains (admin-scoped — includes seed rows + non-canonical
    ones). The user-facing ``GET /api/memory/domains`` is the read endpoint
    for non-admins; this one ships ``created_by`` + timestamps too."""
    rows = memory_domains_repo().list(search=search)
    return [_serialize(r) for r in rows]


#: Category for items the domain builder seeds. One value, not one per
#: domain — see the note at the `category=` argument below.
SEEDED_ITEM_CATEGORY = "corporate-memory"


def seed_domain_item(
    *,
    slug: str,
    name: str,
    content: Optional[str],
    content_title: Optional[str],
    source_user: Optional[str],
) -> Optional[str]:
    """Create the domain's first knowledge item, or ``None`` when there is none.

    Shared with the suggestion-queue replay in
    ``app/api/authoring_suggestions.py``: a non-admin's submission is created
    later, by a different code path, and that path copied only name/slug/
    description — so the knowledge the author actually wrote was dropped on
    approval and an empty domain appeared instead. One function, so the two
    ways a domain gets created cannot drift again. (Devin Review on #1263.)

    The item lands ``pending`` like every other route into corporate memory
    (`POST /api/memory`): approval is what publishes an item into every
    analyst's `.claude/rules/`, and authoring it is not approving it.
    """
    if not content or not content.strip():
        return None

    import uuid as _uuid

    from src.repositories import knowledge_repo

    item_id = str(_uuid.uuid4())
    knowledge_repo().create(
        id=item_id,
        title=(content_title or name).strip(),
        content=content.strip(),
        # `category` is required on the knowledge repo and the builder form
        # does not ask for one. NOT the domain slug: categories are
        # enumerated with `SELECT DISTINCT category` to fill the admin page's
        # dropdowns (`app/web/router.py`), so a slug per domain would grow a
        # long tail of one-item categories there. The domain is already
        # recorded in `domain`; one stable label says where the item came
        # from without inventing a taxonomy. (Devin Review on #1263.)
        category=SEEDED_ITEM_CATEGORY,
        source_user=source_user,
        tags=None,
        domain=slug.strip(),
        confidence=0.50,
    )
    return item_id


@router.post("", status_code=201)
async def create_memory_domain(
    payload: CreateMemoryDomainRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = memory_domains_repo()
    if not payload.name.strip() or not payload.slug.strip():
        raise HTTPException(status_code=400, detail="name and slug are required")
    try:
        domain_id = repo.create(
            name=payload.name.strip(),
            slug=payload.slug.strip(),
            description=payload.description,
            icon=payload.icon,
            color=payload.color,
            cover_image_url=payload.cover_image_url,
            status=payload.status or "prod",
            created_by=user.get("email") or user["id"],
        )
    except duckdb.ConstraintException:
        raise HTTPException(status_code=409, detail="slug_exists")
    _audit(
        conn,
        user["id"],
        "memory_domain.create",
        f"memory_domain:{domain_id}",
        {"slug": payload.slug, "name": payload.name},
    )

    # Seed the first knowledge item when the caller supplied one. Deliberately
    # after the audit row and best-effort in nothing: a failure here must be
    # visible, because the caller asked for content and a silently empty
    # domain is the exact failure this field exists to remove. The domain
    # itself is already created and audited, so the 500 is honest — retrying
    # the item is possible, and re-POSTing the domain 409s on the slug.
    item_id: Optional[str] = seed_domain_item(
        slug=payload.slug,
        name=payload.name,
        content=payload.content,
        content_title=payload.content_title,
        source_user=user.get("email"),
    )
    if item_id:
        _audit(
            conn,
            user["id"],
            "memory_domain.seed_item",
            f"memory_domain:{domain_id}",
            {"item_id": item_id, "slug": payload.slug},
        )

    # `item_status` is stated rather than implied: the seeded item lands
    # `pending` like every other route into corporate memory, because approval
    # is what publishes an item into every analyst's `.claude/rules/` and
    # authoring is not approving. An admin who wrote it still has to approve
    # it, and the builder now says so instead of leaving them to notice.
    # (Devin Review on #1263 asked whether pending was intended — it is.)
    return {"id": domain_id, "item_id": item_id, "item_status": "pending" if item_id else None}


@router.get("/{domain_id}")
async def get_memory_domain(
    domain_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Detail view including the list of items tagged with this domain."""
    repo = memory_domains_repo()
    domain = repo.get(domain_id)
    if not domain:
        raise HTTPException(status_code=404, detail="memory_domain_not_found")
    items = repo.list_items_of_domain(domain_id)
    out = _serialize(domain)
    out["items"] = items
    return out


@router.put("/{domain_id}")
async def update_memory_domain(
    domain_id: str,
    payload: UpdateMemoryDomainRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = memory_domains_repo()
    existing = repo.get(domain_id)
    if not existing:
        raise HTTPException(status_code=404, detail="memory_domain_not_found")
    before = {
        "name": existing.get("name"),
        "description": existing.get("description"),
        "icon": existing.get("icon"),
        "color": existing.get("color"),
        "cover_image_url": existing.get("cover_image_url"),
        "status": existing.get("status"),
    }
    clear_cover = payload.cover_image_url == ""
    repo.update(
        domain_id,
        name=payload.name,
        description=payload.description,
        icon=payload.icon,
        color=payload.color,
        cover_image_url=None if clear_cover else payload.cover_image_url,
        clear_cover_image=clear_cover,
        status=payload.status,
    )
    fresh = repo.get(domain_id)
    after = {
        "name": fresh.get("name") if fresh else None,
        "description": fresh.get("description") if fresh else None,
        "icon": fresh.get("icon") if fresh else None,
        "color": fresh.get("color") if fresh else None,
        "cover_image_url": fresh.get("cover_image_url") if fresh else None,
        "status": fresh.get("status") if fresh else None,
    }
    _audit(
        conn,
        user["id"],
        "memory_domain.update",
        f"memory_domain:{domain_id}",
        {"after": after},
        params_before={"before": before},
    )
    return _serialize(fresh) if fresh else {"id": domain_id}


@router.delete("/{domain_id}", status_code=204)
async def delete_memory_domain(
    domain_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = memory_domains_repo()
    existing = repo.get(domain_id)
    if not existing:
        raise HTTPException(status_code=404, detail="memory_domain_not_found")
    items_count = len(repo.list_items_of_domain(domain_id))
    repo.delete(domain_id)
    _audit(
        conn,
        user["id"],
        "memory_domain.delete",
        f"memory_domain:{domain_id}",
        {"slug": existing.get("slug"), "items_count": items_count},
    )


@router.post("/{domain_id}/restore", status_code=200)
async def restore_memory_domain(
    domain_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """v54 undo: reverse a soft delete. Idempotent."""
    repo = memory_domains_repo()
    existing = repo.get(domain_id, include_deleted=True)
    if not existing:
        raise HTTPException(status_code=404, detail="memory_domain_not_found")
    repo.restore(domain_id)
    _audit(
        conn,
        user["id"],
        "memory_domain.restore",
        f"memory_domain:{domain_id}",
        {"slug": existing.get("slug")},
    )
    return {"id": domain_id, "restored": True}


# ---------------------------------------------------------------------------
# Junction endpoints — add/remove items
# ---------------------------------------------------------------------------


@router.post("/{domain_id}/items")
async def add_item_to_domain(
    domain_id: str,
    payload: AddItemRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = memory_domains_repo()
    if not repo.get(domain_id):
        raise HTTPException(status_code=404, detail="memory_domain_not_found")
    item = knowledge_repo().get_by_id(payload.item_id)
    if not item:
        raise HTTPException(status_code=404, detail="item_not_found")
    added = repo.add_item(domain_id, payload.item_id, added_by=user.get("email") or user["id"])
    if added:
        _audit(
            conn,
            user["id"],
            "memory_domain.add_item",
            f"memory_domain:{domain_id}",
            {"item_id": payload.item_id},
        )
    return {"added": added}


@router.delete("/{domain_id}/items/{item_id}", status_code=204)
async def remove_item_from_domain(
    domain_id: str,
    item_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = memory_domains_repo()
    if not repo.get(domain_id):
        raise HTTPException(status_code=404, detail="memory_domain_not_found")
    removed = repo.remove_item(domain_id, item_id)
    if removed:
        _audit(
            conn,
            user["id"],
            "memory_domain.remove_item",
            f"memory_domain:{domain_id}",
            {"item_id": item_id},
        )
