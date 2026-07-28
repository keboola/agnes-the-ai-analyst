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

Backend-agnostic: the flip itself is an engine-specific UPDATE that lives
on the repository (``StoreSubmissionsRepository.reap_stuck_pending_llm``
for DuckDB, ``StoreSubmissionsPgRepository`` for Postgres). This module
only orchestrates — it resolves the right repo + audit sink from the
factory so it works on whichever backend the deployment runs. Passing a
raw DuckDB ``conn`` (the pre-fix API) silently no-ops on Postgres-backed
instances because the rows live in PG, not the local DuckDB; the factory
path closes that gap.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


def reap_stuck_llm_reviews(
    *,
    grace_seconds: int = 1800,
    subs_repo: Any = None,
    audit: Any = None,
) -> Dict[str, Any]:
    """Flip every ``pending_llm`` submission older than ``grace_seconds``
    to ``review_error``. Returns a summary dict for telemetry.

    Backend resolution: ``subs_repo`` / ``audit`` are resolved from the
    repository factory (``store_submissions_repo()`` / ``audit_repo()``)
    unless injected explicitly (tests). The factory picks DuckDB or
    Postgres per ``use_pg()`` — the only resolution correct on PG
    instances. The pre-fix API took a raw DuckDB ``conn`` and ran the
    flip SQL against it directly, which silently no-op'd on
    Postgres-backed deployments (rows live in PG, the conn pointed at an
    empty local DuckDB); that path is gone.

    ``grace_seconds`` should comfortably exceed the p99 LLM-review wall
    time. Default 1800s (30 min) covers Sonnet/Opus reviews of large
    bundles. Set ``0`` to disable (no-op return).
    """
    if grace_seconds <= 0:
        return {"skipped": True, "reaped": 0, "grace_seconds": grace_seconds}

    if subs_repo is None:
        from src.repositories import store_submissions_repo
        subs_repo = store_submissions_repo()
    if audit is None:
        from src.repositories import audit_repo
        audit = audit_repo()

    error_payload = {
        "risk_level": None,
        "summary": None,
        "findings": [],
        "template_placeholders_found": 0,
        "reviewed_by_model": None,
        "error": "timeout_or_crash",
    }

    reaped_rows = subs_repo.reap_stuck_pending_llm(
        grace_seconds=grace_seconds,
        error_payload=error_payload,
    )

    for sub_id, submitter_id in reaped_rows:
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

    reaped = len(reaped_rows)
    if reaped:
        logger.info(
            "reaper: flipped %d pending_llm rows to review_error "
            "(grace=%ds)",
            reaped, grace_seconds,
        )
    return {"skipped": False, "reaped": reaped, "grace_seconds": grace_seconds}
