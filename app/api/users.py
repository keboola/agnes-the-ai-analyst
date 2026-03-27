"""User management endpoints."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List

import duckdb

from app.auth.dependencies import require_role, Role, _get_db
from src.repositories.users import UserRepository

router = APIRouter(prefix="/api/users", tags=["users"])


class CreateUserRequest(BaseModel):
    email: str
    name: str
    role: str = "analyst"


class UpdateUserRequest(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None


class UserResponse(BaseModel):
    id: str
    email: str
    name: Optional[str]
    role: str
    created_at: Optional[str]


@router.get("", response_model=List[UserResponse])
async def list_users(
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = UserRepository(conn)
    users = repo.list_all()
    return [
        UserResponse(
            id=u["id"], email=u["email"], name=u.get("name"),
            role=u["role"], created_at=str(u.get("created_at", "")),
        ) for u in users
    ]


@router.post("", response_model=UserResponse, status_code=201)
async def create_user(
    request: CreateUserRequest,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = UserRepository(conn)
    if repo.get_by_email(request.email):
        raise HTTPException(status_code=409, detail="User with this email already exists")
    user_id = str(uuid.uuid4())
    repo.create(id=user_id, email=request.email, name=request.name, role=request.role)
    return UserResponse(id=user_id, email=request.email, name=request.name, role=request.role, created_at=None)


@router.delete("/{user_id}", status_code=204)
async def delete_user(
    user_id: str,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = UserRepository(conn)
    if not repo.get_by_id(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    repo.delete(user_id)
