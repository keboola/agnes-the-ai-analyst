"""GET /api/v2/sample/{table_id}?n=5 — sample rows (spec §3.3)."""

from __future__ import annotations
import logging
import math
import time
from fastapi import APIRouter, Depends, HTTPException, Query
import duckdb

from app.auth.dependencies import get_current_user, _get_db
from src.db import _open_duckdb
from src.audit_helpers import identity_for_audit, client_kind_from_user
from src.rbac import can_access_table
from app.api.v2_cache import TTLCache
from connectors.bigquery.access import BqAccess, BqAccessError, get_bq_access

from src.repositories import (
    audit_repo,
    table_registry_repo,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v2", tags=["v2"])

_sample_cache = TTLCache(maxsize=512, ttl_seconds=3600)
_MAX_N = 100


class TableNotSyncedError(FileNotFoundError):
    """Registered row exists but no local data has landed yet.

    Subclasses FileNotFoundError so every existing catch keeps working; the
    endpoint maps it to a 404 whose detail explains the pending/failing
    first sync instead of the misleading bare "table not found" (which reads
    as "registration failed" to the admin who just registered it)."""

    def __init__(self, table_id: str, detail: str):
        super().__init__(table_id)
        self.detail = detail


class TableNotPreviewableError(TableNotSyncedError):
    """No parquet, and there never will be one — by design, not by failure.

    ``query_mode='remote'`` only: every query goes live to the upstream source,
    so nothing is ever materialized. Such rows used to fall into the not-synced
    branch and be reported as "the first sync is pending or failing", sending an
    admin to hunt a job that does not exist and never will.

    ``server_only`` is deliberately NOT one of these — it suppresses
    *distribution* to analyst laptops while the server still materializes the
    parquet, so a missing one there is a real failure and must keep saying so.

    Subclasses ``TableNotSyncedError`` so every existing catch and the endpoint's
    404 mapping keep working unchanged; only the explanation differs.
    """


def _not_previewable_detail(table_id: str, *, query_mode: str) -> str:
    """Explain a `query_mode='remote'` row, which has no server parquet at all.

    ``server_only`` deliberately does NOT belong here, and an earlier version of
    this fix got that wrong. It is a *distribution* suppressor: the server still
    materializes the parquet, `agnes pull` just never ships it to a laptop —
    ``app/api/sync.py`` says as much ("remote tables have no server parquet at
    all, and server_only ones are deliberately not distributed"), and the
    registration validator rejects ``server_only`` together with
    ``query_mode='remote'`` for that reason. So a `server_only` row whose parquet
    is missing HERE, on the server, is a genuinely pending or failing sync, and
    telling its admin "no sync is pending" would hide a broken job behind a
    reassurance (Devin Review on #1189).

    The predicate this originally borrowed from — `sync.py`'s signed-URL gate —
    lumps the two together correctly, because for *distribution* they coincide.
    For *previewability on the server* they do not.
    """
    return (
        f"table {table_id!r} is registered with query_mode={query_mode or 'remote'!r}, so it is "
        "queried at the source and never materialized as a parquet — there is no sample to "
        f"preview, and no sync is pending. Read it at the source instead: `agnes query --remote "
        f'"SELECT * FROM {table_id} LIMIT 10"`.'
    )


def _not_synced_detail(table_id: str) -> str:
    """Explain a registered-but-dataless table, with the last sync error
    when sync_state recorded one."""
    detail = (
        f"table {table_id!r} is registered but has no synced data yet — "
        "the first sync is pending or failing (see the sync status on "
        "Admin → Tables)"
    )
    try:
        from src.repositories import sync_state_repo

        state = sync_state_repo().get_table_state(table_id) or {}
        err = state.get("error") or ""
        if err:
            detail += f"; last sync error: {str(err)[:300]}"
    except Exception:
        logger.debug("sync_state lookup failed for %s while building sample 404 detail", table_id)
    return detail


def _sanitize_for_json(obj):
    """Recursively replace NaN / ±inf floats with None so the response
    survives JSON serialization. FastAPI's default encoder rejects these
    (``ValueError: Out of range float values are not JSON compliant``)
    even though Python's stdlib ``json`` accepts them by default. NaNs
    show up routinely in DuckDB / BigQuery scans (NULL → NaN through the
    pandas DataFrame round-trip), so the endpoint must sanitize at the
    data-prep boundary rather than rely on the serializer."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, list):
        return [_sanitize_for_json(x) for x in obj]
    if isinstance(obj, tuple):
        return tuple(_sanitize_for_json(x) for x in obj)
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    return obj


def _fetch_bq_sample(bq, dataset: str, table: str, n: int) -> list[dict]:
    """Fetch up to `n` sample rows from a BQ table via the DuckDB BQ extension.

    `bq.duckdb_session()` provides a DuckDB conn with the bigquery extension
    loaded + auth secret installed. SQL here is server-constructed (validated
    identifiers + LIMIT n) — a BQ BadRequest means registry corruption, not
    user fault, so it surfaces as `bq_upstream_error` (HTTP 502).
    """
    from connectors.bigquery.access import translate_bq_error
    from src.identifier_validation import validate_quoted_identifier

    # Surface "BQ not configured" as the structured 500 BqAccessError(not_configured)
    # with hint pointing at instance.yaml, NOT as the misleading 400 unsafe_identifier
    # the empty-string sentinel BqAccess would otherwise trigger from
    # validate_quoted_identifier below. Devin BUG_0002 on PR #138.
    if not bq.projects.data:
        bq.client()  # raises BqAccessError(not_configured); endpoint catches it

    # Defense in depth: registry already validates these, but the v2 API
    # endpoints are downstream of admin REST writes that might bypass that
    # gate. A `source_table` containing a backtick would otherwise break
    # out of the `…` quoted identifier and execute arbitrary BQ SQL.
    if not (
        validate_quoted_identifier(bq.projects.data, "BQ project")
        and validate_quoted_identifier(dataset, "BQ dataset")
        and validate_quoted_identifier(table, "BQ source_table")
    ):
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
    repo = table_registry_repo()
    row = repo.get(table_id)
    if not row:
        raise FileNotFoundError(table_id)

    if not can_access_table(user, table_id, conn):
        raise PermissionError(table_id)

    source_type = row.get("source_type") or ""

    # Internal source — never cache. Sample rows here are RBAC-scoped per
    # caller (alice sees alice's rows; admin sees all), so a shared cache
    # would leak alice's rows to bob on the next request. The source data
    # is small + the per-request query is cheap, so skipping the cache
    # entirely is the right trade-off.
    if source_type == "internal":
        from connectors.internal.access import (
            INTERNAL_TABLES_BY_ID,
            build_filter_clause,
            sample_internal_rows,
        )
        from app.auth.access import is_user_admin as _is_admin
        from app.auth.session_principal import PRINCIPAL_TYPES

        if table_id not in INTERNAL_TABLES_BY_ID:
            raise FileNotFoundError(table_id)
        internal_def = INTERNAL_TABLES_BY_ID[table_id]
        # is_user_admin takes (user_id, conn) — earlier draft passed the
        # whole user dict and crashed with TypeError on first request
        # (review #278/2). Same fix as app/api/query.py:_run_internal_query.
        # A Principal has no .get("id") — treat co-session / agent-session as non-admin
        # for internal row-level filter. build_filter_clause expects a dict
        # so pass a shim when user is a principal.
        if isinstance(user, PRINCIPAL_TYPES):
            is_admin = False
            user_dict_shim = {"id": "", "email": ""}
        else:
            is_admin = _is_admin(user.get("id"), conn) if user.get("id") else False
            user_dict_shim = user
        where_clause = build_filter_clause(internal_def, user_dict_shim, is_admin)
        # The source rows live in the active state backend (DuckDB or Postgres);
        # sample_internal_rows dispatches on use_pg() so the preview is correct
        # on either — a raw always-DuckDB read returned nothing on Postgres.
        rows = sample_internal_rows(internal_def, where_clause, n)
        return {"table_id": table_id, "rows": _sanitize_for_json(rows), "source": source_type}

    cache_key = f"{table_id}|{n}"
    cached = _sample_cache.get(cache_key)
    if cached is not None:
        return cached

    if source_type == "bigquery" and (row.get("query_mode") or "") != "materialized":
        rows = _fetch_bq_sample(bq, row.get("bucket") or "", row.get("source_table") or table_id, n)
    else:
        # Resolve by source-name-agnostic lookup — the extract directory is not
        # necessarily the source_type (e.g. the bundled `demo` extract).
        # `_glob` so a PARTITIONED table resolves too: that sync writes
        # `data/<table_id>/<partition>.parquet`, a directory, so the single-file
        # lookup returned None and a healthy fully-synced table was reported as
        # having a pending or failing first sync (Devin Review on #1189).
        from app.utils import resolve_local_parquet_glob

        parquet = resolve_local_parquet_glob(table_id, source_type)
        if parquet is None:
            # The registry row exists (checked above), so this is never "no such
            # table" — but WHY there is no parquet decides what to tell the
            # viewer. Only `query_mode='remote'` is never materialized: every
            # query goes live to the upstream source. `server_only` is NOT in
            # this branch on purpose — it suppresses distribution to analyst
            # laptops, not materialization here, so a missing parquet for one of
            # those rows is a real pending/failing sync and must keep saying so.
            query_mode = row.get("query_mode") or ""
            if query_mode == "remote":
                raise TableNotPreviewableError(table_id, _not_previewable_detail(table_id, query_mode=query_mode))
            # Genuinely "no data has landed yet" — including for server_only.
            raise TableNotSyncedError(table_id, _not_synced_detail(table_id))
        c = _open_duckdb(":memory:")
        try:
            df = c.execute(
                f"SELECT * FROM read_parquet(?) LIMIT {n}",
                [parquet],
            ).fetchdf()
            rows = df.to_dict(orient="records")
        finally:
            c.close()

    rows = _sanitize_for_json(rows)
    payload = {"table_id": table_id, "rows": rows, "source": source_type}
    _sample_cache.set(cache_key, payload)
    return payload


@router.get("/sample/{table_id}")
def sample(
    table_id: str,
    n: int = Query(default=5, ge=1, le=_MAX_N),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
    bq: BqAccess = Depends(get_bq_access),
):
    # Plain ``def`` — opens a `bq.duckdb_session()` and runs sync queries
    # through the BQ extension. See PR #188 Tier 1 entry.
    t0 = time.monotonic()
    resource = f"table:{table_id}"[:256]
    try:
        result = build_sample(conn, user, table_id, n=n, bq=bq)
        try:
            audit_repo().log(
                user_id=identity_for_audit(user)[0],
                action="catalog.sample",
                resource=resource,
                params={
                    "rows_returned": len(result.get("rows", [])),
                    "duration_ms": int((time.monotonic() - t0) * 1000),
                },
                result="success",
                client_kind=client_kind_from_user(user),
            )
        except Exception:
            logger.exception("audit_log write failed for catalog.sample; continuing")
        return result
    except (FileNotFoundError, PermissionError, ValueError, BqAccessError) as exc:
        try:
            if isinstance(exc, FileNotFoundError):
                status_code = 404
            elif isinstance(exc, PermissionError):
                status_code = 403
            elif isinstance(exc, ValueError):
                status_code = 400
            else:
                status_code = BqAccessError.HTTP_STATUS.get(exc.kind, 500)  # type: ignore[union-attr]
            audit_repo().log(
                user_id=identity_for_audit(user)[0],
                action="catalog.sample",
                resource=resource,
                params={"duration_ms": int((time.monotonic() - t0) * 1000), "error": str(exc)[:200]},
                result=f"error.{status_code}",
                client_kind=client_kind_from_user(user),
            )
        except Exception:
            logger.exception("audit_log write failed on error path for catalog.sample; continuing")
        if isinstance(exc, TableNotSyncedError):
            raise HTTPException(status_code=404, detail=exc.detail)
        if isinstance(exc, FileNotFoundError):
            raise HTTPException(status_code=404, detail=f"table {table_id!r} not found")
        if isinstance(exc, PermissionError):
            from src.rbac import table_not_in_stack_message

            raise HTTPException(
                status_code=403,
                detail=table_not_in_stack_message(table_id),
            )
        if isinstance(exc, ValueError):
            raise HTTPException(
                status_code=400,
                detail={"error": "unsafe_identifier", "message": str(exc), "details": {}},
            )
        raise HTTPException(
            status_code=BqAccessError.HTTP_STATUS.get(exc.kind, 500),  # type: ignore[union-attr]
            detail={"error": exc.kind, "message": exc.message, "details": exc.details},  # type: ignore[union-attr]
        )
