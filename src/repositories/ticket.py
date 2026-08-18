"""DuckDB-backed repository for ``chat_broker_tickets`` (v90).

Opaque, short-lived tickets minted for the chat sandbox secret broker
(2026-07-14 incident hardening): a sandbox-local relay holds a ticket in
memory only and presents it to the broker routes instead of a real
credential. ``mint`` returns an opaque ``secrets.token_urlsafe(32)`` value;
``resolve`` rejects unknown or expired tokens; ``revoke``/``revoke_session``
invalidate tickets early (e.g. on session resume, once fresh tickets have
been pushed).

Template: ``src/repositories/setup_tokens.py``.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Sequence

import duckdb


def _hash(token: str) -> str:
    """sha256 of the raw ticket — only the digest is ever persisted."""
    return hashlib.sha256(token.encode()).hexdigest()


#: Scopes a session-wide sweep must NOT delete: long-lived credentials, as
#: opposed to the short-lived egress tickets a sandbox restart is clearing.
#:
#: Most callers of :meth:`revoke_session` are sandbox-lifecycle sweeps — the
#: chat runner spawning, stopping, respawning or resuming a relay, and Slack's
#: session reset — each meaning "retire the tickets that relay was holding".
#: An earlier version of this note claimed that was true of EVERY caller; it is
#: not. ``ChatManager.kill`` runs the same sweep, and a user's permanent delete
#: reaches it (``DELETE /api/chat/sessions/{chat_id}/permanent`` ->
#: ``_kill_quietly`` -> ``kill``). An exemption here therefore cannot be the
#: only thing bounding an exempted credential's life: ``app/api/kai.py``
#: additionally refuses a credential whose session row is gone, so a deleted
#: conversation cuts the engine off immediately rather than at TTL.
#: A ticket in one of these scopes is not that: it is the caller's own proof of
#: identity, minted once per session, and its holder has no channel to be
#: handed a replacement. `kai_session` is the embedded turn engine's
#: credential (``app/api/kai.py``), and the engine's chat row is an ordinary
#: `chat_sessions` row — so a user opening that conversation in web chat used
#: to spawn a native runner whose sweep silently killed the engine's session
#: for good, every later turn rejected.
#:
#: This is an exemption from the SWEEP, not from revocation: `revoke`
#: (single token) and `revoke_session_scopes` (named scopes) still delete these
#: rows when asked explicitly, and every such ticket carries a TTL, so nothing
#: outlives its expiry.
SWEEP_EXEMPT_SCOPES: frozenset[str] = frozenset({"kai_session"})

class TicketRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    def mint(self, session_id: str, scope: str, ttl_seconds: int = 3600) -> str:
        """Insert a new ticket and return the RAW opaque token. Only the
        sha256 digest is stored (the ``token`` PK column holds the digest, not
        the bearer value), mirroring PAT/setup-token hygiene: a read of
        ``system.duckdb`` never yields a usable ticket."""
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=ttl_seconds)
        self.conn.execute(
            """INSERT INTO chat_broker_tickets
               (token, session_id, scope, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            [_hash(token), session_id, scope, expires_at, now],
        )
        return token

    def resolve(self, token: str) -> Optional[Dict[str, Any]]:
        """Return ``{"session_id", "scope", "expires_at"}`` if ``token`` exists
        and is not expired, else ``None``."""
        now = datetime.now(timezone.utc)
        row = self.conn.execute(
            """SELECT session_id, scope, expires_at FROM chat_broker_tickets
               WHERE token = ? AND expires_at > ?""",
            [_hash(token), now],
        ).fetchone()
        if not row:
            return None
        return {"session_id": row[0], "scope": row[1], "expires_at": row[2]}

    def revoke(self, token: str) -> None:
        self.conn.execute("DELETE FROM chat_broker_tickets WHERE token = ?", [_hash(token)])

    def revoke_session(self, session_id: str) -> None:
        """Sweep the session's egress tickets. See :data:`SWEEP_EXEMPT_SCOPES`
        for the long-lived credentials this deliberately leaves alone."""
        placeholders = ", ".join("?" for _ in SWEEP_EXEMPT_SCOPES)
        self.conn.execute(
            f"DELETE FROM chat_broker_tickets WHERE session_id = ? AND scope NOT IN ({placeholders})",  # noqa: S608
            [session_id, *sorted(SWEEP_EXEMPT_SCOPES)],
        )

    def revoke_session_scopes(self, session_id: str, scopes: Sequence[str]) -> None:
        """Revoke only the session's tickets in ``scopes``, leaving its other
        scopes alone.

        ``revoke_session`` is scope-blind, which is wrong for a caller that
        holds a long-lived session credential in one scope and rotates
        short-lived egress tickets in others (``app/api/kai.py``): sweeping
        the whole session would delete the very credential the caller
        authenticated with, and it has no way to be handed a new one.

        An empty ``scopes`` deletes **nothing** — fail safe. The alternative
        (an empty ``IN ()`` degrading to "match everything") is exactly the
        accident this method exists to prevent.
        """
        if not scopes:
            return
        placeholders = ", ".join("?" for _ in scopes)
        self.conn.execute(
            f"DELETE FROM chat_broker_tickets WHERE session_id = ? AND scope IN ({placeholders})",  # noqa: S608
            [session_id, *scopes],
        )
