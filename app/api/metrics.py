"""Metrics API endpoints — CRUD for metric definitions stored in DuckDB."""

from typing import List, Optional

import duckdb
import yaml
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from app.auth.access import require_admin
from app.auth.dependencies import get_current_user, _get_db

from src.rbac import get_accessible_tables, table_not_in_stack_message
from src.repositories import (
    metric_repo,
    table_registry_repo,
)

router = APIRouter(tags=["metrics"])


def _metric_table_names(metric: dict) -> List[str]:
    """Table_registry view NAMEs this metric depends on — the single-table
    ``table_name`` or the relationship-JOIN ``tables`` list."""
    tables = metric.get("tables") or []
    if tables:
        return list(tables)
    name = metric.get("table_name")
    return [name] if name else []


def _first_inaccessible_table(metric: dict, allowed: Optional[set]) -> Optional[str]:
    """Id of the first table (of ``metric``'s ``table_name``/``tables``) the
    caller can't access, or ``None`` if the caller can access all of them
    (or the metric references no table at all — nothing to gate).

    ``allowed=None`` means "all" (admin / no stack gating).

    ``table_name``/``tables`` store the DuckDB VIEW NAME (``table_registry
    .name``), not the id — those can differ (e.g. name "Orders EU" slugifies
    to id "orders_eu"), so each name is resolved via ``get_by_name`` before
    checking membership in ``allowed`` (which is id-keyed). An unresolvable
    name (registry row missing) is treated as inaccessible, fail-closed.
    """
    if allowed is None:
        return None
    repo = table_registry_repo()
    for name in _metric_table_names(metric):
        row = repo.get_by_name(name)
        table_id = row.get("id") if row else None
        if table_id is None or table_id not in allowed:
            return table_id or name
    return None


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
    """List all metric definitions, optionally filtered by category.

    RBAC-gated (#953): a metric whose ``table_name``/``tables`` reference a
    table the caller can't access via their Data Package stack is silently
    omitted — same fail-closed filtering convention as the table catalog
    (see ``app/api/knowledge_search.py``). Admin sees everything.
    """
    repo = metric_repo()
    metrics = repo.list(category=category)
    accessible_ids = get_accessible_tables(user, conn)
    allowed = None if accessible_ids is None else set(accessible_ids)
    metrics = [m for m in metrics if _first_inaccessible_table(m, allowed) is None]
    return {"metrics": metrics, "count": len(metrics)}


@router.get("/api/metrics/{metric_id:path}")
async def get_metric(
    metric_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Get a single metric definition by ID.

    RBAC-gated (#953): 404 if the metric doesn't exist; 403 (using the
    standard ``table_not_in_stack_message``) if it exists but the caller
    lacks access to (any of) its table(s).
    """
    repo = metric_repo()
    metric = repo.get(metric_id)
    if metric is None:
        raise HTTPException(status_code=404, detail=f"Metric '{metric_id}' not found")

    accessible_ids = get_accessible_tables(user, conn)
    allowed = None if accessible_ids is None else set(accessible_ids)
    denial_id = _first_inaccessible_table(metric, allowed)
    if denial_id is not None:
        raise HTTPException(status_code=403, detail=table_not_in_stack_message(denial_id))
    return metric


@router.post("/api/admin/metrics", status_code=201)
async def create_or_update_metric(
    body: MetricCreate,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Create or update a metric definition (admin only)."""
    repo = metric_repo()
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


@router.delete("/api/admin/metrics/{metric_id:path}", status_code=204)
async def delete_metric(
    metric_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Delete a metric definition by ID (admin only)."""
    repo = metric_repo()
    deleted = repo.delete(metric_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Metric '{metric_id}' not found")


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
    repo = metric_repo()
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

    return {"status": "imported", "count": count}
