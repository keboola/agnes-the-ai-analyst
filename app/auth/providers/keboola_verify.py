"""Keboola Storage API token verification for the auth provider.

One module owns every Storage-API identity decision (master-token gate,
project binding, role gate) so the OAuth callback and the
X-StorageApi-Token header path can never drift apart. HTTP goes through
``_fetch_verify`` exclusively — tests monkeypatch it; callers import THIS
MODULE (not names from it) for the same reason.

Facts this encodes (verified against the platform, 2026-08-12):
- ``adminOwner`` is back-filled through the token's creator chain, so its
  presence does NOT mean "admin token" — a restricted bucket token created
  by an admin carries the admin's identity. ``isMasterToken`` is the
  discriminator; gating on adminOwner would let any holder of a scoped
  service token authenticate as the human who created it.
- ``adminOwner`` on the verify response is real but publicly undocumented —
  handle absence defensively, never crash.
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx
from fastapi import HTTPException

from app.instance_config import get_value
from app.keboola_identity import project_identity, project_matches

logger = logging.getLogger(__name__)

VERIFY_TIMEOUT_SECONDS = 5.0


class KeboolaVerifyError(Exception):
    """A verify/gate failure. ``reason`` is machine-readable; ``detail`` is
    the operator/user-facing sentence. The token itself must never appear
    in either."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail or reason
        super().__init__(self.detail)


@dataclass(frozen=True)
class VerifiedKeboolaIdentity:
    token_id: str
    project_id: str
    project_name: str
    email: str
    name: str
    role: str


def stack_url() -> Optional[str]:
    url = get_value("auth", "keboola", "stack_url") or get_value("data_source", "keboola", "stack_url")
    return str(url).rstrip("/") if url else None


def oauth_host() -> Optional[str]:
    url = get_value("auth", "keboola", "oauth_host")
    return str(url).rstrip("/") if url else stack_url()


def configured_project_id() -> Optional[str]:
    value = get_value("auth", "keboola", "project_id")
    if value is None or str(value).strip() == "":
        return None
    return str(value).strip()


def allowed_roles() -> Optional[list[str]]:
    value = get_value("auth", "keboola", "allowed_roles")
    if not value:
        return None
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


def client_id() -> str:
    return str(get_value("auth", "keboola", "client_id") or "")


def client_secret() -> str:
    return str(get_value("auth", "keboola", "client_secret") or "")


def _fetch_verify(base_url: str, headers: Dict[str, str]) -> Dict[str, Any]:
    """GET {base_url}/v2/storage/tokens/verify. The ONLY HTTP call site.

    SSRF: the target is re-validated at every use (not store time) — same
    DNS-rebind / metadata-endpoint posture as the admin source-connection
    verify calls.
    """
    from app.api.admin import _validate_url_not_private

    try:
        _validate_url_not_private(base_url, "auth.keboola.stack_url")
    except HTTPException as exc:
        # The shared SSRF validator speaks HTTP (HTTPException); this module's
        # contract is KeboolaVerifyError-only. Translate the rejection without
        # weakening the gate itself.
        raise KeboolaVerifyError("verify_failed", str(exc.detail))
    try:
        resp = httpx.get(
            f"{base_url}/v2/storage/tokens/verify",
            headers=headers,
            timeout=VERIFY_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        logger.warning("Keboola verify call failed: %s", type(exc).__name__)
        raise KeboolaVerifyError("verify_failed", "Could not reach the Keboola stack to verify the token")
    if resp.status_code in (400, 401, 403):
        raise KeboolaVerifyError("invalid_token", "Keboola rejected the token")
    if resp.status_code != 200:
        logger.warning("Keboola verify returned HTTP %s", resp.status_code)
        raise KeboolaVerifyError("verify_failed", f"Keboola verify returned HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError:  # json.JSONDecodeError is a ValueError subclass
        raise KeboolaVerifyError("verify_failed", "Keboola verify returned a non-JSON response")


def _identity_from_payload(payload: Dict[str, Any]) -> VerifiedKeboolaIdentity:
    if not payload.get("isMasterToken"):
        raise KeboolaVerifyError(
            "not_master_token",
            "Only a master (admin) Storage API token can authenticate — restricted tokens are rejected",
        )
    expected = configured_project_id()
    if expected is None:
        raise KeboolaVerifyError("not_configured", "auth.keboola.project_id is not configured")
    if not project_matches(expected, payload):
        raise KeboolaVerifyError(
            "project_mismatch",
            f"The token belongs to a different Keboola project than this instance is bound to (expected project {expected})",
        )
    admin_owner = payload.get("adminOwner") or {}
    email = str(admin_owner.get("email") or "").strip()
    if not email:
        raise KeboolaVerifyError(
            "no_admin_identity",
            "The verified token carries no admin identity (adminOwner.email missing)",
        )
    role = str((payload.get("admin") or {}).get("role") or "")
    roles = allowed_roles()
    if roles is not None and role not in roles:
        raise KeboolaVerifyError(
            "role_forbidden",
            f"Keboola project role {role or 'unknown'!r} is not permitted on this instance",
        )
    project_id, project_name = project_identity(payload)
    return VerifiedKeboolaIdentity(
        token_id=str(payload.get("id") or ""),
        project_id=str(project_id),
        project_name=project_name,
        email=email,
        name=str(admin_owner.get("name") or ""),
        role=role,
    )


def _configured_base_url() -> str:
    """Config-only gates, checked BEFORE any network I/O.

    An unconfigured instance (missing stack_url OR project_id) must fail
    closed without ever reaching the network.
    """
    base = stack_url()
    if not base:
        raise KeboolaVerifyError("not_configured", "No Keboola stack URL configured")
    if configured_project_id() is None:
        raise KeboolaVerifyError("not_configured", "auth.keboola.project_id is not configured")
    return base


def verify_storage_token(token: str) -> VerifiedKeboolaIdentity:
    """Verify a plain Storage API token (X-StorageApi-Token header path)."""
    base = _configured_base_url()
    payload = _fetch_verify(base, {"X-StorageApi-Token": token})
    return _identity_from_payload(payload)


def verify_oauth_access_token(access_token: str) -> VerifiedKeboolaIdentity:
    """Verify an OAuth access token from the login flow (Bearer path).

    Named assumption (spec): Bearer acceptance on /tokens/verify is real but
    publicly undocumented platform behavior.
    """
    base = _configured_base_url()
    payload = _fetch_verify(base, {"Authorization": f"Bearer {access_token}"})
    return _identity_from_payload(payload)
