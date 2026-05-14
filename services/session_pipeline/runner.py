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

import duckdb

from services.session_pipeline.contract import ProcessorResult, SessionProcessor
from services.session_pipeline.lib import compute_file_hash
from src.repositories.session_processor_state import SessionProcessorStateRepository

logger = logging.getLogger(__name__)


def resolve_user_id(
    conn: duckdb.DuckDBPyConnection,
    username: str,
) -> str | None:
    """Map a session-directory name to the stable ``users.id`` UUID.

    Two conventions exist for the directory name under
    ``/data/user_sessions/``:

    * **Session collector** writes under the OS username, which in
      current deployments equals the email local-part (e.g. ``alice``).
    * **Upload API** writes under ``user["id"]`` — a UUID.

    Resolution order:
    1. Exact match on ``users.id`` (covers the UUID path).
    2. Email local-part match: ``users.email LIKE '<username>@%'``.
       If multiple users share the same local-part (different domains),
       we pick the one that logged in most recently.
    3. Fallback: return ``None`` (orphaned / deleted user).
    """
    row = conn.execute(
        "SELECT id FROM users WHERE id = ?",
        [username],
    ).fetchone()
    if row:
        return row[0]
    row = conn.execute(
        "SELECT id FROM users WHERE email LIKE ? || '@%' ORDER BY updated_at DESC NULLS LAST LIMIT 1",
        [username],
    ).fetchone()
    if row:
        return row[0]
    return None


DEFAULT_SESSION_DATA_DIR = Path(os.environ.get("SESSION_DATA_DIR", "/data/user_sessions"))


def run_processor(
    conn: duckdb.DuckDBPyConnection,
    processor: SessionProcessor,
    session_data_dir: Path | None = None,
) -> dict[str, Any]:
    """Run *processor* against every unprocessed session in
    *session_data_dir* (defaults to $SESSION_DATA_DIR or /data/user_sessions).

    Returns a stats dict with: scanned, processed, skipped, errors,
    items_extracted, errors_detail. Caller (admin endpoint) puts this in the
    audit row and HTTP response body.
    """
    effective_dir = session_data_dir if session_data_dir is not None else DEFAULT_SESSION_DATA_DIR

    stats: dict[str, Any] = {
        "processor": processor.name,
        "scanned": 0,
        "processed": 0,
        "skipped": 0,
        "errors": 0,
        "items_extracted": 0,
        "errors_detail": [],
    }

    repo = SessionProcessorStateRepository(conn)
    candidates = repo.scan_unprocessed_for(processor.name, effective_dir)
    stats["scanned"] = len(candidates)

    if not candidates:
        logger.info("No sessions to process for processor=%s", processor.name)
        return stats

    # Pre-resolve user_id per directory name so each processor can
    # store the stable identity. Cache avoids repeated DB lookups when
    # one user has many sessions.
    _uid_cache: dict[str, str | None] = {}

    for username, jsonl_path in candidates:
        session_key = f"{username}/{jsonl_path.name}"
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

        if username not in _uid_cache:
            _uid_cache[username] = resolve_user_id(conn, username)
        resolved_uid = _uid_cache[username]

        try:
            try:
                result = processor.process_session(
                    jsonl_path,
                    username,
                    session_key,
                    conn,
                    user_id=resolved_uid,
                )
            except TypeError:
                result = processor.process_session(
                    jsonl_path,
                    username,
                    session_key,
                    conn,
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
            username=username,
            items_count=result.items_count,
            file_hash=file_hash,
        )
        stats["processed"] += 1
        stats["items_extracted"] += result.items_count

    logger.info(
        "Processor %s: scanned=%d processed=%d skipped=%d errors=%d items=%d",
        processor.name,
        stats["scanned"],
        stats["processed"],
        stats["skipped"],
        stats["errors"],
        stats["items_extracted"],
    )
    return stats
