"""Admin REST API for Data Packages (v49 unified stack).

A Data Package is a curated bundle of tables (M:N to ``table_registry``) that
serves as the unit of "Add to stack" on /catalog. Section 6 of the unified
stack design specifies a CRUD surface under ``/api/admin/data-packages``:

  - ``GET    /api/admin/data-packages``           — list + search
  - ``POST   /api/admin/data-packages``           — create
  - ``GET    /api/admin/data-packages/{id}``      — detail with embedded tables
  - ``PUT    /api/admin/data-packages/{id}``      — update metadata
  - ``DELETE /api/admin/data-packages/{id}``      — delete (cascades junction)
  - ``POST   /api/admin/data-packages/{id}/tables``         — add table
  - ``DELETE /api/admin/data-packages/{id}/tables/{table}`` — remove table

Every mutation writes an ``audit_log`` row (see Section 9.1 of the design).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.access import require_admin
from app.auth.dependencies import _get_db
from src.repositories.audit import AuditRepository
from src.repositories.data_packages import DataPackagesRepository
from src.repositories.table_registry import TableRegistryRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/data-packages", tags=["data-packages"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CreateDataPackageRequest(BaseModel):
    name: str
    slug: str
    description: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None


class UpdateDataPackageRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None


class AddTableRequest(BaseModel):
    table_id: str


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
    """Best-effort audit row. Mirrors the helper in ``app/api/access.py``."""
    try:
        AuditRepository(conn).log(
            user_id=actor_id,
            action=action,
            resource=resource,
            params=params,
            params_before=params_before,
        )
    except Exception:
        logger.warning("audit log failed for %s/%s", action, resource)


def _serialize(pkg: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": pkg["id"],
        "slug": pkg["slug"],
        "name": pkg["name"],
        "description": pkg.get("description"),
        "icon": pkg.get("icon"),
        "color": pkg.get("color"),
        "created_by": pkg.get("created_by"),
        "created_at": pkg["created_at"].isoformat() if pkg.get("created_at") else None,
        "updated_at": pkg["updated_at"].isoformat() if pkg.get("updated_at") else None,
    }


# ---------------------------------------------------------------------------
# CRUD endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=List[Dict[str, Any]])
async def list_data_packages(
    search: Optional[str] = None,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """List all packages, optionally name-prefix filtered for the chip-input
    typeahead on ``/admin/tables``."""
    rows = DataPackagesRepository(conn).list(search=search)
    return [_serialize(r) for r in rows]


@router.post("", status_code=201)
async def create_data_package(
    payload: CreateDataPackageRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Create a new Data Package. ``slug`` is the user-visible stable id; the
    UNIQUE constraint in DuckDB raises ``ConstraintException`` on collision and
    we translate that to ``409 slug_exists`` per spec."""
    repo = DataPackagesRepository(conn)
    if not payload.name.strip() or not payload.slug.strip():
        raise HTTPException(status_code=400, detail="name and slug are required")
    try:
        pkg_id = repo.create(
            name=payload.name.strip(),
            slug=payload.slug.strip(),
            description=payload.description,
            icon=payload.icon,
            color=payload.color,
            created_by=user.get("email") or user["id"],
        )
    except duckdb.ConstraintException:
        raise HTTPException(status_code=409, detail="slug_exists")
    _audit(
        conn,
        user["id"],
        "data_package.create",
        f"data_package:{pkg_id}",
        {"slug": payload.slug, "name": payload.name},
    )
    return {"id": pkg_id}


@router.get("/{pkg_id}")
async def get_data_package(
    pkg_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Detail view including the list of tables in the package."""
    repo = DataPackagesRepository(conn)
    pkg = repo.get(pkg_id)
    if not pkg:
        raise HTTPException(status_code=404, detail="data_package_not_found")
    tables = repo.list_tables(pkg_id)
    out = _serialize(pkg)
    out["tables"] = tables
    return out


@router.put("/{pkg_id}")
async def update_data_package(
    pkg_id: str,
    payload: UpdateDataPackageRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Patch package metadata. Audit row carries before/after diff so admins
    can reconstruct the rename history."""
    repo = DataPackagesRepository(conn)
    existing = repo.get(pkg_id)
    if not existing:
        raise HTTPException(status_code=404, detail="data_package_not_found")

    before = {
        "name": existing.get("name"),
        "description": existing.get("description"),
        "icon": existing.get("icon"),
        "color": existing.get("color"),
    }
    repo.update(
        pkg_id,
        name=payload.name,
        description=payload.description,
        icon=payload.icon,
        color=payload.color,
    )
    fresh = repo.get(pkg_id)
    after = {
        "name": fresh.get("name") if fresh else None,
        "description": fresh.get("description") if fresh else None,
        "icon": fresh.get("icon") if fresh else None,
        "color": fresh.get("color") if fresh else None,
    }
    _audit(
        conn,
        user["id"],
        "data_package.update",
        f"data_package:{pkg_id}",
        {"after": after},
        params_before={"before": before},
    )
    return _serialize(fresh) if fresh else {"id": pkg_id}


@router.delete("/{pkg_id}", status_code=204)
async def delete_data_package(
    pkg_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Delete a package. The junction (``data_package_tables``) is cleared by
    the repo since DuckDB doesn't honor ON DELETE CASCADE on every FK
    declaration. Tables themselves stay registered."""
    repo = DataPackagesRepository(conn)
    existing = repo.get(pkg_id)
    if not existing:
        raise HTTPException(status_code=404, detail="data_package_not_found")
    tables_count = len(repo.list_tables(pkg_id))
    repo.delete(pkg_id)
    _audit(
        conn,
        user["id"],
        "data_package.delete",
        f"data_package:{pkg_id}",
        {"slug": existing.get("slug"), "tables_count": tables_count},
    )


# ---------------------------------------------------------------------------
# Junction endpoints — add/remove tables
# ---------------------------------------------------------------------------


@router.post("/{pkg_id}/tables")
async def add_table_to_package(
    pkg_id: str,
    payload: AddTableRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Add a table to the package. 404 if the package or table doesn't exist.
    200 + ``{added: True/False}`` so the chip-input UI can re-render without
    a second roundtrip; idempotent on duplicate."""
    repo = DataPackagesRepository(conn)
    if not repo.get(pkg_id):
        raise HTTPException(status_code=404, detail="data_package_not_found")
    table_repo = TableRegistryRepository(conn)
    table = table_repo.get(payload.table_id)
    if not table:
        raise HTTPException(status_code=404, detail="table_not_found")
    added = repo.add_table(
        pkg_id, payload.table_id, added_by=user.get("email") or user["id"]
    )
    if added:
        _audit(
            conn,
            user["id"],
            "data_package.add_table",
            f"data_package:{pkg_id}",
            {"table_id": payload.table_id, "table_name": table.get("name")},
        )
    return {"added": added}


@router.delete("/{pkg_id}/tables/{table_id}", status_code=204)
async def remove_table_from_package(
    pkg_id: str,
    table_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Remove a table from the package. Idempotent on missing junction row."""
    repo = DataPackagesRepository(conn)
    if not repo.get(pkg_id):
        raise HTTPException(status_code=404, detail="data_package_not_found")
    removed = repo.remove_table(pkg_id, table_id)
    if removed:
        _audit(
            conn,
            user["id"],
            "data_package.remove_table",
            f"data_package:{pkg_id}",
            {"table_id": table_id},
        )
