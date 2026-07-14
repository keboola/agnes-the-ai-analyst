"""Policy Engine for Universal MCP passthrough calls (RFC #461 §3).

Three independent gates, each driven by a column on ``tool_registry``:

* ``mutating`` (BOOLEAN) — when true, only admins can invoke the tool.
  POC scope is read-only-by-default for analyst users; admin gets the
  full surface for testing + curation. A future iteration can replace
  the admin-or-bust check with a separate ``mutating_grant`` row.

* ``pii_fields`` (JSON list[str]) — recursive-redact every value whose
  *key* matches an entry in the list. Applied to both ``text`` (when
  the upstream returned JSON content) and ``data`` (the parsed dict).
  Replacement token is the string ``"[REDACTED]"`` — picked so it
  survives JSON round-trip and is grep-able in audit logs.

* ``rate_limit_pm`` (INT, per-minute, per-user, per-tool) — in-memory
  token bucket keyed on ``(tool_id, user_id)``. Cleared on app restart
  (the cowork pattern: rate-limit per session, not per ever). When the
  count of timestamps within the last 60s reaches the cap, ``check``
  returns the seconds-until-next-slot for the caller's 429 Retry-After.

Each helper is independent and pure-ish (rate-limit uses a module-level
dict guarded by a lock). The invoke endpoint wires them in order:
mutating → rate-limit → forward → redact response.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Mutating gate
# ---------------------------------------------------------------------------


class MutatingNotAllowed(Exception):
    """Raised by ``check_mutating`` when a non-admin invokes a mutating tool."""


def check_mutating(tool: Dict[str, Any], *, is_admin: bool) -> None:
    """Raise ``MutatingNotAllowed`` for a non-admin call on a mutating tool.

    No-op for admin callers (curation + testing flow) and for tools whose
    registry row has ``mutating=False`` (the read-only default).
    """
    if not bool(tool.get("mutating", False)):
        return
    if is_admin:
        return
    raise MutatingNotAllowed(f"tool {tool.get('tool_id')!r} is marked mutating; non-admin invocations are blocked")


# ---------------------------------------------------------------------------
# Grant gate + combined authorization
# ---------------------------------------------------------------------------


class GrantDenied(Exception):
    """Raised when the caller's groups have no ``tool_grants`` row for a tool."""


def enforce_passthrough_access(tool: Dict[str, Any], caller_user_id: Optional[str]) -> None:
    """Full authorization gate for a single passthrough invocation.

    The one gate stack shared by the REST endpoint
    (``app/api/mcp_passthrough.invoke_passthrough_tool``) and the SSE /
    Streamable-HTTP transport closures (``app/api/mcp/tools_generator``), so the
    interactive-forward paths can't drift apart. Runs, in order:

    1. **grant** — admin short-circuits; otherwise the caller must be in a group
       listed in ``tool_grants`` for this tool (``GrantDenied`` on miss);
    2. **mutating** — a ``mutating`` tool is admin-only (``MutatingNotAllowed``);
    3. **rate limit** — per-(tool, user) token bucket (``RateLimited``).

    ``caller_user_id`` is ``None`` when the transport could not resolve an
    identity from the request (absent / invalid token). That is treated as a
    non-admin caller with no groups, so the grant check **fails closed** — an
    unauthenticated forward is never allowed.

    Callers map the typed exceptions onto their transport's error surface (REST:
    403/429 HTTP; MCP transports: a tool error). Backend-aware: resolves RBAC
    through the repo factory (``tool_registry_repo``) + ``app.auth.access`` so it
    reads the active state backend (DuckDB or Postgres).
    """
    from app.auth.access import _user_group_ids, is_user_admin
    from src.repositories import tool_registry_repo

    tool_id = tool.get("tool_id")
    is_admin = bool(caller_user_id) and is_user_admin(caller_user_id)
    if not is_admin:
        group_ids = list(_user_group_ids(caller_user_id)) if caller_user_id else []
        if not tool_registry_repo().is_granted_to_groups(tool_id, group_ids):
            raise GrantDenied(f"no grant on tool {tool_id!r} for your groups")
    check_mutating(tool, is_admin=is_admin)
    # An unresolved caller never reaches here (fails closed on grant above), so
    # the rate-bucket key always carries a real user id for identified callers.
    check_rate_limit(tool_id, caller_user_id or "", tool.get("rate_limit_pm"))


class PerUserCredentialMissing(Exception):
    """Raised when a ``scope='per_user'`` source is invoked by an identified
    caller who has not stored their own credential.

    ``source_label`` is the human-facing source name (or id) for the remedy
    message.
    """

    def __init__(self, source_label: str):
        self.source_label = source_label
        super().__init__(
            f"no personal credential for source {source_label!r}. Run "
            f"`agnes mcp my-secret set {source_label}` to connect your own account."
        )


def enforce_per_user_credential(source: Dict[str, Any], caller_user_id: Optional[str]) -> None:
    """Fail closed when a ``per_user`` source lacks the caller's own credential.

    For a ``scope='per_user'`` source an identified caller (admin included —
    data scoping is per identity) must have their own stored credential;
    otherwise the forward would connect with no token (see
    ``connectors.mcp.client._lookup_secret_for_source``, which returns ``None``
    for a per_user source with an identified caller and no row — it does NOT
    borrow the shared credential). Refuse here with an actionable message
    instead of letting it degrade to an opaque upstream auth error.

    Shared by the REST endpoint and the SSE / Streamable-HTTP transport
    closures so the pre-forward guard can't drift. No-op for shared sources and
    for the caller-less (materialize) path, which legitimately rides the shared
    vault. Raises ``PerUserCredentialMissing``.
    """
    if (source.get("scope") or "shared").lower() != "per_user":
        return
    if not caller_user_id:
        # Caller-less materialize path — shared vault is the intended source.
        return
    from src.repositories import per_user_secrets_repo

    if not per_user_secrets_repo().get(source["id"], caller_user_id):
        raise PerUserCredentialMissing(source.get("name") or source["id"])


# ---------------------------------------------------------------------------
# PII redaction
# ---------------------------------------------------------------------------

REDACTED_TOKEN = "[REDACTED]"


def redact_pii(value: Any, pii_keys: Iterable[str]) -> Any:
    """Return ``value`` with every entry whose *key* is in ``pii_keys`` masked.

    Recurses through nested dicts and lists. Non-container values pass
    through unchanged — redaction is keyed off the parent's key, not the
    value's content. The match is case-sensitive and exact, mirroring
    how analysts spell column names when they fill ``pii_fields`` on the
    registry row.

    A shallow copy is returned for containers so the caller can keep a
    pristine copy of the upstream payload (useful when the result also
    feeds the audit log on a future iteration).
    """
    keys = set(pii_keys or [])
    if not keys:
        return value
    return _redact_recursive(value, keys)


def _redact_recursive(value: Any, keys: set) -> Any:
    if isinstance(value, dict):
        return {k: (REDACTED_TOKEN if k in keys else _redact_recursive(v, keys)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_recursive(v, keys) for v in value]
    return value


def redact_response(
    *,
    text: str,
    data: Optional[Any],
    pii_fields: Optional[List[str]],
) -> Tuple[str, Optional[Any]]:
    """Apply PII redaction to both ``data`` and ``text`` consistently.

    When the upstream returned valid JSON, ``text`` is the serialized form
    of ``data`` — so we redact ``data`` and re-serialize for ``text``,
    keeping the two in sync. When ``data`` is None (non-JSON text), we
    leave ``text`` unchanged since key-based redaction has no meaning on
    a flat string. A future iteration can layer regex-based redaction
    for free-form text.
    """
    if not pii_fields:
        return text, data
    if data is None:
        return text, data
    redacted = redact_pii(data, pii_fields)
    return json.dumps(redacted), redacted


# ---------------------------------------------------------------------------
# Per-(tool, user) rate limit — in-memory token bucket
# ---------------------------------------------------------------------------

_RATE_BUCKETS: Dict[Tuple[str, str], deque] = {}
_RATE_LOCK = threading.Lock()
_WINDOW_SECONDS = 60.0


class RateLimited(Exception):
    """Raised by ``check_rate_limit`` when the caller has hit the per-minute cap.

    ``retry_after_seconds`` is set on the instance so the HTTP layer can
    surface it in a Retry-After header — RFC 6585 §4 (Status 429).
    """

    def __init__(self, retry_after_seconds: float):
        super().__init__(f"rate limit exceeded; retry after {retry_after_seconds:.1f}s")
        self.retry_after_seconds = retry_after_seconds


def check_rate_limit(
    tool_id: str,
    user_id: str,
    cap_per_minute: Optional[int],
    *,
    now: Optional[float] = None,
) -> None:
    """Raise ``RateLimited`` if the caller has used ``cap_per_minute`` slots
    in the past 60 seconds. Records this call as a fresh slot on success.

    ``cap_per_minute`` of ``None`` or ``<=0`` disables the gate. The bucket
    is keyed on ``(tool_id, user_id)`` so two different tools share no
    quota, and two different callers don't fight for the same slot pool.
    """
    if not cap_per_minute or cap_per_minute <= 0:
        return
    now = now if now is not None else time.monotonic()
    key = (tool_id, user_id)
    cutoff = now - _WINDOW_SECONDS
    with _RATE_LOCK:
        bucket = _RATE_BUCKETS.setdefault(key, deque())
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= cap_per_minute:
            # The next free slot opens when the oldest timestamp ages out.
            retry_after = (bucket[0] + _WINDOW_SECONDS) - now
            raise RateLimited(max(retry_after, 0.0))
        bucket.append(now)


def reset_rate_buckets_for_tests() -> None:
    """Test-only: clear the module-level state between tests."""
    with _RATE_LOCK:
        _RATE_BUCKETS.clear()
