"""Stuck-review reaper.

A submission stays at ``status='pending_llm'`` until the BackgroundTasks
worker writes a verdict. If the worker crashes between status flip and
verdict write (process kill, OOM, exception not caught by the runner's
catch-all), the row sits at pending_llm forever. Admin queue surfaces
it indefinitely; submitter never gets a verdict.

The reaper sweeps the queue every 15 minutes (scheduler-driven) and
flips any row older than ``grace_seconds`` (default 30 min) to
``review_error`` with ``llm_findings.error='timeout_or_crash'``. Admin
sees the entry under the *Needs review* chip with a Retry button.

Idempotent: runs at any cadence; only flips rows that have been pending
longer than the grace. Safe to call directly outside the scheduler for
on-demand cleanup.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import duckdb

from src.repositories.audit import AuditRepository

logger = logging.getLogger(__name__)


def reap_stuck_llm_reviews(
    conn: duckdb.DuckDBPyConnection,
    *,
    grace_seconds: int = 1800,
) -> Dict[str, Any]:
    """Flip every ``pending_llm`` submission older than ``grace_seconds``
    to ``review_error``. Returns a summary dict for telemetry.

    ``grace_seconds`` should comfortably exceed the p99 LLM-review wall
    time. Default 1800s (30 min) covers Sonnet/Opus reviews of large
    bundles. Set ``0`` to disable (no-op return).
    """
    if grace_seconds <= 0:
        return {"skipped": True, "reaped": 0, "grace_seconds": grace_seconds}

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=grace_seconds)
    rows = conn.execute(
        """SELECT id, submitter_id, entity_id
             FROM store_submissions
            WHERE status = 'pending_llm'
              AND created_at < ?""",
        [cutoff],
    ).fetchall()

    if not rows:
        return {"skipped": False, "reaped": 0, "grace_seconds": grace_seconds}

    audit = AuditRepository(conn)
    error_payload = {
        "risk_level": None,
        "summary": None,
        "findings": [],
        "template_placeholders_found": 0,
        "reviewed_by_model": None,
        "error": "timeout_or_crash",
    }
    now = datetime.now(timezone.utc)

    reaped = 0
    for sub_id, submitter_id, _entity_id in rows:
        conn.execute(
            """UPDATE store_submissions
                  SET status = 'review_error',
                      llm_findings = ?,
                      updated_at = ?
                WHERE id = ?
                  AND status = 'pending_llm'""",
            [json.dumps(error_payload), now, sub_id],
        )
        audit.log(
            user_id=submitter_id,
            action="store.submission.review_error",
            resource=f"store_submission:{sub_id}",
            params={
                "reason": "stuck_review_reaped",
                "grace_seconds": grace_seconds,
            },
            result="error",
        )
        reaped += 1

    logger.info(
        "reaper: flipped %d pending_llm rows to review_error "
        "(grace=%ds)",
        reaped, grace_seconds,
    )
    return {"skipped": False, "reaped": reaped, "grace_seconds": grace_seconds}
