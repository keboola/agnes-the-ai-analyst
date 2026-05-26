"""Admin telemetry endpoints: /api/admin/usage/*.

Phase C.1: GET /api/admin/usage/export — stream telemetry export as csv|json|parquet
Phase C.3: POST /api/admin/usage/ask  — LLM Text-to-SQL over usage_* tables
Phase C.4: POST /api/admin/usage/reprocess, POST /api/admin/usage/prune

All endpoints admin-only. Export writes one audit_log row per call.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

import duckdb
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.auth.access import require_admin
from app.auth.dependencies import _get_db
from connectors.llm.anthropic_provider import AnthropicExtractor
from connectors.llm.exceptions import (
    LLMAuthError,
    LLMFormatError,
    LLMRateLimitError,
    LLMRefusalError,
    LLMTimeoutError,
)

from src.repositories import (
    audit_repo,
)
from src.usage_ask import (
    RESPONSE_SCHEMA,
    SYSTEM_PROMPT,
    build_prompt,
    validate_select_only,
)


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/telemetry", tags=["admin-telemetry"])

_FORMATS = ("csv", "json", "parquet")


@router.get("/export")
def export_usage(
    format: Literal["csv", "json", "parquet"] = Query("csv"),
    since: Optional[str] = Query(None, description="ISO date or datetime; events with occurred_at >= since"),
    until: Optional[str] = Query(None, description="ISO date or datetime; events with occurred_at < until"),
    user_id: Optional[str] = Query(None, description="Filter to a single user_id"),
    source: Optional[Literal["curated", "flea", "builtin"]] = Query(None),
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Stream usage_events filtered by since/until/user_id/source.

    CSV: standard library `csv.writer`, one row per event with all columns.
    JSON: streaming NDJSON (one JSON object per line) — easier to pipe + tail.
    Parquet: DuckDB `COPY (SELECT ...) TO '<tmp>.parquet'` then stream the file.
    """
    where, params = ["1=1"], {}
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"invalid since: {since}")
        where.append("occurred_at >= :since")
        params["since"] = since_dt
    if until:
        try:
            until_dt = datetime.fromisoformat(until.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"invalid until: {until}")
        where.append("occurred_at < :until")
        params["until"] = until_dt
    if user_id:
        where.append("username = :uname")
        params["uname"] = user_id
    if source:
        where.append("source = :source")
        params["source"] = source

    sql = f"SELECT * FROM usage_events WHERE {' AND '.join(where)} ORDER BY occurred_at"
    cnt_sql = f"SELECT COUNT(*) FROM usage_events WHERE {' AND '.join(where)}"
    import sqlalchemy as sa
    from src.db_pg import get_engine
    with get_engine().connect() as eng_conn:
        row_count = int(eng_conn.execute(sa.text(cnt_sql), params).scalar() or 0)

    audit_params = {
        "format": format,
        "since": since,
        "until": until,
        "user_id": user_id,
        "source": source,
        "row_count": row_count,
    }
    try:
        audit_repo().log(
            user_id=user.get("id"),
            action="usage.export",
            params=audit_params,
            result="success",
            client_kind="web",
        )
    except Exception:
        logger.exception("audit_log write failed for usage.export; continuing")

    if format == "csv":
        return _stream_csv(sql, params)
    elif format == "json":
        return _stream_ndjson(sql, params)
    else:
        return _stream_parquet(sql, params)


def _stream_csv(sql, params):
    import sqlalchemy as sa
    from src.db_pg import get_engine

    def gen():
        with get_engine().connect() as eng_conn:
            result = eng_conn.execute(sa.text(sql), params)
            cols = list(result.keys())
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(cols)
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)
            for row in result:
                w.writerow(row)
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate(0)

    return StreamingResponse(
        gen(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=usage_events.csv"},
    )


def _stream_ndjson(sql, params):
    import sqlalchemy as sa
    from src.db_pg import get_engine

    def gen():
        with get_engine().connect() as eng_conn:
            result = eng_conn.execute(sa.text(sql), params)
            cols = list(result.keys())
            for row in result:
                d = {}
                for k, v in zip(cols, row):
                    if isinstance(v, datetime):
                        d[k] = v.isoformat()
                    else:
                        d[k] = v
                yield json.dumps(d) + "\n"

    return StreamingResponse(
        gen(),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": "attachment; filename=usage_events.ndjson"},
    )


def _stream_parquet(sql, params):
    """Stream a parquet file built via PyArrow from PG rows.

    DuckDB's ``COPY (SELECT ...) TO '<path>' (FORMAT PARQUET)`` doesn't
    exist in PG; the equivalent is to fetch the result set into an
    Arrow table and write it via pyarrow.parquet.write_table. Holds
    the whole result in memory — acceptable for the admin export use
    case (audit-logged, filter-scoped, typically tens of thousands of
    rows at most).
    """
    import tempfile
    import pyarrow as pa
    import pyarrow.parquet as pq
    import sqlalchemy as sa
    from src.db_pg import get_engine

    tmp = tempfile.NamedTemporaryFile(suffix=".parquet", delete=False)
    tmp.close()
    out_path = Path(tmp.name)
    try:
        with get_engine().connect() as eng_conn:
            result = eng_conn.execute(sa.text(sql), params)
            cols = list(result.keys())
            rows = result.fetchall()
        # Transpose to column-major for Arrow. Empty result set still
        # produces a schemaless Arrow table — Arrow can't infer column
        # types from zero rows, so fall back to all-string in that case.
        if rows:
            data = {c: [r[i] for r in rows] for i, c in enumerate(cols)}
            table = pa.table(data)
        else:
            table = pa.table({c: pa.array([], type=pa.string()) for c in cols})
        pq.write_table(table, str(out_path))
    except Exception:
        out_path.unlink(missing_ok=True)
        raise

    def gen():
        try:
            with out_path.open("rb") as f:
                while True:
                    chunk = f.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk
        finally:
            out_path.unlink(missing_ok=True)

    return StreamingResponse(
        gen(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": "attachment; filename=usage_events.parquet"},
    )


# ---------------------------------------------------------------------------
# POST /api/admin/usage/ask — LLM Text-to-SQL (Phase C.3)
# ---------------------------------------------------------------------------

_ASK_MODEL = os.environ.get("USAGE_ASK_MODEL", "claude-haiku-4-5-20251001")


@router.post("/ask")
def ask_usage(
    payload: dict = Body(...),
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Translate a natural-language question to SELECT-only SQL via Anthropic + execute.

    Returns the generated SQL even when validation rejects it, so the
    admin sees what the LLM tried.
    """
    question = (payload.get("question") or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is required")
    if len(question) > 1000:
        raise HTTPException(status_code=400, detail="question too long (>1000 chars)")

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="ANTHROPIC_API_KEY is not configured on the server. Set it in instance env / .env_overlay.",
        )

    extractor = AnthropicExtractor(api_key=api_key, model=_ASK_MODEL)
    t0 = time.monotonic()
    try:
        llm_out = extractor.extract_json(
            prompt=build_prompt(question),
            max_tokens=1024,
            json_schema=RESPONSE_SCHEMA,
            schema_name="usage_ask_response",
            system=SYSTEM_PROMPT,
        )
    except LLMAuthError:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY is invalid")
    except LLMRateLimitError:
        raise HTTPException(status_code=503, detail="LLM rate limit — try again in a moment")
    except LLMTimeoutError:
        raise HTTPException(status_code=503, detail="LLM timeout — try again")
    except LLMRefusalError:
        raise HTTPException(status_code=400, detail="LLM refused the request (probably an unsafe question)")
    except LLMFormatError:
        raise HTTPException(status_code=502, detail="LLM returned non-JSON output — try rephrasing")
    except Exception as e:
        logger.exception("usage.ask LLM call failed")
        raise HTTPException(status_code=502, detail=f"LLM call failed: {e}")
    llm_ms = int((time.monotonic() - t0) * 1000)

    sql = llm_out.get("sql") or ""
    rationale = llm_out.get("rationale") or ""

    try:
        validated_sql = validate_select_only(sql)
    except ValueError as e:
        # Return 200 with rejection details so admin sees what the LLM tried.
        try:
            audit_repo().log(
                user_id=user.get("id"),
                action="usage.ask",
                params={"question": question, "sql": sql, "rejected": str(e), "llm_ms": llm_ms},
                result="error.invalid_sql",
            )
        except Exception:
            logger.exception("audit_log write failed for usage.ask rejection")
        return {
            "question": question,
            "sql": sql,
            "rationale": rationale,
            "rejected": str(e),
            "rows": None,
            "row_count": 0,
            "llm_ms": llm_ms,
        }

    # Execute the validated SQL with a row cap (defense in depth even though prompt asks for LIMIT)
    exec_t0 = time.monotonic()
    try:
        import sqlalchemy as sa
        from src.db_pg import get_engine
        with get_engine().connect() as eng_conn:
            result = eng_conn.execute(sa.text(validated_sql))
            cols = list(result.keys())
            rows = result.fetchall()
        if len(rows) > 1000:
            rows = rows[:1000]
            truncated = True
        else:
            truncated = False
        row_dicts = [
            {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in zip(cols, r)}
            for r in rows
        ]
    except Exception as e:
        logger.exception("usage.ask SQL execution failed")
        try:
            audit_repo().log(
                user_id=user.get("id"),
                action="usage.ask",
                params={"question": question, "sql": validated_sql, "error": str(e), "llm_ms": llm_ms},
                result="error.exec_failed",
            )
        except Exception:
            pass
        raise HTTPException(status_code=400, detail=f"SQL execution failed: {e}")
    exec_ms = int((time.monotonic() - exec_t0) * 1000)

    try:
        audit_repo().log(
            user_id=user.get("id"),
            action="usage.ask",
            params={
                "question": question,
                "sql": validated_sql,
                "row_count": len(row_dicts),
                "llm_ms": llm_ms,
                "exec_ms": exec_ms,
            },
            result="success",
        )
    except Exception:
        logger.exception("audit_log write failed for usage.ask success")

    return {
        "question": question,
        "sql": validated_sql,
        "rationale": rationale,
        "columns": cols,
        "rows": row_dicts,
        "row_count": len(row_dicts),
        "truncated": truncated,
        "llm_ms": llm_ms,
        "exec_ms": exec_ms,
    }


# ---------------------------------------------------------------------------
# POST /api/admin/usage/reprocess — force re-extraction (Phase C.4)
# ---------------------------------------------------------------------------


@router.post("/reprocess")
def reprocess_usage(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Force re-extraction of all sessions for the usage processor.

    DELETEs:
      - session_processor_state WHERE processor_name='usage' (so the next
        scheduler tick re-scans every JSONL) + processor_name='marketplace_rollup_30d'
        (forces 30d window rebuild on the next tick)
      - usage_events
      - usage_session_summary
      - usage_tool_daily (legacy)
      - usage_marketplace_item_daily
      - usage_marketplace_item_window

    Verification processor's state untouched (composite PK isolates each processor).
    Audit-logged with deleted-row counts.
    """
    counts = {}
    try:
        import sqlalchemy as sa
        from src.db_pg import get_engine
        with get_engine().begin() as eng_conn:
            r = eng_conn.execute(sa.text(
                "DELETE FROM session_processor_state "
                "WHERE processor_name IN ('usage', 'marketplace_rollup_30d')"
            ))
            counts["state_rows"] = r.rowcount or 0
            r = eng_conn.execute(sa.text("DELETE FROM usage_events"))
            counts["events"] = r.rowcount or 0
            r = eng_conn.execute(sa.text("DELETE FROM usage_session_summary"))
            counts["summaries"] = r.rowcount or 0
            r = eng_conn.execute(sa.text("DELETE FROM usage_tool_daily"))
            counts["tool_daily"] = r.rowcount or 0
            r = eng_conn.execute(sa.text("DELETE FROM usage_marketplace_item_daily"))
            counts["marketplace_item_daily"] = r.rowcount or 0
            r = eng_conn.execute(sa.text("DELETE FROM usage_marketplace_item_window"))
            counts["marketplace_item_window"] = r.rowcount or 0
    except Exception as e:
        logger.exception("reprocess failed")
        raise HTTPException(status_code=500, detail=f"reprocess failed: {e}")

    try:
        audit_repo().log(
            user_id=user.get("id"),
            action="usage.reprocess",
            params=counts,
            result="success",
        )
    except Exception:
        logger.exception("audit_log write failed for usage.reprocess; continuing")

    return {"status": "ok", "deleted": counts}


# ---------------------------------------------------------------------------
# POST /api/admin/usage/prune — retention-based event pruning (Phase C.4)
# ---------------------------------------------------------------------------


@router.post("/prune")
def prune_usage(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Delete usage_events older than USAGE_EVENTS_RETENTION_DAYS.

    Default retention: env var unset or ``0`` → no pruning (forever).
    Daily rollup tables untouched — they're tiny and lossy-by-design.
    """
    retention = int(os.environ.get("USAGE_EVENTS_RETENTION_DAYS", "0") or 0)
    if retention <= 0:
        return {"status": "skipped", "reason": "USAGE_EVENTS_RETENTION_DAYS unset or 0"}
    try:
        import sqlalchemy as sa
        from src.db_pg import get_engine
        # Interval value interpolated as a Python int — retention is the
        # post-parsed integer (positive after the >0 guard above), not
        # operator-supplied; PG's parametriser refuses to type-coerce an
        # interval through a positional placeholder.
        days = int(retention)
        with get_engine().begin() as eng_conn:
            before = eng_conn.execute(
                sa.text("SELECT COUNT(*) FROM usage_events")
            ).scalar()
            eng_conn.execute(
                sa.text(
                    f"DELETE FROM usage_events "
                    f"WHERE occurred_at < CURRENT_DATE - INTERVAL '{days} days'"
                )
            )
            after = eng_conn.execute(
                sa.text("SELECT COUNT(*) FROM usage_events")
            ).scalar()
        deleted = (before or 0) - (after or 0)
    except Exception as e:
        logger.exception("prune failed")
        raise HTTPException(status_code=500, detail=f"prune failed: {e}")

    try:
        audit_repo().log(
            user_id=user.get("id"),
            action="usage.prune",
            params={"retention_days": retention, "deleted": deleted, "remaining": after},
            result="success",
        )
    except Exception:
        logger.exception("audit_log write failed for usage.prune; continuing")

    return {"status": "ok", "retention_days": retention, "deleted": deleted, "remaining": after}
