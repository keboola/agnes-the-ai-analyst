"""audit_log writer for chat events. Re-uses Agnes's existing audit table."""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

import duckdb

logger = logging.getLogger(__name__)


def write_audit(
    conn: duckdb.DuckDBPyConnection,
    *,
    user_email: str,
    action: str,
    details: dict[str, Any],
) -> None:
    """Best-effort insert into audit_log; failure is logged, not raised.

    Maps to the existing audit_log schema:
      user_id  → user_email (chat events use the email as the opaque identifier)
      action   → action
      params   → JSON-serialised details dict
    """
    try:
        conn.execute(
            "INSERT INTO audit_log (id, timestamp, user_id, action, params) VALUES (?, ?, ?, ?, ?)",
            [
                _gen_id(),
                datetime.now(timezone.utc),
                user_email,
                action,
                json.dumps(details),
            ],
        )
    except Exception:
        logger.exception("audit_log write failed: action=%s", action)


def hash_args(args: Any) -> str:
    """Return first 16 hex chars of SHA-256 of the JSON-serialised args."""
    raw = json.dumps(args, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _gen_id() -> str:
    import secrets
    return f"aud_{secrets.token_hex(8)}"
