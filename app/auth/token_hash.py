"""Hashing for at-rest bearer tokens (password-reset, account-setup,
email magic-link).

These tokens are single-use, high-entropy secrets handed to a user in a URL.
Storing them verbatim means anyone who can read ``system.duckdb`` (a backup,
a snapshot, an objectViewer on the state bucket) can replay a live token for
account takeover. We store only the SHA-256 digest and hash the incoming
token before comparing/looking it up — the same hash-at-rest standard the
PAT (``access_tokens``) and setup-token / chat-broker-ticket repositories
already use. The raw token exists only in the emailed link and the user's
browser, never in the database (audit M3).
"""

from __future__ import annotations

import hashlib


def hash_token(raw: str) -> str:
    """Return the SHA-256 hex digest stored/compared for an at-rest token."""
    return hashlib.sha256(raw.encode()).hexdigest()
