"""Corporate memory endpoints — knowledge items, voting, governance admin, contradictions."""

import uuid
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import duckdb

from app.auth.dependencies import get_current_user, require_role, Role, _get_db
from src.repositories.knowledge import KnowledgeRepository
from src.repositories.audit import AuditRepository

router = APIRouter(prefix="/api/memory", tags=["memory"])

VALID_STATUSES = ["pending", "approved", "mandatory", "rejected", "revoked", "expired"]
VALID_DOMAINS = ["finance", "engineering", "product", "data", "operations", "infrastructure"]


class CreateKnowledgeRequest(BaseModel):
    title: str
    content: str
    category: str
    tags: Optional[List[str]] = None
    domain: Optional[str] = None
    entities: Optional[List[str]] = None


class VoteRequest(BaseModel):
    vote: int


class PersonalFlagRequest(BaseModel):
    is_personal: bool


class AdminActionRequest(BaseModel):
    reason: Optional[str] = None
    audience: Optional[str] = None


class EditRequest(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None


class BatchActionRequest(BaseModel):
    item_ids: List[str]
    action: str  # approve, reject, mandate, revoke
    reason: Optional[str] = None
    audience: Optional[str] = None


class ResolveContradictionRequest(BaseModel):
    resolution: str  # kept_a, kept_b, merged, both_valid


# ---- User endpoints ----

@router.get("")
async def list_knowledge(
    status_filter: Optional[str] = None,
    category: Optional[str] = None,
    domain: Optional[str] = None,
    source_type: Optional[str] = None,
    search: Optional[str] = None,
    exclude_personal: bool = True,
    page: int = 1,
    per_page: int = 50,
    sort: str = "updated_at",
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """List knowledge items with filtering, pagination, search."""
    repo = KnowledgeRepository(conn)
    page = max(page, 1)
    offset = (page - 1) * per_page
    if search:
        items = repo.search(search)
    else:
        statuses = [status_filter] if status_filter else None
        items = repo.list_items(
            statuses=statuses,
            category=category,
            domain=domain,
            source_type=source_type,
            exclude_personal=exclude_personal,
            limit=per_page,
            offset=offset,
        )

    # Enrich with votes
    for item in items:
        votes = repo.get_votes(item["id"])
        item["upvotes"] = votes["upvotes"]
        item["downvotes"] = votes["downvotes"]
        item["score"] = votes["upvotes"] - votes["downvotes"]

    return {"items": items, "count": len(items), "page": page, "per_page": per_page}


@router.get("/stats")
async def get_stats(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Get corporate memory statistics."""
    repo = KnowledgeRepository(conn)
    all_items = repo.list_items(limit=10000)
    status_counts: dict = {}
    categories: set = set()
    domains: dict = {}
    source_types: dict = {}
    for item in all_items:
        s = item.get("status", "unknown")
        status_counts[s] = status_counts.get(s, 0) + 1
        if item.get("category"):
            categories.add(item["category"])
        d = item.get("domain") or "unset"
        domains[d] = domains.get(d, 0) + 1
        st = item.get("source_type") or "unknown"
        source_types[st] = source_types.get(st, 0) + 1
    return {
        "total": len(all_items),
        "by_status": status_counts,
        "categories": sorted(categories),
        "by_domain": domains,
        "by_source_type": source_types,
    }


@router.post("", status_code=201)
async def create_knowledge(
    request: CreateKnowledgeRequest,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = KnowledgeRepository(conn)
    item_id = str(uuid.uuid4())
    repo.create(
        id=item_id,
        title=request.title,
        content=request.content,
        category=request.category,
        source_user=user.get("email"),
        tags=request.tags,
        domain=request.domain,
        entities=request.entities,
        confidence=0.50,
    )
    return {"id": item_id, "status": "pending"}


@router.post("/{item_id}/vote")
async def vote_knowledge(
    item_id: str,
    request: VoteRequest,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    if request.vote not in (1, -1):
        raise HTTPException(status_code=400, detail="Vote must be 1 or -1")
    repo = KnowledgeRepository(conn)
    if not repo.get_by_id(item_id):
        raise HTTPException(status_code=404, detail="Knowledge item not found")
    repo.vote(item_id, user["id"], request.vote)
    return repo.get_votes(item_id)


@router.get("/my-votes")
async def get_my_votes(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Get current user's votes on all items."""
    results = conn.execute(
        "SELECT item_id, vote FROM knowledge_votes WHERE user_id = ?", [user["id"]]
    ).fetchall()
    return {row[0]: row[1] for row in results}


@router.get("/my-contributions")
async def get_my_contributions(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Get knowledge items contributed by the current user."""
    repo = KnowledgeRepository(conn)
    email = user.get("email", "")
    items = repo.get_user_contributions(email)
    for item in items:
        votes = repo.get_votes(item["id"])
        item["upvotes"] = votes["upvotes"]
        item["downvotes"] = votes["downvotes"]
        item["score"] = votes["upvotes"] - votes["downvotes"]
    return {"items": items, "count": len(items)}


@router.post("/{item_id}/personal")
async def toggle_personal_flag(
    item_id: str,
    request: PersonalFlagRequest,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Toggle personal/excluded flag on a knowledge item (only by the contributor)."""
    repo = KnowledgeRepository(conn)
    item = repo.get_by_id(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Knowledge item not found")
    if item.get("source_user") != user.get("email"):
        raise HTTPException(status_code=403, detail="Only the contributor can flag personal items")
    repo.set_personal(item_id, request.is_personal)
    return {"id": item_id, "is_personal": request.is_personal}


@router.get("/{item_id}/provenance")
async def get_provenance(
    item_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Get source provenance for a knowledge item."""
    repo = KnowledgeRepository(conn)
    item = repo.get_by_id(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Knowledge item not found")
    return {
        "id": item_id,
        "source_type": item.get("source_type"),
        "source_ref": item.get("source_ref"),
        "source_user": item.get("source_user"),
        "confidence": item.get("confidence"),
        "domain": item.get("domain"),
        "entities": item.get("entities"),
        "valid_from": item.get("valid_from"),
        "valid_until": item.get("valid_until"),
        "supersedes": item.get("supersedes"),
        "created_at": item.get("created_at"),
    }


# ---- Admin governance endpoints ----

def _get_item_or_404(repo: KnowledgeRepository, item_id: str) -> dict:
    item = repo.get_by_id(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Knowledge item not found")
    return item


def _audit_action(conn, admin_email: str, action: str, item_id: str, details: dict = None):
    audit = AuditRepository(conn)
    audit.log(user_id=admin_email, action=f"km_{action}", resource=item_id, params=details)


@router.post("/admin/approve")
async def admin_approve(
    item_id: str,
    user: dict = Depends(require_role(Role.KM_ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = KnowledgeRepository(conn)
    _get_item_or_404(repo, item_id)
    repo.update_status(item_id, "approved")
    _audit_action(conn, user["email"], "approve", item_id)
    return {"id": item_id, "status": "approved"}


@router.post("/admin/reject")
async def admin_reject(
    item_id: str,
    request: AdminActionRequest,
    user: dict = Depends(require_role(Role.KM_ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = KnowledgeRepository(conn)
    _get_item_or_404(repo, item_id)
    repo.update_status(item_id, "rejected")
    _audit_action(conn, user["email"], "reject", item_id, {"reason": request.reason})
    return {"id": item_id, "status": "rejected"}


@router.post("/admin/mandate")
async def admin_mandate(
    item_id: str,
    request: AdminActionRequest,
    user: dict = Depends(require_role(Role.KM_ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = KnowledgeRepository(conn)
    _get_item_or_404(repo, item_id)
    repo.update_status(item_id, "mandatory")
    _audit_action(conn, user["email"], "mandate", item_id, {
        "reason": request.reason, "audience": request.audience,
    })
    return {"id": item_id, "status": "mandatory"}


@router.post("/admin/revoke")
async def admin_revoke(
    item_id: str,
    request: AdminActionRequest,
    user: dict = Depends(require_role(Role.KM_ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = KnowledgeRepository(conn)
    _get_item_or_404(repo, item_id)
    repo.update_status(item_id, "revoked")
    _audit_action(conn, user["email"], "revoke", item_id, {"reason": request.reason})
    return {"id": item_id, "status": "revoked"}


@router.post("/admin/edit")
async def admin_edit(
    item_id: str,
    request: EditRequest,
    user: dict = Depends(require_role(Role.KM_ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = KnowledgeRepository(conn)
    _get_item_or_404(repo, item_id)
    updates = {}
    if request.title is not None:
        updates["title"] = request.title
    if request.content is not None:
        updates["content"] = request.content
    if updates:
        repo.update(item_id, **updates)
    _audit_action(conn, user["email"], "edit", item_id, updates)
    return {"id": item_id, "updated": list(updates.keys())}


@router.post("/admin/batch")
async def admin_batch(
    request: BatchActionRequest,
    user: dict = Depends(require_role(Role.KM_ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Batch governance action on multiple items."""
    repo = KnowledgeRepository(conn)
    action_map = {
        "approve": "approved",
        "reject": "rejected",
        "mandate": "mandatory",
        "revoke": "revoked",
    }
    if request.action not in action_map:
        raise HTTPException(status_code=400, detail=f"Invalid action: {request.action}")

    new_status = action_map[request.action]
    results = {"success": [], "not_found": []}
    for item_id in request.item_ids:
        item = repo.get_by_id(item_id)
        if not item:
            results["not_found"].append(item_id)
            continue
        repo.update_status(item_id, new_status)
        _audit_action(conn, user["email"], request.action, item_id, {
            "reason": request.reason, "audience": request.audience, "batch": True,
        })
        results["success"].append(item_id)

    return results


@router.get("/admin/pending")
async def admin_pending(
    category: Optional[str] = None,
    page: int = 1,
    per_page: int = 50,
    user: dict = Depends(require_role(Role.KM_ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Get pending items queue for admin review."""
    repo = KnowledgeRepository(conn)
    offset = (page - 1) * per_page
    items = repo.list_items(statuses=["pending"], category=category, limit=per_page, offset=offset)
    return {"items": items, "count": len(items)}


@router.get("/admin/audit")
async def admin_audit(
    page: int = 1,
    per_page: int = 50,
    action: Optional[str] = None,
    user: dict = Depends(require_role(Role.KM_ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Get governance audit log."""
    audit = AuditRepository(conn)
    # Filter km_ prefixed actions
    km_action = f"km_{action}" if action else None
    entries = audit.query(action=km_action, limit=per_page)
    if not km_action:
        # Get all km_ actions
        entries = conn.execute(
            "SELECT * FROM audit_log WHERE action LIKE 'km_%' ORDER BY timestamp DESC LIMIT ?",
            [per_page],
        ).fetchall()
        if entries:
            columns = [desc[0] for desc in conn.description]
            entries = [dict(zip(columns, row)) for row in entries]
        else:
            entries = []
    return {"entries": entries, "count": len(entries)}


# ---- Admin contradiction endpoints ----

@router.get("/admin/contradictions")
async def admin_contradictions(
    resolved: Optional[bool] = None,
    user: dict = Depends(require_role(Role.KM_ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """List knowledge contradictions for admin review."""
    repo = KnowledgeRepository(conn)
    contradictions = repo.list_contradictions(resolved=resolved)
    # Enrich with item details
    for c in contradictions:
        c["item_a"] = repo.get_by_id(c["item_a_id"])
        c["item_b"] = repo.get_by_id(c["item_b_id"])
    return {"contradictions": contradictions, "count": len(contradictions)}


@router.post("/admin/contradictions/{contradiction_id}/resolve")
async def admin_resolve_contradiction(
    contradiction_id: str,
    request: ResolveContradictionRequest,
    user: dict = Depends(require_role(Role.KM_ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Resolve a knowledge contradiction."""
    repo = KnowledgeRepository(conn)
    contradiction = repo.get_contradiction(contradiction_id)
    if not contradiction:
        raise HTTPException(status_code=404, detail="Contradiction not found")
    if contradiction.get("resolved"):
        raise HTTPException(status_code=400, detail="Contradiction already resolved")

    valid_resolutions = ["kept_a", "kept_b", "merged", "both_valid"]
    if request.resolution not in valid_resolutions:
        raise HTTPException(
            status_code=400,
            detail=f"Resolution must be one of: {valid_resolutions}",
        )

    repo.resolve_contradiction(contradiction_id, user["email"], request.resolution)
    _audit_action(conn, user["email"], "resolve_contradiction", contradiction_id, {
        "resolution": request.resolution,
        "item_a_id": contradiction["item_a_id"],
        "item_b_id": contradiction["item_b_id"],
    })
    return {"id": contradiction_id, "resolved": True, "resolution": request.resolution}
