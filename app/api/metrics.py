"""Metrics API endpoints — CRUD for metric definitions stored in DuckDB."""

from datetime import datetime
from typing import List, Optional

import duckdb
import yaml
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from app.auth.access import require_admin
from app.auth.dependencies import get_current_user, _get_db
from src.repositories.audit import AuditRepository
from src.repositories.metrics import MetricRepository

router = APIRouter(tags=["metrics"])


def _audit(
    conn: duckdb.DuckDBPyConnection,
    actor_id: str,
    action: str,
    target_id: str,
    params: Optional[dict] = None,
) -> None:
    """Audit-log helper for metric admin mutations. Same shape as
    ``app/api/users.py::_audit`` / ``marketplaces.py::_audit``."""
    try:
        safe_params = None
        if params:
            safe_params = {}
            for k, v in params.items():
                safe_params[k] = v.isoformat() if isinstance(v, datetime) else v
        AuditRepository(conn).log(
            user_id=actor_id,
            action=action,
            resource=f"metric:{target_id}",
            params=safe_params,
        )
    except Exception:
        pass


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
    _audit(
        conn, user["id"], "metric.upsert", body.id,
        {"name": body.name, "category": body.category, "source": body.source},
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
    _audit(conn, user["id"], "metric.delete", metric_id)
    return {"status": "deleted", "id": metric_id}


@router.post("/api/admin/metrics/import", status_code=200)
async def import_metrics(
    file: UploadFile = File(...),
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Import metrics from uploaded YAML file."""
    content = await file.read()
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as e:
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {e}")

    if not data:
        raise HTTPException(status_code=400, detail="Empty YAML file")

    metric_list = data if isinstance(data, list) else [data]
    repo = MetricRepository(conn)
    count = 0

    for metric in metric_list:
        if not isinstance(metric, dict):
            continue
        name = metric.get("name")
        category = metric.get("category")
        if not name or not category:
            raise HTTPException(
                status_code=400,
                detail="Each metric must have 'name' and 'category' fields",
            )

        metric_id = f"{category}/{name}"
        table_name = metric.pop("table", None) or metric.get("table_name")

        # Collect sql_by_* variants
        sql_variants = {}
        for key in list(metric.keys()):
            if key.startswith("sql_by_"):
                sql_variants[key[4:]] = metric.pop(key)

        repo.create(
            id=metric_id,
            name=name,
            display_name=metric.get("display_name", name),
            category=category,
            description=metric.get("description"),
            type=metric.get("type", "sum"),
            unit=metric.get("unit"),
            grain=metric.get("grain", "monthly"),
            table_name=table_name,
            tables=metric.get("tables"),
            expression=metric.get("expression"),
            time_column=metric.get("time_column"),
            dimensions=metric.get("dimensions"),
            filters=metric.get("filters"),
            synonyms=metric.get("synonyms"),
            notes=metric.get("notes"),
            sql=metric.get("sql", ""),
            sql_variants=sql_variants if sql_variants else None,
            validation=metric.get("validation"),
            source="yaml_import",
        )
        count += 1

    _audit(
        conn, user["id"], "metric.import", file.filename or "(unnamed)",
        {"count": count, "size_bytes": len(content)},
    )
    return {"status": "imported", "count": count}
