"""Admin telemetry endpoints: /api/admin/usage/*.

Phase C.1: GET /api/admin/usage/export — stream telemetry export as csv|json|parquet
Phase C.4 (future): POST /api/admin/usage/reprocess, POST /api/admin/usage/prune
Phase C.3 (future): POST /api/admin/usage/ask  (LLM Text-to-SQL)

All endpoints admin-only. Export writes one audit_log row per call.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.auth.access import require_admin
from app.auth.dependencies import _get_db
from src.repositories.audit import AuditRepository

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
    conn.execute(copy_sql, params)

    def gen():
        try:
            with out_path.open("rb") as f:
                while True:
                    chunk = f.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk
        finally:
            try:
                out_path.unlink()
            except Exception:
                pass

    return StreamingResponse(
        gen(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": "attachment; filename=usage_events.parquet"},
    )
