"""Hybrid query endpoint — two-phase BQ registration + DuckDB execution."""

from typing import Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.access import require_admin
from src.db import get_analytics_db_readonly
from src.remote_query import RemoteQueryEngine, RemoteQueryError, load_config

router = APIRouter(prefix="/api/query", tags=["query"])


class HybridQueryRequest(BaseModel):
    sql: str
    register_bq: Dict[str, str] = {}


@router.post("/hybrid")
async def hybrid_query(request: HybridQueryRequest, user: dict = Depends(require_admin)):
    config = load_config()
    analytics = get_analytics_db_readonly()
    try:
        engine = RemoteQueryEngine(
            analytics,
            max_bq_registration_rows=config.get("max_bq_registration_rows", 500_000),
            max_memory_mb=config.get("max_memory_mb", 2048),
            max_result_rows=config.get("max_result_rows", 100_000),
            timeout_seconds=config.get("timeout_seconds", 300),
        )
        for alias, bq_sql in request.register_bq.items():
            try:
                engine.register_bq(alias, bq_sql)
            except RemoteQueryError as e:
                raise HTTPException(status_code=400, detail=f"BQ '{alias}': {e.error_type}: {e}")
        try:
            result = engine.execute(request.sql)
        except RemoteQueryError as e:
            raise HTTPException(status_code=400, detail=f"Query: {e.error_type}: {e}")
        return result
    finally:
        analytics.close()
