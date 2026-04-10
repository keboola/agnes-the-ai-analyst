"""Column metadata API endpoints — CRUD and Keboola push."""

import logging
import os
from typing import List, Optional

import duckdb
import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.dependencies import get_current_user, require_admin, _get_db
from src.repositories.column_metadata import ColumnMetadataRepository
from src.repositories.table_registry import TableRegistryRepository

logger = logging.getLogger(__name__)
router = APIRouter(tags=["metadata"])


class ColumnMetadataItem(BaseModel):
    column_name: str
    basetype: Optional[str] = None
    description: Optional[str] = None
    confidence: str = "manual"


class ColumnMetadataSave(BaseModel):
    columns: List[ColumnMetadataItem]


@router.get("/api/admin/metadata/{table_id}")
async def get_table_metadata(
    table_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Return column metadata for a table."""
    repo = ColumnMetadataRepository(conn)
    columns = repo.list_for_table(table_id)
    return {"table_id": table_id, "columns": columns}


@router.post("/api/admin/metadata/{table_id}")
async def save_table_metadata(
    table_id: str,
    body: ColumnMetadataSave,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Save column metadata for a table. Admin only."""
    repo = ColumnMetadataRepository(conn)
    for item in body.columns:
        repo.save(
            table_id=table_id,
            column_name=item.column_name,
            basetype=item.basetype,
            description=item.description,
            confidence=item.confidence,
        )
    return {"status": "ok", "count": len(body.columns)}


@router.post("/api/admin/metadata/{table_id}/push")
async def push_metadata_to_source(
    table_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Push column metadata to Keboola Storage API. Admin only."""
    registry_repo = TableRegistryRepository(conn)
    table = registry_repo.get(table_id)
    if not table:
        raise HTTPException(status_code=404, detail=f"Table not found: {table_id}")

    source_type = table.get("source_type", "")
    if source_type != "keboola":
        raise HTTPException(
            status_code=400,
            detail=f"Push is only supported for keboola tables (table source_type={source_type!r})",
        )

    source_table = table.get("source_table") or table_id
    stack_url = os.environ.get("KBC_STACK_URL", "").rstrip("/")
    token = os.environ.get("KBC_STORAGE_TOKEN", "")

    if not stack_url or not token:
        raise HTTPException(
            status_code=500,
            detail="KBC_STACK_URL and KBC_STORAGE_TOKEN must be set",
        )

    metadata_repo = ColumnMetadataRepository(conn)
    columns = metadata_repo.list_for_table(table_id)
    if not columns:
        return {"status": "ok", "pushed": 0, "message": "No column metadata to push"}

    pushed = 0
    errors = []

    for col in columns:
        column_name = col["column_name"]
        metadata_payload = []

        if col.get("basetype"):
            metadata_payload.append({"key": "KBC.datatype.basetype", "value": col["basetype"]})
        if col.get("description"):
            metadata_payload.append({"key": "KBC.description", "value": col["description"]})

        if not metadata_payload:
            continue

        endpoint = f"{stack_url}/v2/storage/tables/{source_table}/columns/{column_name}/metadata"
        try:
            resp = httpx.post(
                endpoint,
                headers={"X-StorageApi-Token": token},
                json={"provider": "ai-metadata-enrichment", "metadata": metadata_payload},
                timeout=30,
            )
            if resp.status_code in (200, 201):
                pushed += 1
            else:
                errors.append(f"{column_name}: {resp.status_code} {resp.text[:200]}")
        except httpx.RequestError as e:
            errors.append(f"{column_name}: request error — {e}")

    result = {"status": "ok", "pushed": pushed}
    if errors:
        result["errors"] = errors
    return result
