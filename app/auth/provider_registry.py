"""Per-instance auth provider allowlist (spec 2026-08-12).

``auth.providers`` in instance.yaml (env override ``AGNES_AUTH_PROVIDERS``,
comma-separated) narrows which login methods this instance offers. Unset =
every available provider — byte-for-byte the pre-allowlist behavior. An
explicitly empty (or all-unknown) list is a misconfiguration: rejected at
the admin API, and treated here as unset with a loud error log so one
overlay write can never lock every user out of the instance.
"""

import logging
import os
from typing import Callable, Optional

from fastapi import HTTPException

from app.instance_config import get_value

logger = logging.getLogger(__name__)

KNOWN_PROVIDERS: tuple[str, ...] = ("google", "email", "password", "keboola")


def configured_allowlist() -> Optional[list[str]]:
    raw_env = os.environ.get("AGNES_AUTH_PROVIDERS")
    if raw_env is not None:
        values = [v.strip() for v in raw_env.split(",") if v.strip()]
    else:
        configured = get_value("auth", "providers")
        if configured is None:
            return None
        if isinstance(configured, str):
            values = [v.strip() for v in configured.split(",") if v.strip()]
        else:
            values = [str(v).strip() for v in configured if str(v).strip()]
    unknown = [v for v in values if v not in KNOWN_PROVIDERS]
    for name in unknown:
        logger.warning("auth.providers: unknown provider %r ignored", name)
    known = [v for v in values if v in KNOWN_PROVIDERS]
    if not known:
        logger.error(
            "auth.providers is set but names no known provider — treating as unset "
            "(all providers) so the instance stays reachable; fix the configuration"
        )
        return None
    return known


def provider_allowed(name: str) -> bool:
    allowlist = configured_allowlist()
    return allowlist is None or name in allowlist


def require_provider(name: str) -> Callable[[], None]:
    """Router-level dependency: excluded provider endpoints return 404
    (not 403 — an excluded method should not advertise its existence)."""

    def _dep() -> None:
        if not provider_allowed(name):
            raise HTTPException(status_code=404, detail="Not Found")

    return _dep
