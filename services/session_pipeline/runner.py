"""Per-processor runner — drives one SessionProcessor across all unprocessed
sessions in /data/user_sessions/. Each processor is invoked independently
(one call to run_processor per scheduler tick per processor); there is no
cross-processor coupling.

Failure handling mirrors the pre-refactor verification_detector behavior:
per-session try/except, on raise the state row is NOT written → the same
session will be retried on the next tick. There is no max_retries / dead
letter. A permanently malformed session will retry forever; that is a
known limitation we may revisit (out of scope for this refactor).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any


from services.session_pipeline.contract import ProcessorResult, SessionProcessor
from services.session_pipeline.lib import compute_file_hash

from src.repositories import (
    session_processor_state_repo,
    users_repo,
)

logger = logging.getLogger(__name__)


def resolve_user_identity(username: str) -> tuple[str | None, str | None]:
    """Map a session-directory name to ``(users.id, users.email)``.

    Two conventions exist for the directory name under
    ``/data/user_sessions/``:

    * **Session collector** writes under the OS username, which in
      current deployments equals the email local-part (e.g. ``alice``).
    * **Upload API** writes under ``user["id"]`` — a UUID.

    Resolution order:
    1. Exact match on ``users.id`` (covers the UUID path).
    2. Email local-part match: ``users.email LIKE '<username>@%'``.
       If multiple users share the same local-part (different domains),
       we pick the one most recently updated.
    3. Fallback: return ``(None, None)`` (orphaned / deleted user).

    Email is returned so the runner can normalise the ``username``
    column in ``usage_events`` / ``usage_session_summary`` to a stable
    human-readable identity regardless of which ingestion path the
    session arrived through — otherwise the admin telemetry dropdown
    lists the same user under both their UUID (upload API) and their
    email (REST event emitters).

    Routes through :func:`src.repositories.users_repo` (not a raw
    connection) so the lookup hits the active backend — a raw DuckDB
    query here silently returned no rows on Postgres instances.
    """
    repo = users_repo()
    row = repo.get_by_id(username)
    if row:
        return row["id"], row["email"]
    row = repo.get_by_email_prefix(username)
    if row:
        return row["id"], row["email"]
    return None, None


def resolve_user_id(username: str) -> str | None:
    """Backward-compatible wrapper returning just the resolved ``users.id``.

    Existing call sites (and tests) that only need the UUID stay
    unchanged; new code in ``run_processor`` uses
    :func:`resolve_user_identity` to get the email too.
    """
    uid, _ = resolve_user_identity(username)
    return uid


DEFAULT_SESSION_DATA_DIR = Path(os.environ.get("SESSION_DATA_DIR", "/data/user_sessions"))


def run_processor(
    processor: SessionProcessor,
    session_data_dir: Path | None = None,
    max_sessions_per_run: int | None = None,
) -> dict[str, Any]:
    """Run *processor* against every unprocessed session in
    *session_data_dir* (defaults to $SESSION_DATA_DIR or /data/user_sessions).

    Returns a stats dict with: scanned, processed, skipped, capped, errors,
    items_extracted, errors_detail. Caller (admin endpoint) puts this in the
    audit row and HTTP response body.

    ``max_sessions_per_run``, when set, caps how many candidates are actually
    processed in this call — the rest are left for the next scheduler tick.
    Bounds the worst-case wall-clock/CPU cost of a single invocation (each
    candidate can trigger multiple synchronous, blocking LLM calls); a burst
    of session closures landing in the same tick no longer processes
    unboundedly in one request. ``scanned`` always reflects the true total
    found; ``capped`` reports how many were deferred, so operators can see a
    forming backlog before it becomes one.

    No DB connection is accepted — every state read/write here and in every
    ``SessionProcessor.process_session`` implementation routes through the
    ``src.repositories`` factory. A prior ``conn`` parameter was threaded
    through but never read; at the caller (``POST
    /api/admin/run-session-processor``) it forced open the process-singleton
    DuckDB connection on every invocation, even on Postgres-backed instances.
    """
    effective_dir = session_data_dir if session_data_dir is not None else DEFAULT_SESSION_DATA_DIR

    stats: dict[str, Any] = {
        "processor": processor.name,
        "scanned": 0,
        "processed": 0,
        "skipped": 0,
        "capped": 0,
        "errors": 0,
        "items_extracted": 0,
        "errors_detail": [],
    }

    repo = session_processor_state_repo()
    candidates = repo.scan_unprocessed_for(processor.name, effective_dir)
    stats["scanned"] = len(candidates)

    if not candidates:
        logger.info("No sessions to process for processor=%s", processor.name)
        return stats

    if max_sessions_per_run is not None and len(candidates) > max_sessions_per_run:
        stats["capped"] = len(candidates) - max_sessions_per_run
        logger.info(
            "Processor %s: capping this run to %d/%d candidates; %d deferred to next tick",
            processor.name,
            max_sessions_per_run,
            len(candidates),
            stats["capped"],
        )
        candidates = candidates[:max_sessions_per_run]

    # Pre-resolve (user_id, email) per directory name so each processor
    # can store the stable identity. Cache avoids repeated DB lookups
    # when one user has many sessions. Email is used as the canonical
    # ``username`` written to usage_* tables so the admin telemetry
    # dropdown surfaces one row per user regardless of whether the
    # session arrived via /api/upload/sessions (UUID dir) or the legacy
    # collector (OS-username dir).
    _identity_cache: dict[str, tuple[str | None, str | None]] = {}

    for dir_name, jsonl_path in candidates:
        session_key = f"{dir_name}/{jsonl_path.name}"
        try:
            file_hash = compute_file_hash(jsonl_path)
        except Exception as e:
            logger.warning(
                "Cannot hash %s for processor=%s: %s",
                session_key,
                processor.name,
                e,
            )
            stats["errors"] += 1
            stats["errors_detail"].append({"session": session_key, "error": str(e)})
            continue

        # Hash-aware skip: scan_unprocessed_for returns every candidate; we
        # do the authoritative is_processed check here so the runner is the
        # single place that decides "this exact (processor, session, hash)
        # tuple is already done". Cost: one extra SELECT per candidate, but
        # only for files that survived directory scan.
        if repo.is_processed(processor.name, session_key, file_hash):
            stats["skipped"] += 1
            continue

        if dir_name not in _identity_cache:
            _identity_cache[dir_name] = resolve_user_identity(dir_name)
        resolved_uid, resolved_email = _identity_cache[dir_name]
        # Canonical username = email when the user resolves; fall back
        # to the directory name otherwise (orphaned uploads, sessions
        # for deleted users). The directory name remains the filesystem
        # lookup key via ``session_key`` (``<dir>/<file>``); ``username``
        # is purely the display/grouping identity for telemetry.
        canonical_username = resolved_email or dir_name

        try:
            result = processor.process_session(
                jsonl_path,
                canonical_username,
                session_key,
                user_id=resolved_uid,
            )
        except Exception as e:
            logger.exception(
                "Processor %s failed on %s — leaving state unwritten for retry",
                processor.name,
                session_key,
            )
            stats["errors"] += 1
            stats["errors_detail"].append({"session": session_key, "error": str(e)})
            continue

        if not isinstance(result, ProcessorResult):
            # Defensive: Protocol can't enforce the return type at runtime,
            # so a misbehaving processor that returns None or an arbitrary
            # dict shouldn't poison the state-write path. Treat it as zero
            # items but still mark processed — the alternative (raise) would
            # cause the same session to be retried forever.
            logger.warning(
                "Processor %s returned non-ProcessorResult on %s; coercing to empty result",
                processor.name,
                session_key,
            )
            result = ProcessorResult(items_count=0)

        repo.mark_processed(
            processor_name=processor.name,
            session_file=session_key,
            username=canonical_username,
            items_count=result.items_count,
            file_hash=file_hash,
        )
        stats["processed"] += 1
        stats["items_extracted"] += result.items_count

    logger.info(
        "Processor %s: scanned=%d processed=%d skipped=%d capped=%d errors=%d items=%d",
        processor.name,
        stats["scanned"],
        stats["processed"],
        stats["skipped"],
        stats["capped"],
        stats["errors"],
        stats["items_extracted"],
    )
    return stats
