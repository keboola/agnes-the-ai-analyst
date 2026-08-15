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
- The ``isMasterToken`` gate in ``_identity_from_payload`` is shared by both
  ``verify_storage_token`` (header path) and ``verify_oauth_access_token``
  (login path) on purpose, and it does not contradict the "guest/readOnly
  can sign in" design: a human's interactive Keboola login/OAuth token IS a
  master token (the OAuth flow cannot issue a restricted API token), so this
  gate only ever rejects restricted tokens presented to the header path.
  That "IS a master token" claim is itself a platform assumption we could
  not verify against documentation — which is precisely why the login path
  raises its own ``oauth_not_master_token`` reason instead of the header
  path's ``not_master_token``: if the assumption is ever wrong, the failure
  names the broken assumption (and surfaces as its own error code on the
  login page) rather than masquerading as a restricted-token rejection.
  Project-role filtering for humans (guest, readOnly, etc.) is a separate
  concern, handled below by ``admin.role`` + ``allowed_roles()``.
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


def multi_project_mode() -> str:
    """The instance's multi-project posture — ``disabled`` (default; the
    single-project gate from the original provider), ``select``
    (discover at login, the user picks which projects to import) or
    ``auto`` (discover + provision every allowed project on each login).
    Resolved through the switch registry so env override, validation and
    the admin panel all agree (`keboola_multi_project_mode`)."""
    from app.switches import switch_value

    return str(switch_value("keboola_multi_project_mode"))


def multi_project_active() -> bool:
    """True when login-time project discovery runs at all."""
    return multi_project_mode() in ("select", "auto")


def is_wildcard_project() -> bool:
    """True when this instance is NOT pinned to one Keboola project: a
    discovery mode is on and ``project_id`` is ``"*"`` or unset. The
    single-project verify gates (project binding; the OAuth path's
    home-project role gate) are skipped — membership in at least one
    allowed-role project, per the OAuth host's introspect, becomes the
    trust boundary instead (see ``keboola_projects.filter_projects``)."""
    if not multi_project_active():
        return False
    pid = configured_project_id()
    return pid is None or pid == "*"


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

    # Same bar as the source-connection sibling (_validate_stack_url,
    # "Rejects non-https"): the header path sends a master Storage token in
    # this request, and http would put it on the wire in cleartext. The
    # shared host validator checks only hostname/IP, so the scheme must be
    # enforced here (Devin Review on PR #1288).
    if not str(base_url or "").lower().startswith("https://"):
        raise KeboolaVerifyError("verify_failed", "auth.keboola.stack_url must be https")
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


def _identity_from_payload(payload: Dict[str, Any], *, source: str = "header") -> VerifiedKeboolaIdentity:
    """``source`` is which verify path produced ``payload`` — ``"header"``
    (plain Storage token) or ``"oauth"`` (login flow). It only picks the
    master-token failure's reason: the login path self-describes with
    ``oauth_not_master_token`` because hitting it means the platform
    assumption in the module docstring broke, not that a restricted token
    was presented."""
    if not payload.get("isMasterToken"):
        if source == "oauth":
            raise KeboolaVerifyError(
                "oauth_not_master_token",
                "The OAuth access token did not verify as a master token — this "
                "contradicts the assumption that an interactive Keboola login always "
                "yields one (see module docstring); please report it",
            )
        raise KeboolaVerifyError(
            "not_master_token",
            "Only a master (admin) Storage API token can authenticate — restricted tokens are rejected",
        )
    wildcard = is_wildcard_project()
    if not wildcard:
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
    # Under the wildcard the OAuth path's role gate moves to discovery:
    # ``admin.role`` here is the role in the token's HOME project only, and a
    # user who is e.g. admin of project A must not be turned away because the
    # OAuth token's home project B lists them as guest. The introspect-based
    # filter (``keboola_projects.filter_projects``) enforces allowed_roles
    # across every project, and the callback rejects a login with zero
    # surviving projects. The header path keeps the gate: a plain Storage
    # token cannot call the OAuth introspect API, so its home-project role is
    # the only role there is to check.
    if roles is not None and role not in roles and not (wildcard and source == "oauth"):
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
    if configured_project_id() is None and not multi_project_active():
        raise KeboolaVerifyError("not_configured", "auth.keboola.project_id is not configured")
    return base


def verify_storage_token(token: str) -> VerifiedKeboolaIdentity:
    """Verify a plain Storage API token (X-StorageApi-Token header path)."""
    base = _configured_base_url()
    payload = _fetch_verify(base, {"X-StorageApi-Token": token})
    return _identity_from_payload(payload, source="header")


def verify_oauth_access_token(access_token: str) -> VerifiedKeboolaIdentity:
    """Verify an OAuth access token from the login flow (Bearer path).

    Named assumption (spec): Bearer acceptance on /tokens/verify is real but
    publicly undocumented platform behavior.
    """
    base = _configured_base_url()
    payload = _fetch_verify(base, {"Authorization": f"Bearer {access_token}"})
    return _identity_from_payload(payload, source="oauth")
