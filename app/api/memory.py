"""Corporate memory endpoints — knowledge items, voting, governance admin, contradictions."""

import asyncio
import json
import logging
import uuid
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import duckdb

from app.auth.dependencies import get_current_user, _get_db
from app.auth.access import require_admin, is_user_admin
from src.repositories.knowledge import KnowledgeRepository
from src.repositories.audit import AuditRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/memory", tags=["memory"])

VALID_STATUSES = ["pending", "approved", "mandatory", "rejected", "revoked", "expired"]

BUNDLE_TOKEN_BUDGET = 6000
# Rough chars-per-token estimate (conservative).
_CHARS_PER_TOKEN = 4
VALID_DOMAINS = ["finance", "engineering", "product", "data", "operations", "infrastructure"]

# API-layer allowlist for ``POST /api/memory/admin/bulk-update``. The repo's
# ``_UPDATABLE_FIELDS`` is intentionally broader (``status``, ``sensitivity``,
# ``is_personal``, ``confidence``, valid_from/until, supersedes, etc.) so the
# narrow per-item ``update`` path can still touch them; bulk-edit must NOT,
# because changing status / personal-flag / sensitivity in bulk bypasses the
# proper governance flow (``/admin/mandate``, ``/admin/revoke``,
# ``/{id}/personal``) and its dedicated audit rows. Callers that need those
# fields in bulk should use the per-item endpoints. See PR #126 review.
_BULK_UPDATE_ALLOWED = frozenset({
    "category", "domain", "tags", "tags_add", "tags_remove",
    "audience", "title", "content",
})


def _is_privileged_viewer(user: dict, conn: duckdb.DuckDBPyConnection) -> bool:
    """Admins (members of the Admin system group, per RBAC v13) are the
    privileged viewer tier. Pre-v13 the schema also had a km_admin role; v13
    collapsed the role hierarchy into groups, so the corporate-memory admin
    capability now lives on top of plain admin membership. Module authors
    needing a finer-grained gate (curator-only, etc.) should add a
    ``ResourceType.CORPORATE_MEMORY_ADMIN`` resource type and gate with
    ``require_resource_access`` instead of extending this helper."""
    user_id = user.get("id")
    if not user_id:
        return False
    return is_user_admin(user_id, conn)


def _effective_groups(
    user: dict, conn: duckdb.DuckDBPyConnection
) -> Optional[List[str]]:
    """Audience-filter group list for the caller, or ``None`` for admins
    (no filter — see all items regardless of audience).

    Reads from ``user_group_members`` JOIN ``user_groups`` (the v13 model).
    Pre-v13 this read ``users.groups`` JSON; that column was dropped in v13
    and the membership is now materialized in ``user_group_members`` with a
    ``source`` discriminator (admin / google_sync / system_seed).
    """
    if _is_privileged_viewer(user, conn):
        return None
    user_id = user.get("id")
    if not user_id:
        return []
    rows = conn.execute(
        """SELECT g.name FROM user_group_members m
           JOIN user_groups g ON m.group_id = g.id
           WHERE m.user_id = ?""",
        [user_id],
    ).fetchall()
    return [f"group:{r[0]}" for r in rows]


def _can_view_item(user: dict, item: dict, is_priv: bool) -> bool:
    """Personal items are visible only to the contributor and privileged
    viewers. Non-personal items are visible to any authenticated user.

    ``is_priv`` is pre-computed by the caller (one DB hit per request) so
    a per-item loop doesn't re-query ``user_group_members`` for every row.
    """
    if not item.get("is_personal"):
        return True
    if is_priv:
        return True
    return item.get("source_user") == user.get("email")


class CreateKnowledgeRequest(BaseModel):
    title: str
    content: str
    category: str
    tags: Optional[List[str]] = None
    domain: Optional[str] = None
    entities: Optional[List[str]] = None
    source_type: Optional[str] = None


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


class CreateContradictionRequest(BaseModel):
    item_a_id: str
    item_b_id: str
    explanation: str
    severity: Optional[str] = None
    suggested_resolution: Optional[str] = None


class PatchItemRequest(BaseModel):
    """Partial update for a knowledge item via PATCH /api/memory/admin/{id}.

    Replaces the narrow ``EditRequest`` (title + content only). Any field
    left as ``None`` is unchanged. Domain is validated against
    ``VALID_DOMAINS`` when supplied.
    """
    title: Optional[str] = None
    content: Optional[str] = None
    category: Optional[str] = None
    domain: Optional[str] = None
    tags: Optional[List[str]] = None
    audience: Optional[str] = None


class BulkUpdateRequest(BaseModel):
    """Apply ``updates`` to every id in ``item_ids``. Issue #62."""
    item_ids: List[str]
    updates: dict


class ResolveDuplicateRequest(BaseModel):
    """Resolve a duplicate-candidate relation row.

    ``resolution`` is one of ``duplicate`` / ``different`` / ``dismissed``
    (decision 2 in issue #62 — no auto-merge action; merging is a separate
    larger feature).
    """
    resolution: str


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
    # Privacy: non-privileged viewers can never opt out of the personal filter.
    # Their own personal contributions are visible via /my-contributions, not here.
    effective_exclude_personal = True if not _is_privileged_viewer(user, conn) else exclude_personal
    effective_groups = _effective_groups(user, conn)
    statuses = [status_filter] if status_filter else None
    if search:
        items = repo.search(
            search,
            exclude_personal=effective_exclude_personal,
            user_groups=effective_groups,
            statuses=statuses,
            category=category,
            domain=domain,
            source_type=source_type,
            limit=per_page,
            offset=offset,
        )
    else:
        items = repo.list_items(
            statuses=statuses,
            category=category,
            domain=domain,
            source_type=source_type,
            exclude_personal=effective_exclude_personal,
            user_groups=effective_groups,
            limit=per_page,
            offset=offset,
        )

    # Enrich with votes
    for item in items:
        votes = repo.get_votes(item["id"])
        item["upvotes"] = votes["upvotes"]
        item["downvotes"] = votes["downvotes"]
        item["score"] = votes["upvotes"] - votes["downvotes"]

    import math
    total_count = repo.count_items(
        search=search,
        statuses=statuses,
        category=category,
        domain=domain,
        source_type=source_type,
        exclude_personal=effective_exclude_personal,
        user_groups=effective_groups,
    )
    total_pages = math.ceil(total_count / per_page) if per_page > 0 else 1

    return {
        "items": items,
        "count": len(items),
        "page": page,
        "per_page": per_page,
        "total_count": total_count,
        "total_pages": total_pages,
    }


@router.get("/stats")
async def get_stats(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Get corporate memory statistics.

    Aggregations exclude personal items for non-privileged callers — otherwise
    `total` and the `by_*` counts would change in observable ways when a
    colleague flags or unflags a personal item, leaking existence info per
    ADR Decision 1.

    Uses SQL aggregation rather than ``repo.list_items()`` to keep the
    endpoint cheap on large knowledge bases (the loader path materializes
    every row + parses JSON tags/contributors per row, which blocks the
    event loop on N>1k items). Audience filter mirrors what list_items
    applies: ``audience IS NULL OR audience = 'all'`` plus, for non-admins,
    membership in any of the caller's group-prefixed audiences.
    """
    is_priv = _is_privileged_viewer(user, conn)
    groups = _effective_groups(user, conn)

    where_clauses: List[str] = []
    params: list = []
    if not is_priv:
        # Personal-item privacy: non-privileged callers see no personal items
        # in the aggregate, even their own. /my-contributions is the canonical
        # surface for a user's personal contributions; including them here
        # would make /api/memory/stats.total disagree with the count visible
        # via GET /api/memory (which forces exclude_personal=True for non-
        # admins regardless of source_user).
        where_clauses.append("(is_personal IS NULL OR is_personal = FALSE)")

    if groups is not None:
        # groups is None for admins → no audience filter; otherwise restrict to
        # null/'all' or one of the caller's group audiences.
        if groups:
            placeholders = ",".join(["?"] * len(groups))
            where_clauses.append(
                f"(audience IS NULL OR audience = 'all' OR audience IN ({placeholders}))"
            )
            params.extend(groups)
        else:
            where_clauses.append("(audience IS NULL OR audience = 'all')")

    where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    total = conn.execute(
        f"SELECT COUNT(*) FROM knowledge_items{where_sql}", params
    ).fetchone()[0] or 0

    by_status_rows = conn.execute(
        f"SELECT COALESCE(status, 'unknown') AS s, COUNT(*) "
        f"FROM knowledge_items{where_sql} GROUP BY s",
        params,
    ).fetchall()
    by_status = {r[0]: r[1] for r in by_status_rows}

    cat_rows = conn.execute(
        f"SELECT DISTINCT category FROM knowledge_items{where_sql} "
        f"{'AND' if where_sql else 'WHERE'} category IS NOT NULL",
        params,
    ).fetchall()
    categories = sorted(r[0] for r in cat_rows if r[0])

    by_domain_rows = conn.execute(
        f"SELECT COALESCE(domain, 'unset') AS d, COUNT(*) "
        f"FROM knowledge_items{where_sql} GROUP BY d",
        params,
    ).fetchall()
    by_domain = {r[0]: r[1] for r in by_domain_rows}

    by_source_rows = conn.execute(
        f"SELECT COALESCE(source_type, 'unknown') AS st, COUNT(*) "
        f"FROM knowledge_items{where_sql} GROUP BY st",
        params,
    ).fetchall()
    by_source_type = {r[0]: r[1] for r in by_source_rows}

    # by_tag + by_audience extend stats for the chip-filter UI (issue #62).
    # The repo helpers honor the same audience + personal-item filters this
    # endpoint applies above.
    repo = KnowledgeRepository(conn)
    exclude_personal_for_caller = not is_priv
    by_tag = repo.count_by_tag(
        exclude_personal=exclude_personal_for_caller,
        user_groups=groups,
    )
    by_audience = repo.count_by_audience(
        exclude_personal=exclude_personal_for_caller,
        user_groups=groups,
    )

    return {
        "total": total,
        "by_status": by_status,
        "categories": categories,
        "by_domain": by_domain,
        "by_source_type": by_source_type,
        "by_tag": by_tag,
        "by_audience": by_audience,
    }


@router.post("", status_code=201)
async def create_knowledge(
    request: CreateKnowledgeRequest,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    # Mirror the validation already enforced by PATCH /admin/{id} and bulk-update
    # so an item can't be created with a domain it can't be patched to. Empty /
    # missing domain is fine — only reject non-empty values outside the allowlist.
    # See PR #126 review.
    if request.domain and request.domain not in VALID_DOMAINS:
        raise HTTPException(
            status_code=400,
            detail=f"domain must be one of: {VALID_DOMAINS}",
        )
    repo = KnowledgeRepository(conn)
    item_id = str(uuid.uuid4())

    # Best-effort auto-tagging — runs only when an LLM extractor is configured.
    tags = list(request.tags) if request.tags else []
    try:
        from config.loader import load_instance_config
        from connectors.llm import create_extractor
        from services.corporate_memory.tagger import auto_tag_items
        cfg = load_instance_config()
        ai_cfg = cfg.get("ai")
        if ai_cfg:
            extractor = create_extractor(ai_cfg)
            stub = [{"id": item_id, "title": request.title, "content": request.content}]
            assignments = await asyncio.to_thread(auto_tag_items, stub, extractor)
            topics = assignments.get(item_id, [])
            if topics:
                seen: set[str] = set()
                merged: list[str] = []
                for t in topics + tags:
                    if t not in seen:
                        seen.add(t)
                        merged.append(t)
                tags = merged
    except Exception:
        pass  # tagging is non-critical — never block item creation

    create_kwargs = dict(
        id=item_id,
        title=request.title,
        content=request.content,
        category=request.category,
        source_user=user.get("email"),
        tags=tags or None,
        domain=request.domain,
        entities=request.entities,
        confidence=0.50,
    )
    if request.source_type:
        create_kwargs["source_type"] = request.source_type
    repo.create(**create_kwargs)
    return {"id": item_id, "status": "pending"}


@router.post("/{item_id}/vote")
async def vote_knowledge(
    item_id: str,
    request: VoteRequest,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    if request.vote not in (1, -1, 0):
        raise HTTPException(status_code=400, detail="Vote must be 1, -1, or 0 (retract)")
    repo = KnowledgeRepository(conn)
    item = repo.get_by_id(item_id)
    if not item or not _can_view_item(user, item, _is_privileged_viewer(user, conn)):
        raise HTTPException(status_code=404, detail="Knowledge item not found")
    if request.vote == 0:
        repo.unvote(item_id, user["id"])
    else:
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
    if not item or not _can_view_item(user, item, _is_privileged_viewer(user, conn)):
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
    """Write an admin governance audit row.

    Action names use the ``corporate_memory.<action>`` namespace as advertised
    in the 0.15.0 CHANGELOG. Pre-#62 the code wrote ``km_<action>`` — the
    audit-tab filter (see ``admin_audit`` below) accepts both prefixes so
    historical rows still surface.
    """
    audit = AuditRepository(conn)
    audit.log(
        user_id=admin_email,
        action=f"corporate_memory.{action}",
        resource=item_id,
        params=details,
    )


@router.post("/admin/approve")
async def admin_approve(
    item_id: str,
    user: dict = Depends(require_admin),
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
    user: dict = Depends(require_admin),
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
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = KnowledgeRepository(conn)
    _get_item_or_404(repo, item_id)
    repo.update_status(item_id, "mandatory")
    if request.audience is not None:
        repo.update(item_id, audience=request.audience)
    _audit_action(conn, user["email"], "mandate", item_id, {
        "reason": request.reason, "audience": request.audience,
    })
    return {"id": item_id, "status": "mandatory"}


@router.post("/admin/revoke")
async def admin_revoke(
    item_id: str,
    request: AdminActionRequest,
    user: dict = Depends(require_admin),
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
    user: dict = Depends(require_admin),
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
    user: dict = Depends(require_admin),
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
        if request.action == "mandate" and request.audience is not None:
            repo.update(item_id, audience=request.audience)
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
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Get pending items queue for admin review."""
    repo = KnowledgeRepository(conn)
    page = max(page, 1)
    offset = (page - 1) * per_page
    items = repo.list_items(statuses=["pending"], category=category, limit=per_page, offset=offset)
    return {"items": items, "count": len(items)}


@router.get("/admin/audit")
async def admin_audit(
    page: int = 1,
    per_page: int = 50,
    action: Optional[str] = None,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Get governance audit log.

    Filters ``corporate_memory.<action>`` rows AND legacy ``km_<action>``
    rows. The dual prefix is here because rows already in the audit log keep
    the legacy ``km_*`` action name (no migration of historical audit rows —
    they are write-once); new rows use the ``corporate_memory.*`` namespace.
    See issue #62 decision E.
    """
    # Pagination: page is 1-indexed; offset must apply to BOTH branches so the
    # UI's per-page navigation actually returns subsequent rows. Pre-fix, both
    # SQL paths had LIMIT only and silently returned page 1 for every page.
    offset = (max(page, 1) - 1) * per_page
    if action:
        # Match the action across both prefixes so the per-action filter still
        # surfaces historical rows.
        rows = conn.execute(
            """SELECT * FROM audit_log
                WHERE action IN (?, ?)
                ORDER BY timestamp DESC LIMIT ? OFFSET ?""",
            [f"corporate_memory.{action}", f"km_{action}", per_page, offset],
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM audit_log
                WHERE action LIKE 'corporate_memory.%' OR action LIKE 'km_%'
                ORDER BY timestamp DESC LIMIT ? OFFSET ?""",
            [per_page, offset],
        ).fetchall()
    if rows:
        columns = [desc[0] for desc in conn.description]
        entries = [dict(zip(columns, row)) for row in rows]
    else:
        entries = []
    return {"entries": entries, "count": len(entries)}


# ---- Admin contradiction endpoints ----

@router.get("/admin/contradictions")
async def admin_contradictions(
    resolved: Optional[bool] = None,
    exclude_personal: bool = True,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """List knowledge contradictions for admin review.

    By default (`exclude_personal=True`), personal items are replaced with
    {id, hidden: true} so the contradiction record is still visible for
    governance but personal content is not exposed. Pass exclude_personal=false
    to opt in to full content (KM_ADMIN only — see ADR Decision 1).
    """
    repo = KnowledgeRepository(conn)
    contradictions = repo.list_contradictions(resolved=resolved)
    # Collect all distinct item IDs and fetch in one query (M5 batch optimisation).
    all_item_ids = list({
        id_
        for c in contradictions
        for id_ in (c["item_a_id"], c["item_b_id"])
    })
    items_by_id = repo.get_by_ids(all_item_ids)
    for c in contradictions:
        item_a = items_by_id.get(c["item_a_id"])
        item_b = items_by_id.get(c["item_b_id"])
        if exclude_personal:
            c["item_a"] = {"id": c["item_a_id"], "hidden": True} if item_a and item_a.get("is_personal") else item_a
            c["item_b"] = {"id": c["item_b_id"], "hidden": True} if item_b and item_b.get("is_personal") else item_b
        else:
            c["item_a"] = item_a
            c["item_b"] = item_b
    return {"contradictions": contradictions, "count": len(contradictions)}


@router.post("/admin/contradictions")
async def admin_create_contradiction(
    request: CreateContradictionRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Admin endpoint for manually recording a contradiction between two knowledge items."""
    repo = KnowledgeRepository(conn)
    if not repo.get_by_id(request.item_a_id):
        raise HTTPException(status_code=404, detail=f"Item A not found: {request.item_a_id}")
    if not repo.get_by_id(request.item_b_id):
        raise HTTPException(status_code=404, detail=f"Item B not found: {request.item_b_id}")

    cid = repo.create_contradiction(
        item_a_id=request.item_a_id,
        item_b_id=request.item_b_id,
        explanation=request.explanation,
        severity=request.severity,
        suggested_resolution=request.suggested_resolution,
    )
    return {"id": cid}


@router.post("/admin/contradictions/{contradiction_id}/resolve")
async def admin_resolve_contradiction(
    contradiction_id: str,
    request: ResolveContradictionRequest,
    user: dict = Depends(require_admin),
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


# ---- Admin duplicate-candidate endpoints (issue #62) ----

VALID_DUPLICATE_RESOLUTIONS = ["duplicate", "different", "dismissed"]
DUPLICATE_RELATION_TYPE = "likely_duplicate"


def _strip_personal(item: Optional[dict], hide: bool) -> Optional[dict]:
    """Return a placeholder dict when ``item`` is personal and ``hide`` is set."""
    if item is None:
        return None
    if hide and item.get("is_personal"):
        return {"id": item.get("id"), "hidden": True}
    return item


@router.get("/admin/duplicate-candidates")
async def admin_duplicate_candidates(
    resolved: Optional[bool] = None,
    exclude_personal: bool = True,
    limit: int = 100,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """List duplicate-candidate relations for admin review.

    Pass ``resolved=true`` or ``resolved=false`` to filter; omit both to fetch
    every state (the original UI default). The web UI keeps surfacing the
    actionable backlog by passing ``resolved=false`` explicitly.

    With ``exclude_personal=true`` (default) personal items in the pair are
    replaced with ``{id, hidden: true}`` — the relation row is still visible
    so admins can resolve it, but content stays inside the personal-item
    privacy boundary (ADR Decision 1 precedent).
    """
    repo = KnowledgeRepository(conn)
    relations = repo.list_relations(
        relation_type=DUPLICATE_RELATION_TYPE,
        resolved=resolved,
        limit=limit,
    )
    item_ids = list({
        id_
        for r in relations
        for id_ in (r["item_a_id"], r["item_b_id"])
    })
    items_by_id = repo.get_by_ids(item_ids) if item_ids else {}
    for r in relations:
        r["item_a"] = _strip_personal(items_by_id.get(r["item_a_id"]), exclude_personal)
        r["item_b"] = _strip_personal(items_by_id.get(r["item_b_id"]), exclude_personal)
    return {"relations": relations, "count": len(relations)}


@router.post("/admin/duplicate-candidates/resolve")
async def admin_resolve_duplicate_candidate(
    item_a_id: str,
    item_b_id: str,
    request: ResolveDuplicateRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Resolve a duplicate-candidate relation.

    Admin chooses: ``duplicate`` (acknowledge), ``different`` (false
    positive), or ``dismissed`` (don't surface again, but no judgment).
    Idempotent re-resolve is rejected with 400 — the audit trail wants one
    decision per pair.
    """
    if request.resolution not in VALID_DUPLICATE_RESOLUTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"resolution must be one of: {VALID_DUPLICATE_RESOLUTIONS}",
        )
    repo = KnowledgeRepository(conn)
    existing = repo.get_relation(item_a_id, item_b_id, DUPLICATE_RELATION_TYPE)
    if not existing:
        raise HTTPException(status_code=404, detail="Duplicate-candidate relation not found")
    if existing.get("resolved"):
        raise HTTPException(status_code=400, detail="Relation already resolved")

    repo.resolve_relation(
        item_a_id=item_a_id,
        item_b_id=item_b_id,
        relation_type=DUPLICATE_RELATION_TYPE,
        resolved_by=user["email"],
        resolution=request.resolution,
    )
    # Resource-id of audit row is the canonical (a,b) pair for grep-ability.
    a, b = sorted([item_a_id, item_b_id])
    _audit_action(
        conn,
        user["email"],
        "resolve_duplicate",
        f"{a}::{b}",
        {"resolution": request.resolution, "item_a_id": a, "item_b_id": b},
    )
    return {
        "item_a_id": a,
        "item_b_id": b,
        "resolved": True,
        "resolution": request.resolution,
    }


# ---- Admin PATCH + bulk-update + tree endpoints (issue #62) ----


@router.patch("/admin/{item_id}")
async def admin_patch_item(
    item_id: str,
    request: PatchItemRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Partial update — accepts category/domain/tags/audience/title/content.

    Replaces the narrow ``POST /api/memory/admin/edit`` (kept one release as
    a thin alias). Audit row tagged ``corporate_memory.update_item`` records
    which fields changed (not the full diff — keep audit rows compact).
    """
    repo = KnowledgeRepository(conn)
    _get_item_or_404(repo, item_id)

    # ``exclude_unset=True`` preserves explicit ``null`` values from the request
    # body so callers can clear previously-set Optional fields (e.g. PATCH
    # ``{"audience": null}`` resets audience to NULL). With ``exclude_none=True``
    # those nulls were silently dropped — the only path to clear was the
    # empty-string short-circuit on ``domain``, and ``audience`` had no clearing
    # path at all. See PR #126 round-4 review.
    updates = request.model_dump(exclude_unset=True)
    if "domain" in updates and updates["domain"] and updates["domain"] not in VALID_DOMAINS:
        raise HTTPException(
            status_code=400,
            detail=f"domain must be one of: {VALID_DOMAINS}",
        )
    # ``title`` is NOT NULL in the schema. ``exclude_unset=True`` lets explicit
    # ``null`` through, which would 500 on a DuckDB constraint violation. Reject
    # at the boundary so the caller gets a 400 with a clear message instead.
    if "title" in updates and updates["title"] is None:
        raise HTTPException(status_code=400, detail="title cannot be null")
    if not updates:
        return {"id": item_id, "updated": []}

    # tags is a list — JSON-encode to match the column type, mirroring create().
    repo_kwargs = dict(updates)
    if "tags" in repo_kwargs:
        repo_kwargs["tags"] = (
            json.dumps(repo_kwargs["tags"]) if repo_kwargs["tags"] else None
        )
    repo.update(item_id, **repo_kwargs)
    _audit_action(
        conn,
        user["email"],
        "update_item",
        item_id,
        {"updated_fields": sorted(updates.keys())},
    )
    return {"id": item_id, "updated": sorted(updates.keys())}


@router.post("/admin/bulk-update")
async def admin_bulk_update(
    request: BulkUpdateRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Apply ``updates`` to every id in ``item_ids``. Per-id audit rows.

    Returns a per-id status map plus rolled-up convenience lists (200 even on
    partial failure — the body distinguishes successes from misses).
    """
    repo = KnowledgeRepository(conn)
    updates = dict(request.updates or {})
    # Reject governance-sensitive fields BEFORE hitting the repo. _UPDATABLE_FIELDS
    # in the repo is broad on purpose; this endpoint is the narrow path. Callers
    # that need to flip status/sensitivity/is_personal must use the dedicated
    # governance endpoints so the right audit row is written. See PR #126 review.
    disallowed = sorted(k for k in updates.keys() if k not in _BULK_UPDATE_ALLOWED)
    if disallowed:
        raise HTTPException(
            status_code=400,
            detail=(
                f"updates contains disallowed field(s): {disallowed}. "
                f"Allowed: {sorted(_BULK_UPDATE_ALLOWED)}"
            ),
        )
    if "domain" in updates and updates["domain"] and updates["domain"] not in VALID_DOMAINS:
        raise HTTPException(
            status_code=400,
            detail=f"domain must be one of: {VALID_DOMAINS}",
        )
    if not request.item_ids:
        return {"updated": [], "not_found": [], "errors": {}}

    statuses = repo.bulk_update(request.item_ids, updates)

    # Allowlist already enforced above, so every key in updates is auditable.
    audited_fields = sorted(updates.keys())
    updated: List[str] = []
    not_found: List[str] = []
    errors: dict = {}
    for item_id, status in statuses.items():
        if status == "updated":
            updated.append(item_id)
            _audit_action(
                conn,
                user["email"],
                "bulk_update",
                item_id,
                {"updated_fields": audited_fields, "batch": True},
            )
        elif status == "not_found":
            not_found.append(item_id)
        else:
            errors[item_id] = status
    return {"updated": updated, "not_found": not_found, "errors": errors}


# Axes the tree endpoint groups by. Anything else → 400. Order matters for
# the default chip rendering in the UI.
_TREE_AXES = ("domain", "category", "tag", "audience")


def _label_for_axis(axis: str, key: Optional[str]) -> str:
    """Pretty bucket label for the tree UI. Falls back to the raw key."""
    if key is None or key == "":
        return {
            "domain": "(no domain)",
            "category": "(no category)",
            "tag": "(no tag)",
            "audience": "All users",
        }.get(axis, "(unset)")
    if axis == "audience" and key == "all":
        return "All users"
    if axis == "audience" and key.startswith("group:"):
        return f"Group: {key[len('group:'):]}"
    return key


def _matches_chip_filters(
    item: dict,
    *,
    status_filter: Optional[str],
    source_type: Optional[str],
    audience: Optional[str],
    has_duplicate_ids: Optional[set],
    q: Optional[str],
) -> bool:
    """Apply chip filters to an already-RBAC-filtered item."""
    if status_filter and item.get("status") != status_filter:
        return False
    if source_type and item.get("source_type") != source_type:
        return False
    if audience:
        # Treat NULL audience as 'all' so chip-filter behavior matches the
        # SQL audience filter, ``count_by_audience`` (COALESCE→'all'), and the
        # tree's ``_bucket_key`` (NULL → 'all'). Without this coalesce,
        # NULL-audience items disappear from the ``audience=all`` chip even
        # though the rest of the system treats them as visible-to-everyone.
        item_audience = item.get("audience") or "all"
        if item_audience != audience:
            return False
    if has_duplicate_ids is not None and item.get("id") not in has_duplicate_ids:
        return False
    if q:
        needle = q.lower()
        title = (item.get("title") or "").lower()
        content = (item.get("content") or "").lower()
        if needle not in title and needle not in content:
            return False
    return True


@router.get("/tree")
async def get_tree(
    axis: str = "domain",
    status_filter: Optional[str] = None,
    source_type: Optional[str] = None,
    audience: Optional[str] = None,
    q: Optional[str] = None,
    has_duplicate: bool = False,
    exclude_personal: bool = True,
    page: int = 1,
    per_page: int = 50,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Server-side grouping for the Browse / Group-by tree (issue #62).

    Returns ``{groups: [{key, label, count, items: [...]}]}`` already
    RBAC-filtered + chip-filtered. The same ``_effective_groups`` and
    ``_can_view_item`` helpers used by ``GET /api/memory`` apply, so a
    non-admin caller never sees personal items belonging to others, and
    audience-restricted items only surface for members of the audience
    group.

    On the ``tag`` axis a single item appears once per tag it holds — that
    is the intended "overlapping bucket" affordance. Every other axis puts
    each item in its single canonical bucket.
    """
    if axis not in _TREE_AXES:
        raise HTTPException(
            status_code=400,
            detail=f"axis must be one of: {list(_TREE_AXES)}",
        )
    repo = KnowledgeRepository(conn)
    is_priv = _is_privileged_viewer(user, conn)
    effective_groups = _effective_groups(user, conn)
    # Privacy parity with ``GET /api/memory``: non-admin can never opt out.
    effective_exclude_personal = True if not is_priv else exclude_personal

    page = max(page, 1)
    per_page = max(min(per_page, 500), 1)

    # has_duplicate=true narrows the candidate set to items present in any
    # unresolved likely_duplicate relation. Computed once; intersected per
    # item below.
    has_duplicate_ids: Optional[set] = None
    if has_duplicate:
        rels = repo.list_relations(
            relation_type=DUPLICATE_RELATION_TYPE, resolved=False, limit=10000,
        )
        has_duplicate_ids = {
            id_ for r in rels for id_ in (r["item_a_id"], r["item_b_id"])
        }

    # Audience-axis privacy (decision 13): non-admins only see their own
    # group buckets + null/all. Use the audience pre-filter on the SQL side
    # so non-admins never accidentally see another group's bucket count.
    statuses = [status_filter] if status_filter else None
    items = repo.list_items(
        statuses=statuses,
        source_type=source_type,
        exclude_personal=effective_exclude_personal,
        user_groups=effective_groups,
        limit=10000,
        offset=0,
    )

    # Apply remaining chip filters that don't have a SQL layer yet.
    visible: List[dict] = []
    for item in items:
        if not _can_view_item(user, item, is_priv):
            continue
        if not _matches_chip_filters(
            item,
            status_filter=None,  # already in SQL
            source_type=None,    # already in SQL
            audience=audience,
            has_duplicate_ids=has_duplicate_ids,
            q=q,
        ):
            continue
        visible.append(item)

    # Group items by axis. tag is multi-bucket; everything else is single.
    groups: dict = {}

    def _bucket_key(item: dict, axis: str) -> List[str]:
        if axis == "tag":
            tags = item.get("tags")
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except json.JSONDecodeError:
                    tags = []
            if not tags:
                return [""]
            return [str(t) for t in tags]
        if axis == "audience":
            aud = item.get("audience")
            return [aud if aud else "all"]
        if axis == "category":
            return [item.get("category") or ""]
        # default: domain
        return [item.get("domain") or ""]

    for item in visible:
        for key in _bucket_key(item, axis):
            bucket = groups.setdefault(key, {
                "key": key,
                "label": _label_for_axis(axis, key),
                "items": [],
            })
            bucket["items"].append(item)

    # Stable ordering: alphabetic on key with the empty bucket sinking to
    # the bottom — the UI usually wants the "real" buckets up top.
    ordered = sorted(
        groups.values(),
        key=lambda g: (g["key"] == "", g["key"].lower() if isinstance(g["key"], str) else ""),
    )
    for g in ordered:
        g["count"] = len(g["items"])

    # Page over groups (not items): operators paging through hundreds of
    # tags want bucket-level pagination. The UI expands a bucket to see all
    # its items.
    start = (page - 1) * per_page
    paged = ordered[start:start + per_page]
    return {
        "axis": axis,
        "groups": paged,
        "page": page,
        "per_page": per_page,
        "total_groups": len(ordered),
        "total_items": sum(g["count"] for g in ordered),
    }


# ---- Bundle endpoint ----

@router.get("/bundle")
async def get_bundle(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Token-budgeted bundle of knowledge items for AI agent injection.

    Mandatory items are always included regardless of the token budget.
    Approved items are confidence×recency-ranked and included until the budget
    is exhausted. Audience-filtered by the caller's group memberships (admins
    see everything).
    """
    from datetime import datetime, timezone

    repo = KnowledgeRepository(conn)
    effective_groups = _effective_groups(user, conn)

    mandatory = repo.list_items(
        statuses=["mandatory"],
        exclude_personal=True,
        user_groups=effective_groups,
        limit=1000,
        offset=0,
    )

    approved = repo.list_items(
        statuses=["approved"],
        exclude_personal=True,
        user_groups=effective_groups,
        limit=1000,
        offset=0,
    )

    # Rank approved by confidence × recency (days since updated_at, max 365).
    # updated_at is intentional: a recently admin-edited item reflects a human
    # who just reviewed and corrected it, making it more trustworthy than an
    # older untouched item. This differs from confidence.py which decays from
    # created_at — the two scores serve different purposes (credibility vs freshness).
    now = datetime.now(timezone.utc)

    def _rank(item: dict) -> float:
        confidence = float(item["confidence"]) if item.get("confidence") is not None else 0.5
        updated_raw = item.get("updated_at")
        if updated_raw:
            try:
                if isinstance(updated_raw, str):
                    from datetime import datetime as dt
                    updated = dt.fromisoformat(updated_raw.replace("Z", "+00:00"))
                else:
                    updated = updated_raw
                if updated.tzinfo is None:
                    from datetime import timezone as tz
                    updated = updated.replace(tzinfo=tz.utc)
                age_days = max((now - updated).days, 0)
            except Exception:
                age_days = 365
        else:
            age_days = 365
        recency = max(0.0, 1.0 - age_days / 365.0)
        return confidence * recency

    approved_ranked = sorted(approved, key=_rank, reverse=True)

    def _token_est(item: dict) -> int:
        return len((item.get("title", "") + " " + item.get("content", ""))) // _CHARS_PER_TOKEN

    budget_remaining = BUNDLE_TOKEN_BUDGET - sum(_token_est(i) for i in mandatory)
    approved_included = []
    for item in approved_ranked:
        cost = _token_est(item)
        if budget_remaining - cost < 0:
            break
        approved_included.append(item)
        budget_remaining -= cost

    return {
        "mandatory": mandatory,
        "approved": approved_included,
        "token_estimate": BUNDLE_TOKEN_BUDGET - budget_remaining,
        "token_budget": BUNDLE_TOKEN_BUDGET,
    }
