"""Self-scoped user endpoints for the /home onboarding flow.

POST /api/me/onboarded toggles ``users.onboarded`` for the calling user
and writes an audit_log row distinguishing the trigger source:

- ``agnes_init``       — fired by the CLI's ``agnes init`` final step.
- ``self_acknowledged`` — fired by the on-page "I've already set this up"
  button shown to users who set up locally before /home shipped.
- ``self_unmark``      — fired by the on-page "Mark me as offboarded"
  button (visible once the user is onboarded).

The body's optional ``onboarded`` field defaults to ``True`` for
backward compat with existing ``agnes init`` calls. Pass ``false`` to
flip back — useful when an analyst wipes their workspace and wants the
inline install steps back, or when an operator demos the not-onboarded
view without an SQL UPDATE.

Idempotent — a second call still returns 200 and writes a second audit
row, so duplicate fires are visible without breaking the client. See
origin: docs/brainstorms/home-page-requirements.md §2 + §6.
"""

from __future__ import annotations

from typing import Literal

import duckdb
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth.dependencies import _get_db, get_current_user
from src.repositories.audit import AuditRepository

router = APIRouter(prefix="/api/me", tags=["me"])


class OnboardedRequest(BaseModel):
    source: Literal["agnes_init", "self_acknowledged", "self_unmark"] = "agnes_init"
    onboarded: bool = True


@router.post("/onboarded")
async def post_onboarded(
    body: OnboardedRequest = OnboardedRequest(),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    target = bool(body.onboarded)
    conn.execute(
        "UPDATE users SET onboarded = ? WHERE id = ?",
        [target, user["id"]],
    )
    AuditRepository(conn).log(
        user_id=user["id"],
        action="user_onboarded" if target else "user_offboarded",
        params={"source": body.source},
        result="ok",
    )
    return {"status": "ok", "onboarded": target}


# ---------------------------------------------------------------------------
# GET /api/me/home-stats — backing data for the /home status frame
# ---------------------------------------------------------------------------


_WINDOW_INTERVALS = {
    "24h": "INTERVAL 24 HOUR",
    "7d": "INTERVAL 7 DAY",
}


def _username_for_stats(user: dict) -> str:
    """Return the key that ``usage_session_summary.username`` holds for
    sessions uploaded by *user*.

    Production convention: ``app/api/upload.py`` writes JSONLs under
    ``${DATA_DIR}/user_sessions/<user_id>/``; the session-pipeline
    runner uses the directory name as the ``username`` column when
    extracting summaries. ``/profile/sessions`` reads the same dir
    keyed by ``user_id``. The column is historically named
    ``username`` but its current contents are user_ids — return the
    matching lookup key.
    """
    return user["id"]


def compute_home_stats(
    conn: duckdb.DuckDBPyConnection, user: dict, window: str = "24h"
) -> dict:
    """Pure helper that returns the home-stats payload for the given user.

    Shared by the HTTP endpoint and the /home Jinja handler (server-side
    initial render). Unknown windows clamp to ``24h`` so callers never
    need to pre-validate. Returns a dict with ISO-stringified
    ``last_pull_at`` (or None) so the same shape works for both JSON
    serialization and Jinja rendering.
    """
    interval = _WINDOW_INTERVALS.get(window)
    if interval is None:
        window = "24h"
        interval = _WINDOW_INTERVALS["24h"]

    username = _username_for_stats(user)

    # f-string interpolates only the validated interval literal above;
    # all user-controlled input flows through bound parameters.
    sql = f"""
        WITH win AS (
            SELECT current_timestamp - {interval} AS since
        ),
        sess AS (
            SELECT
                COUNT(*)                                 AS sessions,
                COALESCE(SUM(user_messages), 0)          AS prompts,
                COALESCE(SUM(input_tokens), 0)           AS input_tokens,
                COALESCE(SUM(output_tokens), 0)          AS output_tokens,
                COALESCE(SUM(cache_read_tokens), 0)      AS cache_read,
                COALESCE(SUM(cache_creation_tokens), 0)  AS cache_creation
            FROM usage_session_summary, win
            WHERE username = ? AND started_at >= win.since
        ),
        proj AS (
            SELECT COUNT(DISTINCT cwd) AS projects
            FROM usage_events, win
            WHERE username = ?
              AND cwd IS NOT NULL
              AND occurred_at >= win.since
        ),
        u AS (
            SELECT last_pull_at FROM users WHERE id = ?
        )
        SELECT
            u.last_pull_at,
            sess.sessions, sess.prompts,
            sess.input_tokens, sess.output_tokens,
            sess.cache_read, sess.cache_creation,
            proj.projects
        FROM u, sess, proj
    """
    row = conn.execute(sql, [username, username, user["id"]]).fetchone()

    if row is None:
        return {
            "window": window,
            "last_pull_at": None,
            "sessions": 0,
            "prompts": 0,
            "tokens": {
                "input": 0, "output": 0,
                "cache_read": 0, "cache_creation": 0,
                "total": 0,
            },
            "projects": 0,
        }

    (last_pull_at, sessions, prompts,
     input_t, output_t, cache_read, cache_creation, projects) = row
    return {
        "window": window,
        "last_pull_at": last_pull_at.isoformat() if last_pull_at else None,
        "sessions": int(sessions or 0),
        "prompts": int(prompts or 0),
        "tokens": {
            "input": int(input_t or 0),
            "output": int(output_t or 0),
            "cache_read": int(cache_read or 0),
            "cache_creation": int(cache_creation or 0),
            "total": int((input_t or 0) + (output_t or 0)
                         + (cache_read or 0) + (cache_creation or 0)),
        },
        "projects": int(projects or 0),
    }


@router.get("/home-stats")
async def get_home_stats(
    window: str = "24h",
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Return the five counters rendered in the /home status frame for
    the calling user, over a 24-hour or 7-day window.

    Single round-trip: one DuckDB query joins ``users``,
    ``usage_session_summary``, and ``usage_events`` so the homepage
    renders without N+1. Missing rows (new user, no telemetry yet)
    surface as zeros / null rather than 404 — the frame still renders
    cleanly for first-day analysts.
    """
    return compute_home_stats(conn, user, window)
