"""Proactive PAT re-mint nudge (#477) — Option 3, no new server primitives.

`agnes auth login` (PR #475) stores a 90-day personal access token minted by
the browser loopback flow. Agnes PATs are HS256 JWTs — the `exp` claim is
plain base64 JSON and client-decodable WITHOUT the signing secret (which
lives server-side only and is never available to the CLI). This module uses
that fact to remind the analyst to re-run `agnes auth login` before the
token actually expires, instead of building a refresh-token grant or
changing the default PAT TTL.

Surfaces:
  - `maybe_print_nudge()` — a one-line stderr warning, wired into the
    `cli/main.py` root callback so it fires on (almost) every non-quiet
    command. At most once per UTC calendar day via a marker file next to
    `token.json` (same pattern as `cli/upgrade_status.py`).
  - `format_status_line()` — human-readable status used by
    `agnes auth whoami` and the `agnes update` convergence report.

Never raises: an expiry-nudge failure must not break a working `agnes`
command.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Optional

from cli.config import _config_dir, get_token

_MARKER_FILENAME = "token_nudge_state.json"
_DEFAULT_RENEW_DAYS = 7


def get_renew_days() -> int:
    """Read `AGNES_TOKEN_RENEW_DAYS` (default 7 days). `0` disables the nudge.

    An unparseable value falls back to the default rather than silently
    disabling the safety net — a typo'd env var shouldn't turn this off.
    Negative values clamp to 0 (disabled).
    """
    raw = os.environ.get("AGNES_TOKEN_RENEW_DAYS")
    if raw is None or raw.strip() == "":
        return _DEFAULT_RENEW_DAYS
    try:
        n = int(raw.strip())
    except ValueError:
        return _DEFAULT_RENEW_DAYS
    return max(0, n)


def decode_expiry(token: str) -> Optional[datetime]:
    """Return the token's `exp` claim as a tz-aware UTC datetime, or None.

    Decoded WITHOUT signature verification — the HS256 secret is
    server-side only; the `exp` claim is readable by design (same
    decode-without-verify pattern already used by `agnes auth whoami` /
    `agnes auth import-token`). Returns None for a garbage token, a
    missing `exp` claim, or any decode failure — callers treat "unknown"
    as "don't nudge", never as "assume it's fine" or "assume it's expired".
    """
    try:
        import jwt

        payload = jwt.decode(token, options={"verify_signature": False})
    except Exception:
        return None
    exp = payload.get("exp")
    if exp is None:
        return None
    try:
        return datetime.fromtimestamp(float(exp), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def days_remaining(token: str, *, now: Optional[datetime] = None) -> Optional[float]:
    """Days until `token` expires (negative if already expired), or None if
    the expiry can't be determined."""
    exp = decode_expiry(token)
    if exp is None:
        return None
    reference = now if now is not None else datetime.now(timezone.utc)
    return (exp - reference).total_seconds() / 86400.0


def format_nudge(days_left: float) -> str:
    """One-line stderr warning. `days_left` may be negative (already expired)."""
    if days_left <= 0:
        return "agnes: token has expired — run `agnes auth login` to renew."
    n = max(1, round(days_left))
    plural = "day" if n == 1 else "days"
    return f"agnes: token expires in {n} {plural} — run `agnes auth login` to renew."


def format_status_line(token: str, *, now: Optional[datetime] = None) -> str:
    """Human-readable one-line token status for `agnes auth whoami` /
    `agnes update`'s convergence report.

    Examples: "valid until 2026-09-01 (12 days)", "expired 3 days ago
    (2026-01-01)", "expiry unknown (no exp claim)".
    """
    exp = decode_expiry(token)
    if exp is None:
        return "expiry unknown (no exp claim)"
    date_str = exp.strftime("%Y-%m-%d")
    left = days_remaining(token, now=now)
    if left is None:
        return f"valid until {date_str}"
    if left <= 0:
        n = max(1, round(-left))
        plural = "day" if n == 1 else "days"
        return f"expired {n} {plural} ago ({date_str})"
    n = max(1, round(left))
    plural = "day" if n == 1 else "days"
    return f"valid until {date_str} ({n} {plural})"


def _marker_path():
    return _config_dir() / _MARKER_FILENAME


def _today_utc(now: Optional[datetime] = None) -> str:
    reference = now if now is not None else datetime.now(timezone.utc)
    return reference.strftime("%Y-%m-%d")


def _last_nudged_date() -> Optional[str]:
    p = _marker_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    val = data.get("last_nudge_date")
    return val if isinstance(val, str) else None


def _mark_nudged(date_str: str) -> None:
    p = _marker_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"last_nudge_date": date_str}), encoding="utf-8")
    except OSError:
        pass  # best-effort — a marker-write failure must not break the flow


def maybe_print_nudge(*, now: Optional[datetime] = None) -> bool:
    """Print the renewal nudge to stderr if due; return True iff it fired.

    Silent (returns False, never raises) when: the nudge is disabled
    (`AGNES_TOKEN_RENEW_DAYS=0`), there's no stored token, the expiry can't
    be decoded, the token isn't inside the renewal window yet, or we
    already nudged today (UTC calendar day, via the marker file).
    """
    try:
        renew_days = get_renew_days()
        if renew_days <= 0:
            return False
        token = get_token()
        if not token:
            return False
        left = days_remaining(token, now=now)
        if left is None or left > renew_days:
            return False
        today = _today_utc(now)
        if _last_nudged_date() == today:
            return False
        import typer

        typer.echo(format_nudge(left), err=True)
        _mark_nudged(today)
        return True
    except Exception:
        return False
