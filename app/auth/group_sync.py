"""Sync a user's Google Workspace group membership at OAuth callback.

Called from `app/auth/providers/google.py`. Uses keyless Domain-Wide
Delegation: the VM service account signs the impersonation JWT via the IAM
``signJwt`` API (no private key on disk), then exchanges that JWT for a
short-lived OAuth token scoped to ``admin.directory.group.readonly``. The
Admin SDK ``groups.list?userKey=`` endpoint returns the user's static AND
dynamic group memberships in one call.

Required GCP setup (one-off):

  - The VM SA grants itself ``roles/iam.serviceAccountTokenCreator`` so it
    can call ``IAMCredentials.signJwt`` for its own identity.
  - A Domain-Wide Delegation entry exists in admin.google.com → Security →
    API controls → Domain-wide Delegation, mapping the VM SA's numeric
    Unique ID to scope ``admin.directory.group.readonly``.

Required env on the VM:

  - ``GOOGLE_ADMIN_SDK_SUBJECT`` — the Workspace admin email the SA
    impersonates. Must be a real Workspace user with directory read
    privileges. When unset, this module fails soft and returns ``None``.
  - ``GOOGLE_ADMIN_SDK_SA_EMAIL`` (optional) — explicit SA email override.
    When unset, the SA is auto-detected from the GCE metadata server, i.e.
    whichever SA the VM is currently running as. Useful off-VM (CI, tests).

Local dev / CI:

  Set ``GOOGLE_ADMIN_SDK_MOCK_GROUPS`` to a comma-separated list of group
  emails to bypass all Google calls. Empty value → empty list. Unset →
  the real keyless-DWD path.
"""

from __future__ import annotations

import logging
import os
import urllib.error
import urllib.request
from typing import List, Optional

logger = logging.getLogger(__name__)

# Bypass real API entirely. Comma-separated group emails. Empty → []. Unset →
# real keyless-DWD path.
MOCK_ENV = "GOOGLE_ADMIN_SDK_MOCK_GROUPS"

# Required: the Workspace admin email impersonated through DWD.
SUBJECT_ENV = "GOOGLE_ADMIN_SDK_SUBJECT"

# Optional: SA email override. When unset, auto-detect from GCE metadata.
SA_EMAIL_ENV = "GOOGLE_ADMIN_SDK_SA_EMAIL"

SCOPE = "https://www.googleapis.com/auth/admin.directory.group.readonly"

_METADATA_SA_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/"
    "service-accounts/default/email"
)


def fetch_user_groups(email: str) -> Optional[List[str]]:
    """Return the list of group emails ``email`` is a member of.

    Three-state return so the caller can distinguish between "Google said
    zero groups" and "we couldn't ask Google":

      * ``[...]`` — API answered with this list.
      * ``[]``   — API answered, user has zero groups.
      * ``None`` — soft fail: missing config, metadata server unreachable,
        API 4xx/5xx, network outage. The caller decides whether to
        deny login or pass-through on cached membership.

    The mock env (``GOOGLE_ADMIN_SDK_MOCK_GROUPS``) always returns a list
    — never ``None`` — because tests use the empty-string value as the
    explicit "API succeeded with zero groups" signal.
    """
    mock = os.environ.get(MOCK_ENV)
    if mock is not None:
        return [g.strip() for g in mock.split(",") if g.strip()]
    return _fetch_real(email)


def _detect_sa_email() -> str | None:
    """Return the SA email this process should impersonate as.

    Order of resolution:
      1. ``GOOGLE_ADMIN_SDK_SA_EMAIL`` env var — explicit override.
      2. GCE metadata server — the SA the VM is attached to.

    Returns ``None`` when neither is available (off-VM with no override).
    """
    explicit = os.environ.get(SA_EMAIL_ENV, "").strip()
    if explicit:
        return explicit
    try:
        req = urllib.request.Request(
            _METADATA_SA_URL,
            headers={"Metadata-Flavor": "Google"},
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.read().decode("ascii").strip()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return None


def _fetch_real(email: str) -> Optional[List[str]]:
    try:
        from google.auth import default, iam
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError:
        logger.warning(
            "google-api-python-client / google-auth not installed; "
            "skipping group fetch"
        )
        return None

    subject = os.environ.get(SUBJECT_ENV, "").strip()
    if not subject:
        logger.warning(
            "%s not set; skipping group fetch (keyless DWD requires an "
            "admin email to impersonate)",
            SUBJECT_ENV,
        )
        return None

    sa_email = _detect_sa_email()
    if not sa_email:
        logger.warning(
            "Could not determine VM service account email "
            "(metadata server unreachable and %s not set); "
            "skipping group fetch",
            SA_EMAIL_ENV,
        )
        return None

    try:
        source, _ = default()
        signer = iam.Signer(Request(), source, sa_email)
        creds = service_account.Credentials(
            signer=signer,
            service_account_email=sa_email,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=[SCOPE],
            subject=subject,
        )
        service = build(
            "admin", "directory_v1",
            credentials=creds,
            cache_discovery=False,
        )
    except Exception as e:  # noqa: BLE001 - fail-soft by design
        logger.warning("Admin SDK init failed: %s", e)
        return None

    groups: List[str] = []
    page_token: str | None = None
    try:
        while True:
            resp = service.groups().list(
                userKey=email,
                maxResults=200,
                pageToken=page_token,
            ).execute()
            for g in resp.get("groups", []):
                gid = g.get("email")
                if gid:
                    groups.append(gid)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    except Exception as e:  # noqa: BLE001 - fail-soft by design
        logger.warning("Group fetch failed for %s: %s", email, e)
        return None

    return groups
