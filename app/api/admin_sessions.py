"""Admin endpoints for browsing sessions across all users.

Per-user endpoints (`/api/admin/users/{user_id}/sessions/*`) live in
``admin_user_sessions.py``. This module adds:

- ``GET /api/admin/sessions/list``     — cross-user list, filterable
- ``GET /api/admin/sessions/kpis``     — top-bar numbers for the list page
- ``GET /api/admin/sessions/{username}/{session_file}/transcript``
                                        — parsed JSONL events for the viewer

Both backend paths reuse ``_session_data_dir`` + the filename regex from
``admin_user_sessions``; the goal is one source of truth for path safety.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.access import require_admin
from app.auth.dependencies import _get_db
from app.api.admin_user_sessions import _SESSION_FILE_RE, _session_data_dir
from services.session_pipeline.lib import parse_jsonl

from src.repositories import (
    audit_repo,
)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/sessions", tags=["admin-sessions"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _window_since(since_minutes: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=since_minutes)


def _build_where(
    since: datetime,
    username: Optional[str],
    model: Optional[str],
    only_errors: bool,
    q: Optional[str],
) -> tuple[str, dict]:
    where = ["started_at >= :since"]
    params: dict = {"since": since}
    if username:
        where.append("username = :username"); params["username"] = username
    if model:
        where.append("primary_model = :model"); params["model"] = model
    if only_errors:
        where.append("tool_errors > 0")
    if q:
        from src.sql_safe import pg_like_escape
        where.append(
            "(session_id LIKE :q ESCAPE '\\' OR session_file LIKE :q ESCAPE '\\')"
        )
        params["q"] = f"%{pg_like_escape(q)}%"
    return " AND ".join(where), params


# ---------------------------------------------------------------------------
# GET /api/admin/sessions/list
# ---------------------------------------------------------------------------

_VALID_SORT_KEYS = {
    "started_at": "started_at",
    "ended_at":   "ended_at",
    "tool_calls": "tool_calls",
    "tool_errors": "tool_errors",
    "active_seconds": "active_seconds",
    "username": "username",
    "primary_model": "primary_model",
}


@router.get("/list")
def list_sessions(
    since_minutes: int = Query(default=10080, ge=1, le=525600),   # default 7d
    username: Optional[str] = None,
    model: Optional[str] = None,
    only_errors: bool = False,
    q: Optional[str] = None,
    sort: str = Query(default="started_at:desc"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=50000),
    _user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    since = _window_since(since_minutes)
    where_sql, params = _build_where(since, username, model, only_errors, q)

    sort_col, _, sort_dir = sort.partition(":")
    col = _VALID_SORT_KEYS.get(sort_col, "started_at")
    direction = "ASC" if (sort_dir or "desc").lower() == "asc" else "DESC"

    import sqlalchemy as sa
    from src.db_pg import get_engine
    lim_params = {**params, "lim": limit, "off": offset}
    with get_engine().connect() as eng_conn:
        total = eng_conn.execute(
            sa.text(f"SELECT COUNT(*) FROM usage_session_summary WHERE {where_sql}"),
            params,
        ).scalar()
        rows = eng_conn.execute(
            sa.text(
                f"""SELECT session_file, session_id, username,
                          started_at, ended_at, active_seconds, wall_seconds,
                          user_messages, assistant_messages,
                          tool_calls, tool_errors,
                          skill_invocations, subagent_dispatches,
                          mcp_calls, slash_commands,
                          distinct_tools, distinct_skills, primary_model
                   FROM usage_session_summary WHERE {where_sql}
                   ORDER BY {col} {direction}
                   LIMIT :lim OFFSET :off"""
            ),
            lim_params,
        ).fetchall()
    cols = [
        "session_file","session_id","username",
        "started_at","ended_at","active_seconds","wall_seconds",
        "user_messages","assistant_messages",
        "tool_calls","tool_errors",
        "skill_invocations","subagent_dispatches",
        "mcp_calls","slash_commands",
        "distinct_tools","distinct_skills","primary_model",
    ]
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        for k in ("started_at", "ended_at"):
            v = d.get(k)
            if isinstance(v, datetime):
                d[k] = v.isoformat()
        out.append(d)
    return {
        "rows":        out,
        "total":       int(total or 0),
        "limit":       limit,
        "offset":      offset,
        "next_offset": offset + limit if (offset + limit) < (total or 0) else None,
    }


# ---------------------------------------------------------------------------
# GET /api/admin/sessions/kpis  +  /facets
# ---------------------------------------------------------------------------

@router.get("/kpis")
def kpis(
    since_minutes: int = Query(default=10080, ge=1, le=525600),
    username: Optional[str] = None,
    model: Optional[str] = None,
    only_errors: bool = False,
    q: Optional[str] = None,
    _user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    since = _window_since(since_minutes)
    where_sql, params = _build_where(since, username, model, only_errors, q)
    import sqlalchemy as sa
    from src.db_pg import get_engine
    with get_engine().connect() as eng_conn:
        row = eng_conn.execute(
            sa.text(
                f"""SELECT COUNT(*),
                          COUNT(DISTINCT username),
                          SUM(CASE WHEN tool_errors > 0 THEN 1 ELSE 0 END),
                          SUM(tool_calls),
                          SUM(tool_errors)
                   FROM usage_session_summary WHERE {where_sql}"""
            ),
            params,
        ).first()
    sessions_total, users, error_sessions, tool_calls_total, tool_errors_total = (
        int(x or 0) for x in row
    )
    error_rate = (tool_errors_total / tool_calls_total) if tool_calls_total else 0.0
    return {
        "sessions_total":  sessions_total,
        "distinct_users":  users,
        "error_sessions":  error_sessions,
        "tool_calls_total": tool_calls_total,
        "tool_errors_total": tool_errors_total,
        "tool_error_rate": round(error_rate, 4),
    }


@router.get("/facets")
def facets(
    since_minutes: int = Query(default=10080, ge=1, le=525600),
    _user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    since = _window_since(since_minutes)
    import sqlalchemy as sa
    from src.db_pg import get_engine
    with get_engine().connect() as eng_conn:
        users = eng_conn.execute(
            sa.text(
                "SELECT username, COUNT(*) AS n FROM usage_session_summary "
                "WHERE started_at >= :since AND username IS NOT NULL "
                "GROUP BY username ORDER BY n DESC LIMIT 50"
            ),
            {"since": since},
        ).fetchall()
        models = eng_conn.execute(
            sa.text(
                "SELECT primary_model, COUNT(*) AS n FROM usage_session_summary "
                "WHERE started_at >= :since AND primary_model IS NOT NULL "
                "GROUP BY primary_model ORDER BY n DESC LIMIT 30"
            ),
            {"since": since},
        ).fetchall()
    return {
        "users":  [{"value": r[0], "count": r[1]} for r in users],
        "models": [{"value": r[0], "count": r[1]} for r in models],
    }


# ---------------------------------------------------------------------------
# Transcript viewer
# ---------------------------------------------------------------------------

# Username constraint: same allowlist as the session-file regex (alnums + `._-`).
# Filesystem username is the local-part of an email today, so no `@` etc.
import re as _re
_USERNAME_RE = _re.compile(r"^[A-Za-z0-9._-]{1,200}$")


def _safe_session_path(username: str, session_file: str) -> Path:
    """Resolve a session jsonl path with three layers of guards.

    1. Both segments must match the allowlist regex; reject `..`, `/`, etc.
    2. After joining, ``resolve().relative_to(root)`` confirms no symlink
       escape moved the final path outside the user-sessions root.
    3. The file must end in ``.jsonl``.
    """
    if not _USERNAME_RE.match(username):
        raise HTTPException(status_code=400, detail="invalid username")
    if not _SESSION_FILE_RE.match(session_file):
        raise HTTPException(status_code=400, detail="invalid session_file")
    root = _session_data_dir().resolve()
    path = (root / username / session_file).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="path escape rejected")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="session not found")
    return path


def _flatten_text_content(content: Any) -> str:
    """Tool result `content` is often `list[{type:'text', text:'…'}]`. Flatten
    to a string preserving newlines for readable rendering."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def _render_transcript(turns: list[dict]) -> list[dict]:
    """Flatten a Claude Code session jsonl into a chronological list of
    render-ready event dicts. Each event carries enough context for the UI
    to show role / kind / text / tool-call payload / error flag.

    Three event kinds:
      - ``text``         (role=user|assistant)
      - ``tool_use``     (assistant requested a tool)
      - ``tool_result``  (user-role echo from Claude Code carrying tool output)
    Non-conversational turns (system, summary, file-history-snapshot…) are
    skipped; they're noise for an operator investigating a failure.
    """
    events: list[dict] = []
    for turn in turns:
        ttype = turn.get("type")
        if ttype not in ("user", "assistant"):
            continue
        ts = turn.get("timestamp")
        uuid = turn.get("uuid")
        msg = turn.get("message", {}) or {}
        role = msg.get("role") or ttype
        content = msg.get("content")

        if isinstance(content, str):
            events.append({
                "kind": "text", "role": role, "text": content,
                "ts": ts, "uuid": uuid,
            })
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                events.append({
                    "kind": "text", "role": role,
                    "text": block.get("text") or "",
                    "ts": ts, "uuid": uuid,
                })
            elif btype == "tool_use":
                events.append({
                    "kind": "tool_use",
                    "tool_name": block.get("name"),
                    "input": block.get("input"),
                    "tool_use_id": block.get("id"),
                    "ts": ts, "uuid": uuid,
                })
            elif btype == "tool_result":
                events.append({
                    "kind": "tool_result",
                    "tool_use_id": block.get("tool_use_id"),
                    "is_error": bool(block.get("is_error", False)),
                    "text":     _flatten_text_content(block.get("content")),
                    "ts": ts, "uuid": uuid,
                })
    return events


@router.get("/{username}/{session_file}/download")
def download(
    username: str,
    session_file: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Stream a single JSONL straight from disk. Path-safety guarded the
    same way as ``/transcript``. Audit-logged."""
    from fastapi.responses import StreamingResponse
    path = _safe_session_path(username, session_file)

    def _iter():
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(64 * 1024)
                if not chunk:
                    break
                yield chunk

    try:
        audit_repo().log(
            user_id=user.get("id"),
            action="session_download",
            resource=f"{username}/{session_file}",
            params={"bytes": path.stat().st_size},
            result="success",
            client_kind="web",
        )
    except Exception:
        logger.exception("audit_log write failed for session_download")

    return StreamingResponse(
        _iter(),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{session_file}"'},
    )


@router.get("/{username}/{session_file}/transcript")
def transcript(
    username: str,
    session_file: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    path = _safe_session_path(username, session_file)
    turns = parse_jsonl(path)
    events = _render_transcript(turns)

    import sqlalchemy as sa
    from src.db_pg import get_engine
    with get_engine().connect() as eng_conn:
        summary_row = eng_conn.execute(
            sa.text(
                "SELECT session_id, started_at, ended_at, active_seconds, "
                "wall_seconds, user_messages, assistant_messages, tool_calls, "
                "tool_errors, primary_model FROM usage_session_summary "
                "WHERE session_file = :sf"
            ),
            {"sf": f"{username}/{session_file}"},
        ).first()
    summary: dict[str, Any] = {}
    if summary_row:
        keys = (
            "session_id","started_at","ended_at","active_seconds","wall_seconds",
            "user_messages","assistant_messages","tool_calls","tool_errors",
            "primary_model",
        )
        summary = dict(zip(keys, summary_row))
        for k in ("started_at","ended_at"):
            v = summary.get(k)
            if isinstance(v, datetime):
                summary[k] = v.isoformat()

    # Audit: looking at someone else's transcript is a privacy-sensitive
    # operation; record actor + target + bytes scanned for traceability.
    try:
        audit_repo().log(
            user_id=user.get("id"),
            action="session.transcript_view",
            resource=f"{username}/{session_file}",
            params={"events": len(events)},
            result="success",
            client_kind="web",
        )
    except Exception:
        logger.exception("audit_log write failed for session.transcript_view")

    return {
        "username":     username,
        "session_file": session_file,
        "summary":      summary,
        "events":       events,
    }
