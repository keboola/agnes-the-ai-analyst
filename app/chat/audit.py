"""audit_log writer for chat events. Re-uses Agnes's existing audit table."""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def write_audit(
    *,
    user_email: str,
    action: str,
    details: dict[str, Any],
) -> None:
    """Best-effort insert into audit_log; failure is logged, not raised.

    Maps to the existing audit_log schema:
      user_id  → user_email (chat events use the email as the opaque identifier)
      action   → action
      params   → details dict

    Routes through the ``src.repositories`` factory (``audit_repo().log()``)
    so the row lands in whichever backend (DuckDB or Postgres) the
    deployment runs on — the prior raw ``conn.execute`` always targeted the
    DuckDB system connection, silently dropping chat audit rows on
    Postgres-backed instances.
    """
    try:
        from src.repositories import audit_repo

        audit_repo().log(
            user_id=user_email,
            action=action,
            params=details,
        )
    except Exception:
        logger.exception("audit_log write failed: action=%s", action)


def hash_args(args: Any) -> str:
    """Return first 16 hex chars of SHA-256 of the JSON-serialised args."""
    raw = json.dumps(args, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]
