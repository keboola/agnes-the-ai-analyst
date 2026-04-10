"""Metrics API endpoints — CRUD for metric definitions stored in DuckDB."""

from typing import List, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.dependencies import get_current_user, require_admin, _get_db
from src.repositories.metrics import MetricRepository

router = APIRouter(tags=["metrics"])


class MetricCreate(BaseModel):
    id: str
    name: str
    display_name: str
    category: str
    sql: str
    description: Optional[str] = None
    type: str = "sum"
    unit: Optional[str] = None
    grain: str = "monthly"
    table_name: Optional[str] = None
    tables: Optional[List[str]] = None
    expression: Optional[str] = None
    time_column: Optional[str] = None
    dimensions: Optional[List[str]] = None
    filters: Optional[List[str]] = None
    synonyms: Optional[List[str]] = None
    notes: Optional[List[str]] = None
    sql_variants: Optional[dict] = None
    validation: Optional[dict] = None
    source: str = "manual"


@router.get("/api/metrics")
async def list_metrics(
    category: Optional[str] = None,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """List all metric definitions, optionally filtered by category."""
    repo = MetricRepository(conn)
    metrics = repo.list(category=category)
    return {"metrics": metrics, "count": len(metrics)}


@router.get("/api/metrics/{metric_id:path}")
async def get_metric(
    metric_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Get a single metric definition by ID."""
    repo = MetricRepository(conn)
    metric = repo.get(metric_id)
    if metric is None:
        raise HTTPException(status_code=404, detail=f"Metric '{metric_id}' not found")
    return metric


@router.post("/api/admin/metrics", status_code=201)
async def create_or_update_metric(
    body: MetricCreate,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Create or update a metric definition (admin only)."""
    repo = MetricRepository(conn)
    metric = repo.create(
        id=body.id,
        name=body.name,
        display_name=body.display_name,
        category=body.category,
        sql=body.sql,
        description=body.description,
        type=body.type,
        unit=body.unit,
        grain=body.grain,
        table_name=body.table_name,
        tables=body.tables,
        expression=body.expression,
        time_column=body.time_column,
        dimensions=body.dimensions,
        filters=body.filters,
        synonyms=body.synonyms,
        notes=body.notes,
        sql_variants=body.sql_variants,
        validation=body.validation,
        source=body.source,
    )
    return metric


@router.delete("/api/admin/metrics/{metric_id:path}")
async def delete_metric(
    metric_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Delete a metric definition by ID (admin only)."""
    repo = MetricRepository(conn)
    deleted = repo.delete(metric_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Metric '{metric_id}' not found")
    return {"status": "deleted", "id": metric_id}
