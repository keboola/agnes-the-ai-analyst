"""Admin endpoints — table discovery, registry management."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List
import duckdb

from app.auth.dependencies import require_role, Role, _get_db
from src.repositories.table_registry import TableRegistryRepository

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin"])


class RegisterTableRequest(BaseModel):
    name: str
    folder: Optional[str] = None
    sync_strategy: str = "full_refresh"
    primary_key: Optional[str] = None
    description: Optional[str] = None


class UpdateTableRequest(BaseModel):
    name: Optional[str] = None
    sync_strategy: Optional[str] = None
    primary_key: Optional[str] = None
    description: Optional[str] = None


@router.get("/discover-tables")
async def discover_tables(
    user: dict = Depends(require_role(Role.ADMIN)),
):
    """Discover all available tables from the configured data source."""
    try:
        from src.data_sync import create_data_source
        source = create_data_source()
        tables = source.discover_tables()
        return {"tables": tables, "count": len(tables), "source": source.get_source_name()}
    except ImportError:
        return {"tables": [], "count": 0, "error": "Data source not configured"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Discovery failed: {e}")


@router.get("/registry")
async def list_registry(
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Get full table registry."""
    repo = TableRegistryRepository(conn)
    tables = repo.list_all()
    return {"tables": tables, "count": len(tables)}


@router.post("/register-table", status_code=201)
async def register_table(
    request: RegisterTableRequest,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Register a new table in the system."""
    repo = TableRegistryRepository(conn)
    table_id = request.name.lower().replace(" ", "_")

    if repo.get(table_id):
        raise HTTPException(status_code=409, detail=f"Table '{table_id}' already registered")

    repo.register(
        id=table_id,
        name=request.name,
        folder=request.folder,
        sync_strategy=request.sync_strategy,
        primary_key=request.primary_key,
        description=request.description,
        registered_by=user.get("email"),
    )

    # Regenerate data_description.md if table_registry module supports it
    try:
        from src.table_registry import TableRegistry
        tr = TableRegistry()
        tr.generate_data_description_md()
        logger.info(f"Regenerated data_description.md after registering {table_id}")
    except Exception as e:
        logger.warning(f"Could not regenerate data_description.md: {e}")

    return {"id": table_id, "name": request.name, "status": "registered"}


@router.put("/registry/{table_id}")
async def update_table(
    table_id: str,
    request: UpdateTableRequest,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Update a registered table's configuration."""
    repo = TableRegistryRepository(conn)
    if not repo.get(table_id):
        raise HTTPException(status_code=404, detail="Table not found")

    updates = {k: v for k, v in request.dict().items() if v is not None}
    if updates:
        repo.register(id=table_id, **{**repo.get(table_id), **updates})
    return {"id": table_id, "updated": list(updates.keys())}


@router.delete("/registry/{table_id}", status_code=204)
async def unregister_table(
    table_id: str,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Unregister a table from the system."""
    repo = TableRegistryRepository(conn)
    if not repo.get(table_id):
        raise HTTPException(status_code=404, detail="Table not found")
    repo.unregister(table_id)
