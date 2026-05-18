"""Recipes — admin-curated, multi-table query templates (v53).

Sibling concept to Data Packages on /catalog (separate "Recipes" tab).
Recipes aren't stack-subscribable; analysts use a recipe, they don't
opt in. Admin POST/PUT/DELETE; any authenticated user can GET.

  - ``GET    /api/recipes``                — list (any user; filtered
                                              by status='prod' for non-
                                              admin — drafts hidden)
  - ``GET    /api/recipes/{slug}``         — read by slug (drilldown)
  - ``POST   /api/admin/recipes``          — create
  - ``PUT    /api/admin/recipes/{id}``     — update
  - ``DELETE /api/admin/recipes/{id}``     — delete

Audit actions: ``recipe.create / update / delete``.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from app.api.data_packages import _validate_color, _validate_status
from app.auth.access import is_user_admin, require_admin
from app.auth.dependencies import _get_db, get_current_user
from src.repositories.audit import AuditRepository
from src.repositories.recipes import RecipesRepository

logger = logging.getLogger(__name__)

# Two routers — public (any auth user) at /api/recipes, admin-only at
# /api/admin/recipes. Mirrors the data_packages split.
public_router = APIRouter(prefix="/api/recipes", tags=["recipes"])
admin_router = APIRouter(prefix="/api/admin/recipes", tags=["recipes-admin"])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def _validate_slug(slug: str) -> str:
    if not _SLUG_RE.match(slug or ""):
        raise ValueError(
            "slug must be lowercase alphanumeric + dashes, 1-63 chars"
        )
    return slug


class CreateRecipeRequest(BaseModel):
    slug: str
    title: str
    description: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    sql_template: Optional[str] = None
    related_table_ids: Optional[List[str]] = None
    status: Optional[str] = None

    @field_validator("slug")
    @classmethod
    def _check_slug(cls, v: str) -> str:
        return _validate_slug(v.strip())

    @field_validator("color")
    @classmethod
    def _check_color(cls, v: Optional[str]) -> Optional[str]:
        return _validate_color(v)

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: Optional[str]) -> Optional[str]:
        return _validate_status(v)


class UpdateRecipeRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    sql_template: Optional[str] = None
    related_table_ids: Optional[List[str]] = None
    status: Optional[str] = None

    @field_validator("color")
    @classmethod
    def _check_color(cls, v: Optional[str]) -> Optional[str]:
        return _validate_color(v)

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: Optional[str]) -> Optional[str]:
        return _validate_status(v)


def _audit(conn, actor_id, action, resource, params=None, before=None):
    try:
        AuditRepository(conn).log(
            user_id=actor_id, action=action, resource=resource,
            params=params, params_before=before,
        )
    except Exception:
        logger.warning("audit log failed for %s/%s", action, resource)


def _serialize(r: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": r["id"],
        "slug": r["slug"],
        "title": r["title"],
        "description": r.get("description"),
        "icon": r.get("icon"),
        "color": r.get("color"),
        "sql_template": r.get("sql_template"),
        "related_table_ids": r.get("related_table_ids") or [],
        "status": r.get("status") or "prod",
        "created_by": r.get("created_by"),
        "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
        "updated_at": r["updated_at"].isoformat() if r.get("updated_at") else None,
    }


# ---------------------------------------------------------------------------
# Public endpoints (any authenticated user)
# ---------------------------------------------------------------------------


@public_router.get("")
async def list_recipes(
    search: Optional[str] = None,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """List recipes. Non-admin viewers see only ``status='prod'``;
    admins see every status (incl. drafts)."""
    rows = RecipesRepository(conn).list(search=search)
    if not is_user_admin(user["id"], conn):
        rows = [r for r in rows if (r.get("status") or "prod") == "prod"]
    return {"items": [_serialize(r) for r in rows]}


@public_router.get("/{slug}")
async def get_recipe(
    slug: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = RecipesRepository(conn)
    r = repo.get_by_slug(slug)
    if not r:
        raise HTTPException(status_code=404, detail="recipe_not_found")
    # Non-admins can't see draft recipes via the slug endpoint either.
    if (r.get("status") or "prod") != "prod" and not is_user_admin(user["id"], conn):
        raise HTTPException(status_code=404, detail="recipe_not_found")
    return _serialize(r)


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------


@admin_router.post("", status_code=201)
async def create_recipe(
    payload: CreateRecipeRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = RecipesRepository(conn)
    if not payload.title.strip():
        raise HTTPException(status_code=400, detail="title is required")
    try:
        recipe_id = repo.create(
            slug=payload.slug,
            title=payload.title.strip(),
            description=payload.description,
            icon=payload.icon,
            color=payload.color,
            sql_template=payload.sql_template,
            related_table_ids=payload.related_table_ids,
            status=payload.status or "prod",
            created_by=user.get("email") or user["id"],
        )
    except duckdb.ConstraintException:
        raise HTTPException(status_code=409, detail="slug_exists")
    _audit(conn, user["id"], "recipe.create", f"recipe:{recipe_id}",
           {"slug": payload.slug, "title": payload.title})
    return {"id": recipe_id}


@admin_router.get("")
async def admin_list_recipes(
    search: Optional[str] = None,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    rows = RecipesRepository(conn).list(search=search)
    return [_serialize(r) for r in rows]


@admin_router.get("/{recipe_id}")
async def admin_get_recipe(
    recipe_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    r = RecipesRepository(conn).get(recipe_id)
    if not r:
        raise HTTPException(status_code=404, detail="recipe_not_found")
    return _serialize(r)


@admin_router.put("/{recipe_id}")
async def update_recipe(
    recipe_id: str,
    payload: UpdateRecipeRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = RecipesRepository(conn)
    existing = repo.get(recipe_id)
    if not existing:
        raise HTTPException(status_code=404, detail="recipe_not_found")
    clear_related = payload.related_table_ids == []
    repo.update(
        recipe_id,
        title=payload.title,
        description=payload.description,
        icon=payload.icon,
        color=payload.color,
        sql_template=payload.sql_template,
        related_table_ids=(
            None if clear_related else payload.related_table_ids
        ),
        clear_related_tables=clear_related,
        status=payload.status,
    )
    fresh = repo.get(recipe_id)
    _audit(
        conn, user["id"], "recipe.update", f"recipe:{recipe_id}",
        {"after": {k: fresh.get(k) for k in ("title", "status")}},
        before={k: existing.get(k) for k in ("title", "status")},
    )
    return _serialize(fresh) if fresh else {"id": recipe_id}


@admin_router.delete("/{recipe_id}", status_code=204)
async def delete_recipe(
    recipe_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = RecipesRepository(conn)
    existing = repo.get(recipe_id)
    if not existing:
        raise HTTPException(status_code=404, detail="recipe_not_found")
    repo.delete(recipe_id)
    _audit(conn, user["id"], "recipe.delete", f"recipe:{recipe_id}",
           {"slug": existing.get("slug")})
