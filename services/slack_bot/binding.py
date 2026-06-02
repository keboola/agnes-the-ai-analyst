"""Slack user ↔ Agnes user binding via 6-digit verification code.

Code is generated when a Slack user DMs the bot for the first time;
they paste it at /setup while logged in to bind the IDs.
"""
from __future__ import annotations

import secrets
from datetime import datetime
from typing import Optional

import duckdb

_CODE_TTL_SECONDS = 10 * 60


def _ensure_table(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS slack_binding_codes ("
        " code VARCHAR PRIMARY KEY,"
        " slack_user_id VARCHAR NOT NULL,"
        " issued_at TIMESTAMP NOT NULL"
        ")"
    )
    # users table is assumed to exist; add a nullable slack_user_id column
    cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "slack_user_id" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN slack_user_id VARCHAR")


def issue_verification_code(conn: duckdb.DuckDBPyConnection, *, slack_user_id: str) -> str:
    _ensure_table(conn)
    code = f"{secrets.randbelow(1_000_000):06d}"
    conn.execute(
        "INSERT INTO slack_binding_codes(code, slack_user_id, issued_at) VALUES (?, ?, current_timestamp)",
        [code, slack_user_id],
    )
    return code


def redeem_verification_code(
    conn: duckdb.DuckDBPyConnection, *, user_email: str, code: str,
) -> bool:
    _ensure_table(conn)
    row = conn.execute(
        "SELECT slack_user_id, issued_at FROM slack_binding_codes WHERE code = ?",
        [code],
    ).fetchone()
    if not row:
        return False
    slack_user_id, issued_at = row
    # DuckDB returns naive datetimes in local time (current_timestamp semantics)
    now = datetime.now()
    age = (now - issued_at).total_seconds()
    if age > _CODE_TTL_SECONDS:
        conn.execute("DELETE FROM slack_binding_codes WHERE code = ?", [code])
        return False
    conn.execute("UPDATE users SET slack_user_id = ? WHERE email = ?", [slack_user_id, user_email])
    conn.execute("DELETE FROM slack_binding_codes WHERE code = ?", [code])
    return True


def lookup_user_email(repo, slack_user_id: str) -> Optional[str]:
    row = repo._conn.execute(
        "SELECT email FROM users WHERE slack_user_id = ?", [slack_user_id]
    ).fetchone()
    return row[0] if row else None
