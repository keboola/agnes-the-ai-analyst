"""Pure helpers for the Keboola project identity carried by Storage API payloads.

Shared by the admin source-connection endpoints and the Keboola auth provider,
so the int-vs-str coercion and the None-hole rejection live in exactly one
place (both bit real deployments before: Devin Review on #1242).
"""

from typing import Any, Dict, Optional


def project_identity(payload: Optional[Dict[str, Any]]) -> tuple[Optional[Any], str]:
    """``(project_id, project_name)`` from a Storage API payload that carries
    an ``owner`` block — both ``GET /tokens/verify`` and ``GET /v2/storage``
    do, so one reader serves the token preflights and the /test probe.

    Returns ``(None, "")`` when the payload has no owner id: an identity we
    cannot read must never be persisted as a *known* identity, or a
    cross-token check would compare against a hole and pass anything.
    """
    owner = (payload or {}).get("owner") or {}
    owner_id = owner.get("id")
    if owner_id is None:
        return None, ""
    return owner_id, owner.get("name") or ""


def project_matches(expected: Any, payload: Optional[Dict[str, Any]]) -> bool:
    """True iff the payload's owner id equals ``expected``.

    Compared as strings — the id round-trips through YAML/env config and JSON
    columns on two backends, so 5947 vs "5947" must not read as a mismatch.
    ``None`` on either side is an explicit reject, never a match.
    """
    if expected is None:
        return False
    project_id, _ = project_identity(payload)
    if project_id is None:
        return False
    return str(project_id) == str(expected)
