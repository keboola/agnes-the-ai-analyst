"""Signed, short-lived ``state`` blobs for the outbound MCP OAuth connect
flow (2026-07-30 outbound MCP OAuth sources spec §3/§6).

``GET /api/mcp/sources/{id}/oauth/authorize`` mints a ``state`` value that
survives an opaque round-trip through a third-party authorization server —
it must be tamper-evident (so a forged state can't point the callback at an
arbitrary user/source pair) and self-expiring (so a stale link found in
browser history is dead). ``itsdangerous.URLSafeTimedSerializer`` gives both
in one primitive: an HMAC-signed, base64url payload with a signing timestamp
that ``loads(..., max_age=...)`` enforces.

This is deliberately NOT a JWT — the payload is opaque flow bookkeeping
(``source_id``, ``user_id``, ``nonce``), not an identity credential, and the
nonce itself is single-use via ``mcp_oauth_flows.consume()`` (DB-backed,
deleted on first read) — the signature here only proves the triple wasn't
tampered with in transit through the AS, not that it hasn't been replayed
(that's the nonce's job).
"""

from __future__ import annotations

from typing import Dict

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.auth.jwt import get_signing_secret

#: Matches the spec's "rows expire after 10 min" for the backing
#: ``mcp_oauth_flows`` row — the signed state must not outlive the flow it
#: references.
MAX_AGE_SECONDS = 600

_SALT = "mcp-oauth-connect-state"

_REQUIRED_KEYS = ("source_id", "user_id", "nonce")


class ConnectStateInvalid(Exception):
    """Raised by :func:`verify_connect_state` for any unusable ``state``:
    bad signature, expired, or a malformed/missing payload field."""


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_signing_secret(), salt=_SALT)


def sign_connect_state(source_id: str, user_id: str, nonce: str) -> str:
    """Sign ``{source_id, user_id, nonce}`` into an opaque ``state`` string."""
    return _serializer().dumps({"source_id": source_id, "user_id": user_id, "nonce": nonce})


def verify_connect_state(state: str) -> Dict[str, str]:
    """Verify signature + age (``MAX_AGE_SECONDS``) and shape.

    Raises :class:`ConnectStateInvalid` on any failure. Never raises the
    underlying ``itsdangerous`` exception type — callers (the callback
    handler) treat every failure identically (redirect with a generic
    error), so there's no reason to leak the distinction between "expired"
    and "tampered" past this module.
    """
    try:
        data = _serializer().loads(state, max_age=MAX_AGE_SECONDS)
    except SignatureExpired as exc:
        raise ConnectStateInvalid("state expired") from exc
    except BadSignature as exc:
        raise ConnectStateInvalid("state signature invalid") from exc
    if not isinstance(data, dict):
        raise ConnectStateInvalid("state payload malformed")
    for key in _REQUIRED_KEYS:
        value = data.get(key)
        if not isinstance(value, str) or not value:
            raise ConnectStateInvalid(f"state missing {key!r}")
    return {key: data[key] for key in _REQUIRED_KEYS}
