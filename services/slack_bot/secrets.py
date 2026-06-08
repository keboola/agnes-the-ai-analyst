"""Resolve Slack bot secrets: env > vault > None.

Environment variables are authoritative (Terraform / secret-manager
deployments stay in control); the ``system_secrets`` vault is the
UI-managed fallback. Only the three known Slack secret names are
resolvable via the vault — the allow-list prevents using the vault
namespace to read arbitrary environment variables.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

SLACK_SECRET_NAMES = (
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_SIGNING_SECRET",
)


def slack_secret(name: str) -> Optional[str]:
    """Return the value for ``name`` resolving env > vault > None.

    Raises ``ValueError`` for any name outside the Slack allow-list. A vault
    lookup failure (DB unavailable, etc.) is swallowed and treated as unset
    so signature verification fails closed (401) rather than 500-ing.
    """
    if name not in SLACK_SECRET_NAMES:
        raise ValueError(f"{name!r} is not a Slack secret name")
    env = os.environ.get(name)
    if env:
        return env
    try:
        from src.repositories import system_secrets_repo

        return system_secrets_repo().get(name)
    except Exception:
        logger.warning("vault lookup for %s failed; treating as unset", name)
        return None
