"""GET /api/v2/sample/{table_id}?n=5 — sample rows (spec §3.3)."""

from __future__ import annotations
import logging
from fastapi import APIRouter, Depends, HTTPException, Query
import duckdb

from app.auth.dependencies import get_current_user, _get_db
from app.instance_config import get_value
from src.rbac import can_access_table
from src.repositories.table_registry import TableRegistryRepository
from app.api.v2_cache import TTLCache

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v2", tags=["v2"])

_sample_cache = TTLCache(maxsize=512, ttl_seconds=3600)
_MAX_N = 100


def _fetch_bq_sample(project: str, dataset: str, table: str, n: int) -> list[dict]:
    import duckdb
    from google.api_core import exceptions as gax
    from connectors.bigquery.auth import get_metadata_token
    from src.identifier_validation import validate_quoted_identifier

    # Defense in depth: registry already validates these, but the v2 API
    # endpoints are downstream of admin REST writes that might bypass that
    # gate. A `source_table` containing a backtick would otherwise break
    # out of the `…` quoted identifier and execute arbitrary BQ SQL.
    if not (validate_quoted_identifier(project, "BQ project")
            and validate_quoted_identifier(dataset, "BQ dataset")
            and validate_quoted_identifier(table, "BQ source_table")):
        raise ValueError("unsafe BQ identifier in registry — refusing to query")

    token = get_metadata_token()
    conn = duckdb.connect(":memory:")
    try:
        conn.execute("INSTALL bigquery FROM community; LOAD bigquery;")
        escaped = token.replace("'", "''")
        conn.execute(f"CREATE OR REPLACE SECRET bq_s (TYPE bigquery, ACCESS_TOKEN '{escaped}')")
        bq_sql = f"SELECT * FROM `{project}.{dataset}.{table}` LIMIT {int(n)}"
        try:
            df = conn.execute(
                "SELECT * FROM bigquery_query(?, ?)",
                [project, bq_sql],
            ).fetchdf()
        except gax.Forbidden as e:
            kind = "cross_project_forbidden" if "serviceusage" in str(e).lower() else "bq_forbidden"
            raise HTTPException(
                status_code=502,
                detail={
                    "error": kind,
                    "message": str(e),
                    "details": {
                        "billing_project": project,
                        "hint": (
                            "Set data_source.bigquery.billing_project in instance.yaml to a project "
                            "where the SA has serviceusage.services.use, or grant the SA that role "
                            "on the data project."
                        ) if kind == "cross_project_forbidden" else "",
                    },
                },
            )
        except gax.BadRequest as e:
            # /sample SQL is server-constructed (validated identifiers + LIMIT n);
            # a BadRequest here means registry corruption → upstream error, not user fault.
            raise HTTPException(
                status_code=502,
                detail={"error": "bq_upstream_error", "message": str(e), "details": {}},
            )
        except gax.GoogleAPICallError as e:
            raise HTTPException(
                status_code=502,
                detail={"error": "bq_upstream_error", "message": str(e), "details": {}},
            )
        return df.to_dict(orient="records")
    finally:
        conn.close()


def build_sample(
    conn: duckdb.DuckDBPyConnection,
    user: dict,
    table_id: str,
    *,
    n: int,
    project_id: str,
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
        rows = _fetch_bq_sample(project_id, row.get("bucket") or "", row.get("source_table") or table_id, n)
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
):
    # billing_project fallback (#134): when the SA lacks serviceusage on the data
    # project but has it on a billing project, BQ jobs must be billed to the latter.
    project_id = (
        get_value("data_source", "bigquery", "billing_project", default="")
        or get_value("data_source", "bigquery", "project", default="")
        or ""
    )
    try:
        return build_sample(conn, user, table_id, n=n, project_id=project_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"table {table_id!r} not found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="not authorized for this table")
    except HTTPException:
        # Already-translated upstream error from _fetch_bq_sample — propagate.
        raise
    except Exception as e:
        # Defense in depth: if a Google API exception bubbles up untranslated
        # (e.g. raised from a call path that bypassed _fetch_bq_sample's inner
        # try/except), translate at the endpoint boundary so the operator gets
        # a structured 502 instead of a bare 500.
        from google.api_core import exceptions as gax
        if isinstance(e, gax.Forbidden):
            kind = "cross_project_forbidden" if "serviceusage" in str(e).lower() else "bq_forbidden"
            raise HTTPException(
                status_code=502,
                detail={
                    "error": kind,
                    "message": str(e),
                    "details": {
                        "billing_project": project_id,
                        "hint": (
                            "Set data_source.bigquery.billing_project in instance.yaml to a project "
                            "where the SA has serviceusage.services.use, or grant the SA that role "
                            "on the data project."
                        ) if kind == "cross_project_forbidden" else "",
                    },
                },
            )
        if isinstance(e, gax.GoogleAPICallError):
            raise HTTPException(
                status_code=502,
                detail={"error": "bq_upstream_error", "message": str(e), "details": {}},
            )
        raise
