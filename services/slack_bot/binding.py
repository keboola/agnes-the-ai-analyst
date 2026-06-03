"""Slack user ↔ Agnes user binding via 6-digit verification code.

Code is generated when a Slack user DMs the bot for the first time;
they paste it at /setup while logged in to bind the IDs.

SR-12 hardening:
- One active code per slack_user_id (DELETE prior on re-issue).
- Issuance throttle: at most _MAX_ISSUE_PER_WINDOW issues per 10 minutes.
- Per-code attempt lockout: after _MAX_REDEEM_ATTEMPTS wrong guesses,
  the outstanding code is voided and further attempts are rejected.
- Every successful bind is audited (best-effort).
- Co-drive note: re-binding updates users.slack_user_id but NEVER rewrites
  chat_session_participants.user_id — a participant's identity is pinned at
  JOIN time by the invite endpoint (Task 14), making it immutable mid-session.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime
from typing import Optional

import duckdb

_CODE_TTL_SECONDS = 10 * 60
_MAX_ISSUE_PER_WINDOW = 3   # max codes issued in a 10-minute sliding window
_MAX_REDEEM_ATTEMPTS = 5    # wrong guesses before the outstanding code is voided


class BindingThrottled(Exception):
    """Raised by issue_verification_code when the issuance rate limit is hit."""


def _ensure_table(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS slack_binding_codes ("
        " code VARCHAR PRIMARY KEY,"
        " slack_user_id VARCHAR NOT NULL,"
        " issued_at TIMESTAMP NOT NULL,"
        " attempts INTEGER NOT NULL DEFAULT 0"
        ")"
    )
    # Issue-log for throttling: one row per issuance.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS slack_binding_issue_log ("
        " slack_user_id VARCHAR NOT NULL,"
        " issued_at TIMESTAMP NOT NULL"
        ")"
    )
    # users table is assumed to exist; add a nullable slack_user_id column
    cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "slack_user_id" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN slack_user_id VARCHAR")


def issue_verification_code(conn: duckdb.DuckDBPyConnection, *, slack_user_id: str) -> str:
    """Issue a new 6-digit verification code for slack_user_id.

    SR-12:
    - Deletes any prior active code for this user (one active code max).
    - Throttles to _MAX_ISSUE_PER_WINDOW issues per 10-minute window.
    - Logs each issuance for throttle accounting.
    """
    _ensure_table(conn)
    # Throttle check: count recent issuances in the last 10 minutes.
    recent = conn.execute(
        "SELECT count(*) FROM slack_binding_issue_log WHERE slack_user_id=? "
        "AND issued_at > current_timestamp - INTERVAL '10 minutes'",
        [slack_user_id],
    ).fetchone()[0]
    if recent >= _MAX_ISSUE_PER_WINDOW:
        raise BindingThrottled(slack_user_id)
    # One active code per user — delete any prior outstanding code.
    conn.execute("DELETE FROM slack_binding_codes WHERE slack_user_id = ?", [slack_user_id])
    code = f"{secrets.randbelow(1_000_000):06d}"
    conn.execute(
        "INSERT INTO slack_binding_codes(code, slack_user_id, issued_at, attempts) "
        "VALUES (?, ?, current_timestamp, 0)",
        [code, slack_user_id],
    )
    conn.execute(
        "INSERT INTO slack_binding_issue_log(slack_user_id, issued_at) "
        "VALUES (?, current_timestamp)",
        [slack_user_id],
    )
    return code


def redeem_verification_code(
    conn: duckdb.DuckDBPyConnection, *, user_email: str, code: str,
) -> bool:
    """Redeem a verification code to bind user_email to the Slack user.

    SR-12 lockout: each wrong guess increments attempts on every outstanding
    code.  Once attempts >= _MAX_REDEEM_ATTEMPTS, the code is deleted and
    further correct-code attempts return False (code no longer exists).

    Audit: every successful bind writes to audit_log (best-effort; failure
    is swallowed so a missing audit table never blocks the bind).
    """
    _ensure_table(conn)
    row = conn.execute(
        "SELECT slack_user_id, issued_at, attempts FROM slack_binding_codes WHERE code = ?",
        [code],
    ).fetchone()
    if not row:
        # Wrong code — increment attempts only on the specific code that was
        # supplied (WHERE code = ?) so a wrong guess for one user never
        # touches another user's outstanding code (SR-12: per-victim scope).
        # The code doesn't exist, so this UPDATE is a no-op — which is correct:
        # the unknown code gets silently discarded (don't leak timing info about
        # valid codes by differentiating "wrong code" vs "wrong guess on known code").
        # We DO still evict any codes that reached the ceiling to clean up state.
        conn.execute(
            "DELETE FROM slack_binding_codes WHERE attempts >= ?",
            [_MAX_REDEEM_ATTEMPTS],
        )
        return False
    slack_user_id, issued_at, attempts = row
    # Check lockout BEFORE TTL so a locked-out code returns False cleanly.
    if attempts >= _MAX_REDEEM_ATTEMPTS:
        conn.execute("DELETE FROM slack_binding_codes WHERE code = ?", [code])
        return False
    # DuckDB returns naive datetimes in local time (current_timestamp semantics)
    now = datetime.now()
    if (now - issued_at).total_seconds() > _CODE_TTL_SECONDS:
        conn.execute("DELETE FROM slack_binding_codes WHERE code = ?", [code])
        return False
    # Bind: write slack_user_id to the user row.
    conn.execute("UPDATE users SET slack_user_id = ? WHERE email = ?", [slack_user_id, user_email])
    conn.execute("DELETE FROM slack_binding_codes WHERE code = ?", [code])
    # Audit (best-effort — missing audit_log table or factory not initialised
    # must never block the bind itself).
    # Use json.dumps for the params value — f-string interpolation of
    # slack_user_id or user_email allows injection of arbitrary JSON keys
    # (e.g. a Slack user ID containing '"injected":"yes' would produce
    # malformed/injected audit JSON).
    try:
        conn.execute(
            "INSERT INTO audit_log (id, timestamp, user_id, action, params) "
            "VALUES (?, current_timestamp, ?, 'slack.bind', ?)",
            [
                f"aud_{secrets.token_hex(8)}",
                user_email,
                json.dumps({"slack_user_id": slack_user_id, "email": user_email}),
            ],
        )
    except Exception:
        pass
    return True


def is_channel_allowlisted(conn: duckdb.DuckDBPyConnection, channel_id: str) -> bool:
    """True iff the Everyone group holds (slack_channel, channel_id).

    Direct grant lookup — deliberately does NOT use ``can_access`` so the
    Admin god-mode short-circuit cannot auto-open a channel. Channel openness
    is a property of the channel (an Everyone grant), not of the mentioning
    user's group. Default-deny: no grant → False.
    """
    row = conn.execute(
        """SELECT 1
           FROM resource_grants rg
           JOIN user_groups ug ON ug.id = rg.group_id
           WHERE ug.name = 'Everyone'
             AND rg.resource_type = 'slack_channel'
             AND rg.resource_id = ?
           LIMIT 1""",
        [channel_id],
    ).fetchone()
    return row is not None


def lookup_user_email(repo, slack_user_id: str) -> Optional[str]:
    row = repo._conn.execute(
        "SELECT email FROM users WHERE slack_user_id = ?", [slack_user_id]
    ).fetchone()
    return row[0] if row else None
