"""Resolve datasource credentials: env > vault > None.

Environment variables are authoritative (Terraform / secret-manager
deployments stay in control); the ``system_secrets`` vault is the
UI-managed fallback. Only the known datasource secret names are
resolvable via the vault — the allow-list prevents using the vault
namespace to read arbitrary environment variables.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

DATA_SOURCE_SECRET_NAMES = (
    "KEBOOLA_STORAGE_TOKEN",
    "BIGQUERY_SERVICE_ACCOUNT_JSON",
    "AGNES_GWS_CLIENT_ID",
    "AGNES_GWS_CLIENT_SECRET",
)


def datasource_secret(name: str) -> Optional[str]:
    """Return the value for ``name`` resolving env > vault > None.

    Raises ``ValueError`` for any name outside the datasource allow-list. A
    vault lookup failure (DB unavailable, etc.) is swallowed and treated as
    unset so the caller can fall back to the YAML config path.
    """
    if name not in DATA_SOURCE_SECRET_NAMES:
        raise ValueError(f"{name!r} is not a known datasource secret name")
    env = os.environ.get(name)
    if env:
        return env
    try:
        from src.repositories import system_secrets_repo

        return system_secrets_repo().get(name)
    except Exception:
        logger.warning("vault lookup for %s failed; treating as unset", name)
        return None
