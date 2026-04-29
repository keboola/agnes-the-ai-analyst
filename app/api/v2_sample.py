"""GET /api/v2/sample/{table_id}?n=5 — sample rows (spec §3.3)."""

from __future__ import annotations
import logging
from fastapi import APIRouter, Depends, HTTPException, Query
import duckdb

from app.auth.dependencies import get_current_user, _get_db
from src.rbac import can_access_table
from src.repositories.table_registry import TableRegistryRepository
from app.api.v2_cache import TTLCache
from connectors.bigquery.access import BqAccess, BqAccessError, get_bq_access

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v2", tags=["v2"])

_sample_cache = TTLCache(maxsize=512, ttl_seconds=3600)
_MAX_N = 100


def _fetch_bq_sample(bq, dataset: str, table: str, n: int) -> list[dict]:
    """Fetch up to `n` sample rows from a BQ table via the DuckDB BQ extension.

    `bq.duckdb_session()` provides a DuckDB conn with the bigquery extension
    loaded + auth secret installed. SQL here is server-constructed (validated
    identifiers + LIMIT n) — a BQ BadRequest means registry corruption, not
    user fault, so it surfaces as `bq_upstream_error` (HTTP 502).
    """
    from connectors.bigquery.access import translate_bq_error
    from src.identifier_validation import validate_quoted_identifier

    # Defense in depth: registry already validates these, but the v2 API
    # endpoints are downstream of admin REST writes that might bypass that
    # gate. A `source_table` containing a backtick would otherwise break
    # out of the `…` quoted identifier and execute arbitrary BQ SQL.
    if not (validate_quoted_identifier(bq.projects.data, "BQ project")
            and validate_quoted_identifier(dataset, "BQ dataset")
            and validate_quoted_identifier(table, "BQ source_table")):
        raise ValueError("unsafe BQ identifier in registry — refusing to query")

    bq_sql = f"SELECT * FROM `{bq.projects.data}.{dataset}.{table}` LIMIT {int(n)}"
    with bq.duckdb_session() as conn:
        try:
            df = conn.execute(
                "SELECT * FROM bigquery_query(?, ?)",
                [bq.projects.billing, bq_sql],
            ).fetchdf()
            return df.to_dict(orient="records")
        except Exception as e:
            raise translate_bq_error(e, bq.projects, bad_request_status="upstream_error")


def build_sample(
    conn: duckdb.DuckDBPyConnection,
    user: dict,
    table_id: str,
    *,
    n: int,
    bq: BqAccess,
) -> dict:
    n = max(1, min(int(n), _MAX_N))

    # RBAC + existence check MUST run before cache lookup — otherwise an
    # unauthorized user can read cached sample rows fetched by an authorized one.
    repo = TableRegistryRepository(conn)
    row = repo.get(table_id)
    if not row:
        raise FileNotFoundError(table_id)

    if user.get("role") != "admin" and not can_access_table(user, table_id, conn):
        raise PermissionError(table_id)

    cache_key = f"{table_id}|{n}"
    cached = _sample_cache.get(cache_key)
    if cached is not None:
        return cached

    source_type = row.get("source_type") or ""
    if source_type == "bigquery":
        rows = _fetch_bq_sample(bq, row.get("bucket") or "", row.get("source_table") or table_id, n)
    else:
        from app.utils import get_data_dir
        parquet = get_data_dir() / "extracts" / source_type / "data" / f"{table_id}.parquet"
        c = duckdb.connect(":memory:")
        try:
            df = c.execute(
                f"SELECT * FROM read_parquet(?) LIMIT {n}",
                [str(parquet)],
            ).fetchdf()
            rows = df.to_dict(orient="records")
        finally:
            c.close()

    payload = {"table_id": table_id, "rows": rows, "source": source_type}
    _sample_cache.set(cache_key, payload)
    return payload


@router.get("/sample/{table_id}")
async def sample(
    table_id: str,
    n: int = Query(default=5, ge=1, le=_MAX_N),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
    bq: BqAccess = Depends(get_bq_access),
):
    try:
        return build_sample(conn, user, table_id, n=n, bq=bq)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"table {table_id!r} not found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="not authorized for this table")
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail={"error": "unsafe_identifier", "message": str(e), "details": {}},
        )
    except BqAccessError as e:
        raise HTTPException(
            status_code=BqAccessError.HTTP_STATUS.get(e.kind, 500),
            detail={"error": e.kind, "message": e.message, "details": e.details},
        )
