"""Slack signing-secret HMAC verification (per Slack Events API spec)."""
from __future__ import annotations

import hashlib
import hmac
import time

MAX_SKEW_SECONDS = 60 * 5


def verify_slack_signature(
    signing_secret: str, timestamp: str, signature: str, body: bytes,
) -> bool:
    try:
        ts_int = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(time.time() - ts_int) > MAX_SKEW_SECONDS:
        return False
    base = f"v0:{timestamp}:".encode() + body
    expected = "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
