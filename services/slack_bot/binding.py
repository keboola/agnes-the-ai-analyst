"""Slack user ↔ Agnes user binding via 6-digit verification code.

Code is generated when a Slack user DMs the bot for the first time;
they paste it at /setup while logged in to bind the IDs.

SR-12 hardening:
- One active code per slack_user_id (DELETE prior on re-issue).
- Issuance throttle: at most _MAX_ISSUE_PER_WINDOW issues per 10 minutes.
- Per-caller redeem throttle: at most _MAX_REDEEM_ATTEMPTS failed redeem
  attempts per redeeming user_email per 10-minute window; a 6th failed
  attempt raises BindingThrottled. This bounds brute-forcing the 1M PIN
  space against a victim's live code without the cross-user DoS a global
  per-code counter would cause (a wrong guess matches no code row, so a
  per-code counter is dead code — see the redeem-log pattern below).
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
_MAX_ISSUE_PER_WINDOW = 3      # max codes issued in a 10-minute sliding window
_MAX_REDEEM_ATTEMPTS = 5      # max FAILED redeem attempts per caller per window
_REDEEM_WINDOW_SECONDS = 10 * 60  # sliding window for the redeem throttle


class BindingThrottled(Exception):
    """Raised by issue_verification_code (issuance rate limit) or
    redeem_verification_code (per-caller redeem rate limit) when the caller
    has exceeded the allowed attempts in the sliding window."""


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
    # Redeem-log for the per-caller redeem throttle: one row per FAILED
    # redeem attempt, keyed on the redeeming user_email. Mirrors the
    # issue-log pattern. Lazily created here (no migration needed) — same
    # convention as slack_binding_codes / slack_binding_issue_log, DuckDB-only
    # by design (these binding tables are not part of the dual-backend repo
    # layer; they're connection-local lazy tables).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS slack_binding_redeem_log ("
        " user_email VARCHAR NOT NULL,"
        " attempted_at TIMESTAMP NOT NULL"
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

    SR-12 per-caller redeem throttle: before checking the code, count this
    caller's FAILED redeem attempts in the last _REDEEM_WINDOW_SECONDS. If
    they have already reached _MAX_REDEEM_ATTEMPTS, raise BindingThrottled
    WITHOUT inspecting the code (so a locked-out caller can't even probe
    whether a guessed code exists). Each failed match (wrong/expired code)
    records an attempt row; a successful bind clears the caller's attempts.

    Why per-caller and not per-code: a wrong guess matches no row in
    slack_binding_codes, so a per-code attempt counter never increments and
    is dead code. A global per-code increment would let any caller evict
    every user's outstanding code (cross-user DoS). Keying the throttle on
    the redeeming user_email bounds brute-forcing the 1M PIN space against a
    victim's live code while isolating callers from each other.

    Returns True on a successful bind, False on a wrong/expired code.
    Raises BindingThrottled when the caller is rate-limited.

    Audit: every successful bind writes to audit_log (best-effort; failure
    is swallowed so a missing audit table never blocks the bind).
    """
    _ensure_table(conn)

    # Per-caller redeem throttle — count FAILED attempts in the sliding window.
    recent_failures = conn.execute(
        "SELECT count(*) FROM slack_binding_redeem_log WHERE user_email = ? "
        "AND attempted_at > current_timestamp - INTERVAL '10 minutes'",
        [user_email],
    ).fetchone()[0]
    if recent_failures >= _MAX_REDEEM_ATTEMPTS:
        # Locked out — do NOT inspect the code (no probing of code existence).
        raise BindingThrottled(user_email)

    def _record_failure() -> None:
        conn.execute(
            "INSERT INTO slack_binding_redeem_log(user_email, attempted_at) "
            "VALUES (?, current_timestamp)",
            [user_email],
        )

    row = conn.execute(
        "SELECT slack_user_id, issued_at FROM slack_binding_codes WHERE code = ?",
        [code],
    ).fetchone()
    if not row:
        # Wrong code — record a failed attempt against this caller's window.
        _record_failure()
        return False
    slack_user_id, issued_at = row
    # DuckDB returns naive datetimes in local time (current_timestamp semantics)
    now = datetime.now()
    if (now - issued_at).total_seconds() > _CODE_TTL_SECONDS:
        conn.execute("DELETE FROM slack_binding_codes WHERE code = ?", [code])
        _record_failure()
        return False
    # Bind: write slack_user_id to the user row.
    conn.execute("UPDATE users SET slack_user_id = ? WHERE email = ?", [slack_user_id, user_email])
    conn.execute("DELETE FROM slack_binding_codes WHERE code = ?", [code])
    # Success — clear this caller's failed-attempt history so a future
    # legitimate re-bind isn't penalised by earlier typos.
    conn.execute("DELETE FROM slack_binding_redeem_log WHERE user_email = ?", [user_email])
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
