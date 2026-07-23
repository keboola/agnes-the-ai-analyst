"""Repository for the ``idempotency_keys`` table (v96) — replay storage for
the agent-as-API surface (`POST /api/v1/agents/{slug}/responses`, Task 9).

A key is scoped to `(key, owner_user_id, agent_id)` — the same idempotency
key string can be reused by a different owner or against a different agent
without colliding. Each stored row carries the sha256 of the raw request
body (`request_hash`) alongside the previously-computed response, so a
replay with an IDENTICAL body can be served straight from the row while a
replay with a DIFFERENT body under the same key is detectable as key reuse
(the caller maps that to `409 idempotency_key_reuse`, not handled here).

``get`` never returns an expired row — TTL enforcement lives here, not at
the call site, so every caller gets it for free.

**In-flight reservation (``reserve``/``fulfill``, review carry-over on Task
9).** ``get``-miss-then-``put`` has a race: two concurrent requests under the
same key can both miss ``get`` and both run the underlying (expensive,
side-effecting) work before either calls ``put``. ``reserve()`` closes that
window by inserting a placeholder row (``response_body``/``status_code``
NULL) immediately after the miss — the composite PRIMARY KEY makes a second
concurrent ``reserve()`` for the same triple fail, so the caller can turn
that into a clean 409 instead of double-executing. ``fulfill()`` finalizes a
reservation with the real response (delegates to ``put()`` — see its
docstring). A reservation left unfulfilled (the request crashed, or is
still legitimately running) is deliberately given a SHORT expiry
(``RESERVATION_TTL_S``, independent of the caller's full replay-cache
``ttl_s``) — ``get()``'s existing expiry check then treats a stale
reservation exactly like a miss, and ``reserve()`` itself clears a stale
conflicting row before inserting, so a crashed request can never
permanently wedge a key.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import duckdb

#: Ceiling on how long an unfulfilled reservation (``response_body IS
#: NULL``) stays "live" before ``get()``/``reserve()`` treat it as stale and
#: let a fresh attempt through — independent of the caller's full
#: replay-cache ``ttl_s`` (which only takes effect once ``fulfill()`` runs).
#: 15 minutes comfortably covers the agent-as-API surface's own bounds
#: (sync `timeout_s` maxes out at 600s; the background job's own internal
#: wait, `AGNES_AGENT_RESPONSE_JOB_TIMEOUT_S`, defaults to 1800s but that
#: path never reserves an idempotency key mid-run — only the initiating
#: HTTP call does, before either the sync wait or the enqueue).
RESERVATION_TTL_S = 900


class IdempotencyRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def _row_to_dict(self, row) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, row))

    def get(self, key: str, owner_user_id: str, agent_id: str) -> Optional[Dict[str, Any]]:
        """Return the stored row for `(key, owner_user_id, agent_id)`, or
        ``None`` if absent OR expired (an expired row is treated exactly
        like a miss — callers re-run the request and `put()` overwrites
        it)."""
        row = self.conn.execute(
            """SELECT * FROM idempotency_keys
            WHERE key = ? AND owner_user_id = ? AND agent_id = ?""",
            [key, owner_user_id, agent_id],
        ).fetchone()
        record = self._row_to_dict(row)
        if record is None:
            return None
        expires_at = record.get("expires_at")
        if expires_at is not None:
            if isinstance(expires_at, str):
                expires_at = datetime.fromisoformat(expires_at)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > expires_at:
                return None
        return record

    def put(
        self,
        key: str,
        owner_user_id: str,
        agent_id: str,
        request_hash: str,
        response_body: str,
        status_code: int,
        ttl_s: int,
    ) -> None:
        """Insert-or-replace the row for `(key, owner_user_id, agent_id)`.

        DuckDB's composite PRIMARY KEY on `(key, owner_user_id, agent_id)`
        (see `src/db.py`'s `idempotency_keys` DDL) makes a plain re-INSERT
        of the same triple raise a constraint violation — DELETE-then-INSERT
        under one call keeps `put()` idempotent itself, matching the ON
        CONFLICT DO UPDATE the Postgres sibling uses.
        """
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=ttl_s)
        self.conn.execute(
            "DELETE FROM idempotency_keys WHERE key = ? AND owner_user_id = ? AND agent_id = ?",
            [key, owner_user_id, agent_id],
        )
        self.conn.execute(
            """INSERT INTO idempotency_keys
            (key, owner_user_id, agent_id, request_hash, response_body, status_code, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [key, owner_user_id, agent_id, request_hash, response_body, status_code, now, expires_at],
        )

    def reserve(
        self,
        key: str,
        owner_user_id: str,
        agent_id: str,
        request_hash: str,
    ) -> bool:
        """Insert an in-flight reservation row (``response_body``/
        ``status_code`` left ``NULL``) for `(key, owner_user_id, agent_id)`.

        Returns ``True`` if this call won the race — a fresh insert, or a
        stale prior row (an unfulfilled reservation past ``RESERVATION_TTL_S``,
        or an expired replay row) was overwritten — ``False`` if a LIVE
        conflicting row is already there (the caller turns that into
        ``409 idempotency_key_in_flight``/``idempotency_key_reuse`` depending
        on whether ``request_hash`` matches the conflicting row's).

        One atomic ``INSERT ... ON CONFLICT ... WHERE <stale> ... RETURNING``
        statement — no separate SELECT-then-decide — so this is race-free
        under genuine concurrent execution, not just safe by convention. The
        ``WHERE`` clause is the exact same staleness test ``get()`` applies
        (``expires_at`` in the past): a live reservation or a live replay row
        both fail it and the conflict is left untouched (0 rows RETURNING);
        an absent or stale row lets the insert/overwrite through (1 row
        RETURNING).
        """
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=RESERVATION_TTL_S)
        rows = self.conn.execute(
            """INSERT INTO idempotency_keys
            (key, owner_user_id, agent_id, request_hash, response_body, status_code, created_at, expires_at)
            VALUES (?, ?, ?, ?, NULL, NULL, ?, ?)
            ON CONFLICT (key, owner_user_id, agent_id) DO UPDATE SET
                request_hash = EXCLUDED.request_hash,
                response_body = NULL,
                status_code = NULL,
                created_at = EXCLUDED.created_at,
                expires_at = EXCLUDED.expires_at
            WHERE idempotency_keys.expires_at IS NOT NULL AND idempotency_keys.expires_at < ?
            RETURNING key""",
            [key, owner_user_id, agent_id, request_hash, now, expires_at, now],
        ).fetchall()
        return bool(rows)

    def fulfill(
        self,
        key: str,
        owner_user_id: str,
        agent_id: str,
        request_hash: str,
        response_body: str,
        status_code: int,
        ttl_s: int,
    ) -> None:
        """Finalize a `reserve()`d row with the real response, extending its
        expiry from the short reservation window to the caller's full
        replay-cache ``ttl_s``.

        Implemented as `put()` (identical DELETE-then-INSERT semantics) — a
        fulfilled row is byte-identical to what `put()` alone would have
        written. This exists as a distinct name purely so `reserve()` ->
        `fulfill()` call sites read as the lifecycle they are; `put()` stays
        the direct insert-or-replace primitive for any caller that doesn't
        need the reservation step.
        """
        self.put(key, owner_user_id, agent_id, request_hash, response_body, status_code, ttl_s)

    def purge_expired(self) -> int:
        """Delete every row whose `expires_at` is in the past. Returns the
        number of rows removed — callable from a maintenance job or a test
        assertion."""
        rows = self.conn.execute(
            "DELETE FROM idempotency_keys WHERE expires_at IS NOT NULL AND expires_at < ? RETURNING key",
            [datetime.now(timezone.utc)],
        ).fetchall()
        return len(rows)
