"""Corporate memory endpoints — knowledge items, voting."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List

import duckdb

from app.auth.dependencies import get_current_user, require_role, Role, _get_db
from src.repositories.knowledge import KnowledgeRepository

router = APIRouter(prefix="/api/memory", tags=["memory"])


class CreateKnowledgeRequest(BaseModel):
    title: str
    content: str
    category: str
    tags: Optional[List[str]] = None


class VoteRequest(BaseModel):
    vote: int  # 1 or -1


class KnowledgeResponse(BaseModel):
    id: str
    title: str
    content: Optional[str]
    category: Optional[str]
    status: str
    created_at: Optional[str]


@router.get("")
async def list_knowledge(
    status_filter: Optional[str] = None,
    category: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 50,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = KnowledgeRepository(conn)
    if search:
        items = repo.search(search)
    else:
        statuses = [status_filter] if status_filter else None
        items = repo.list_items(statuses=statuses, category=category, limit=limit)
    return {"items": items, "count": len(items)}


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


@router.put("/{item_id}/status")
async def update_status(
    item_id: str,
    new_status: str,
    user: dict = Depends(require_role(Role.KM_ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = KnowledgeRepository(conn)
    if not repo.get_by_id(item_id):
        raise HTTPException(status_code=404, detail="Knowledge item not found")
    repo.update_status(item_id, new_status)
    return {"id": item_id, "status": new_status}
