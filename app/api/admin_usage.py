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
from src.repositories.audit import AuditRepository
from src.usage_ask import (
    RESPONSE_SCHEMA,
    SYSTEM_PROMPT,
    build_prompt,
    validate_select_only,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/usage", tags=["admin-usage"])

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
    where, params = ["1=1"], []
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"invalid since: {since}")
        where.append("occurred_at >= ?")
        params.append(since_dt)
    if until:
        try:
            until_dt = datetime.fromisoformat(until.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"invalid until: {until}")
        where.append("occurred_at < ?")
        params.append(until_dt)
    if user_id:
        where.append("username = ?")
        params.append(user_id)
    if source:
        where.append("source = ?")
        params.append(source)

    sql = f"SELECT * FROM usage_events WHERE {' AND '.join(where)} ORDER BY occurred_at"
    # Row count for audit (one extra query — acceptable for audit fidelity)
    cnt_sql = f"SELECT COUNT(*) FROM usage_events WHERE {' AND '.join(where)}"
    row_count = int(conn.execute(cnt_sql, params).fetchone()[0])

    audit_params = {
        "format": format,
        "since": since,
        "until": until,
        "user_id": user_id,
        "source": source,
        "row_count": row_count,
    }
    try:
        AuditRepository(conn).log(
            user_id=user.get("id"),
            action="usage.export",
            params=audit_params,
            result="success",
            client_kind="web",
        )
    except Exception:
        logger.exception("audit_log write failed for usage.export; continuing")

    if format == "csv":
        return _stream_csv(conn, sql, params)
    elif format == "json":
        return _stream_ndjson(conn, sql, params)
    else:
        return _stream_parquet(conn, sql, params)


def _stream_csv(conn, sql, params):
    def gen():
        rel = conn.execute(sql, params)
        cols = [d[0] for d in rel.description]
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)
        for row in rel.fetchall():
            w.writerow(row)
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)

    return StreamingResponse(
        gen(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=usage_events.csv"},
    )


def _stream_ndjson(conn, sql, params):
    def gen():
        rel = conn.execute(sql, params)
        cols = [d[0] for d in rel.description]
        for row in rel.fetchall():
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


def _stream_parquet(conn, sql, params):
    import tempfile

    tmp = tempfile.NamedTemporaryFile(suffix=".parquet", delete=False)
    tmp.close()
    out_path = Path(tmp.name)
    # DuckDB COPY embeds the filter directly — use the same params
    copy_sql = f"COPY ({sql}) TO '{out_path}' (FORMAT PARQUET)"
    try:
        conn.execute(copy_sql, params)
    except Exception:
        # Ensure the temp file is cleaned up if COPY fails before we hand off to StreamingResponse
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
            AuditRepository(conn).log(
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
        rel = conn.execute(validated_sql)
        cols = [d[0] for d in rel.description]
        rows = rel.fetchall()
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
            AuditRepository(conn).log(
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
        AuditRepository(conn).log(
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
      - session_processor_state WHERE processor_name='usage'
        (so the next scheduler tick re-scans every JSONL)
      - usage_events
      - usage_session_summary
      - usage_tool_daily
      - usage_plugin_daily

    Verification processor's state untouched (composite PK isolates each processor).
    Audit-logged with deleted-row counts.
    """
    counts = {}
    try:
        conn.execute("BEGIN")
        n_state = conn.execute(
            "DELETE FROM session_processor_state WHERE processor_name = 'usage' RETURNING 1"
        ).fetchall()
        counts["state_rows"] = len(n_state)
        n_events = conn.execute("DELETE FROM usage_events RETURNING 1").fetchall()
        counts["events"] = len(n_events)
        n_sum = conn.execute("DELETE FROM usage_session_summary RETURNING 1").fetchall()
        counts["summaries"] = len(n_sum)
        n_tool = conn.execute("DELETE FROM usage_tool_daily RETURNING 1").fetchall()
        counts["tool_daily"] = len(n_tool)
        n_plugin = conn.execute("DELETE FROM usage_plugin_daily RETURNING 1").fetchall()
        counts["plugin_daily"] = len(n_plugin)
        conn.execute("COMMIT")
    except Exception as e:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        logger.exception("reprocess failed")
        raise HTTPException(status_code=500, detail=f"reprocess failed: {e}")

    try:
        AuditRepository(conn).log(
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
        before = conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        conn.execute(
            "DELETE FROM usage_events WHERE occurred_at < CURRENT_DATE - INTERVAL (?) DAY",
            [retention],
        )
        after = conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        deleted = before - after
    except Exception as e:
        logger.exception("prune failed")
        raise HTTPException(status_code=500, detail=f"prune failed: {e}")

    try:
        AuditRepository(conn).log(
            user_id=user.get("id"),
            action="usage.prune",
            params={"retention_days": retention, "deleted": deleted, "remaining": after},
            result="success",
        )
    except Exception:
        logger.exception("audit_log write failed for usage.prune; continuing")

    return {"status": "ok", "retention_days": retention, "deleted": deleted, "remaining": after}
