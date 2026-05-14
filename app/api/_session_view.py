"""Shared view-builder for per-user session listings.

One source of truth for the three views that list a user's uploaded
Claude Code sessions:

- ``/profile/sessions`` — user-self FS view with verification status.
- ``/api/me/stats/sessions`` — user-self Stats-tab table.
- ``/api/admin/users/{user_id}/sessions`` — admin per-user view.

Pre-refactor each view walked the filesystem independently, joined
either ``session_processor_state`` (for one specific processor) or
``usage_session_summary``, and assembled differently-shaped rows.
The classic divergent-readers symptom surfaced when:

- A session that had been processed by the ``usage`` processor (5min
  cadence) but not yet by ``verification`` (15min cadence) showed up
  as ``pending`` on /profile/sessions while /me/stats happily
  reported its token totals. Two views, two truths.

This helper builds a single unified row per JSONL — FS metadata +
both processor states (verification + usage) + the usage processor's
aggregate (token counts, prompts, tool_calls, primary_model) — and
returns the list ordered by upload time. Callers project whichever
fields their template needs.

All views key by ``user["id"]`` (the directory name under
``${DATA_DIR}/user_sessions/`` and the value stored in the
``username`` column of usage_session_summary, despite that column's
historical name).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

logger = logging.getLogger(__name__)


def _user_sessions_dir(user_id: str) -> Path:
    """Filesystem root the upload + readers share.

    Matches `/profile/sessions` (`${DATA_DIR}/user_sessions/<user_id>/`)
    and `app/api/upload.py`'s write path; the session-pipeline runner
    (`services/session_pipeline/runner.py`) walks this with the leaf
    directory name becoming the ``username`` column value.
    """
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    return data_dir / "user_sessions" / user_id


def get_user_sessions_view(
    conn: duckdb.DuckDBPyConnection,
    user_id: str,
) -> list[dict[str, Any]]:
    """Return one unified row per session JSONL the user has uploaded.

    Each row carries:

    - ``name`` — bare filename (e.g. ``abc.jsonl``)
    - ``session_file`` — ``f"{user_id}/{name}"`` (matches the
      ``session_processor_state.session_file`` PK and
      ``usage_session_summary.session_file``)
    - ``size_bytes``, ``size_kb``
    - ``uploaded_at`` (file mtime, UTC)
    - ``processors`` — ``dict[name, {processed_at, items_extracted, file_hash}]``
      with one entry per row found in ``session_processor_state``. A
      processor that hasn't run on this session is **absent** from the
      dict (caller checks ``if 'usage' in row['processors']``).
    - ``summary`` — usage_session_summary aggregates if the usage
      processor has run, else ``None``. Carries: ``primary_model``,
      ``user_messages``, ``tool_calls``, ``input_tokens``,
      ``output_tokens``, ``cache_read_tokens``, ``cache_creation_tokens``,
      ``tokens_total``, ``started_at``, ``ended_at``.

    Ordering: newest upload first. Empty list if the user has no
    sessions on disk.
    """
    user_dir = _user_sessions_dir(user_id)
    if not user_dir.is_dir():
        return []

    # FS scan with OSError tolerance per Devin Review on #179
    # (`/profile/sessions`).
    statted: list[tuple[Path, os.stat_result]] = []
    for jsonl in user_dir.glob("*.jsonl"):
        try:
            st = jsonl.stat()
        except OSError:
            continue
        statted.append((jsonl, st))
    statted.sort(key=lambda pair: pair[1].st_mtime, reverse=True)
    if not statted:
        return []

    keys = [f"{user_id}/{p.name}" for p, _ in statted]
    placeholders = ",".join("?" for _ in keys)

    # All processor states for this user's sessions, single query.
    proc_rows = conn.execute(
        f"""
        SELECT processor_name, session_file, processed_at,
               items_extracted, file_hash
        FROM session_processor_state
        WHERE session_file IN ({placeholders})
        """,
        keys,
    ).fetchall()
    proc_by_key: dict[str, dict[str, dict]] = {}
    for proc_name, sf, pa, items, fh in proc_rows:
        proc_by_key.setdefault(sf, {})[proc_name] = {
            "processed_at": pa.isoformat() if pa and hasattr(pa, "isoformat") else pa,
            "items_extracted": items,
            "file_hash": fh,
        }

    # Usage aggregates for sessions where the usage processor has run.
    # Wrapped in try/except so a partially-migrated DB (table missing)
    # doesn't 500 the page — the table-rebuild path in v44 fresh installs
    # always creates it, but be defensive.
    summary_by_key: dict[str, dict] = {}
    try:
        sum_rows = conn.execute(
            f"""
            SELECT session_file, session_id, started_at, ended_at,
                   active_seconds, wall_seconds,
                   user_messages, tool_calls, tool_errors,
                   primary_model,
                   input_tokens, output_tokens,
                   cache_read_tokens, cache_creation_tokens
            FROM usage_session_summary
            WHERE session_file IN ({placeholders})
            """,
            keys,
        ).fetchall()
        sum_cols = [d[0] for d in conn.description]
        for r in sum_rows:
            d = dict(zip(sum_cols, r))
            for k in ("started_at", "ended_at"):
                v = d.get(k)
                if v is not None and hasattr(v, "isoformat"):
                    d[k] = v.isoformat()
            d["tokens_total"] = (
                int(d.get("input_tokens") or 0)
                + int(d.get("output_tokens") or 0)
                + int(d.get("cache_read_tokens") or 0)
                + int(d.get("cache_creation_tokens") or 0)
            )
            summary_by_key[d["session_file"]] = d
    except Exception:
        logger.exception("usage_session_summary lookup failed; degrading view")

    out: list[dict[str, Any]] = []
    for jsonl, st in statted:
        key = f"{user_id}/{jsonl.name}"
        mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
        out.append({
            "name": jsonl.name,
            "session_file": key,
            "size_bytes": st.st_size,
            "size_kb": round(st.st_size / 1024, 1),
            "uploaded_at": mtime,
            "processors": proc_by_key.get(key, {}),
            "summary": summary_by_key.get(key),
        })
    return out
