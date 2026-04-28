"""Sync a user's Google Workspace group membership into users.groups.

Called from `app/auth/providers/google.py` in the OAuth callback. Uses the
Cloud Identity API (searchTransitiveGroups — returns nested group
memberships too) with Application Default Credentials from the VM metadata
server. No JSON key, no domain-wide delegation.

Required one-off Workspace setup:
  - Assign Groups Admin admin role to the VM service account.
  - See docs/google-workspace-groups-request.md.

Required VM config:
  - `cloud-platform` access scope on the VM (already set on
    grpn-sa-foundryai-execution) — covers `cloud-identity.groups.readonly`.
  - Cloud Identity API enabled on the project.

Local dev / CI:
  Set GOOGLE_ADMIN_SDK_MOCK_GROUPS to a comma-separated list. ADC from the
  metadata server doesn't exist off-VM; without this flag local runs fall
  through to the real-path and bail out with an empty list (fail-soft).
"""

from __future__ import annotations

import logging
import os
from typing import List

logger = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/cloud-identity.groups.readonly"

# CEL label filter — regular Workspace email groups (grp_*, eng-team@..., etc).
# Skips security groups, dynamic groups, POSIX groups, which we don't use for
# plugin RBAC.
_GROUP_LABEL_DISCUSSION = "cloudidentity.googleapis.com/groups.discussion_forum"

# Env var that, when set, bypasses the real API entirely. Value is comma-
# separated group names. Empty string → empty list. Unset → real API path.
MOCK_ENV = "GOOGLE_ADMIN_SDK_MOCK_GROUPS"


def fetch_user_groups(email: str) -> List[str]:
    """Return the list of group names (emails) the user belongs to.

    Fail-soft: returns [] on any error. Caller must treat this as a soft
    signal (login proceeds, users.groups stays whatever it was before).
    """
    mock = os.environ.get(MOCK_ENV)
    if mock is not None:
        return [g.strip() for g in mock.split(",") if g.strip()]
    return _fetch_real(email)


def _fetch_real(email: str) -> List[str]:
    try:
        from google.auth import default
        from googleapiclient.discovery import build
    except ImportError:
        logger.warning(
            "google-api-python-client not installed; skipping group fetch"
        )
        return []

    try:
        creds, _ = default(scopes=[SCOPE])
        service = build(
            "cloudidentity", "v1", credentials=creds, cache_discovery=False
        )
    except Exception as e:  # noqa: BLE001 - fail-soft by design
        logger.warning("Google client init failed: %s", e)
        return []

    # Escape single quotes in the email to keep the CEL query well-formed even
    # if a user has a quote in their login (rare, but defensive).
    safe_email = email.replace("'", "\\'")
    query = (
        f"member_key_id == '{safe_email}' && "
        f"'{_GROUP_LABEL_DISCUSSION}' in labels"
    )

    groups: List[str] = []
    page_token = None
    try:
        while True:
            resp = (
                service.groups()
                .memberships()
                .searchTransitiveGroups(
                    parent="groups/-",
                    query=query,
                    pageToken=page_token,
                )
                .execute()
            )
            for m in resp.get("memberships", []):
                gkey = m.get("groupKey", {}).get("id")
                if gkey:
                    groups.append(gkey)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    except Exception as e:  # noqa: BLE001 - fail-soft by design
        logger.warning("Group fetch failed for %s: %s", email, e)
        return []

    return groups
