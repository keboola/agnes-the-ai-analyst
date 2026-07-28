"""Process-role resolution for the three-plane deployment model.

One image, one entrypoint: ``AGNES_ROLE`` (env, comma-separable) or
``instance.yaml::deployment.role`` selects which planes this process
serves. Default ``all`` keeps today's single-process behavior.
Spec: docs/superpowers/specs/2026-07-16-three-plane-scalable-architecture.md §3.1.
"""

import os
from enum import StrEnum
from functools import lru_cache


class Role(StrEnum):
    API = "api"
    GATEWAY = "gateway"
    WORKER = "worker"


_ALL = frozenset({Role.API, Role.GATEWAY, Role.WORKER})


def _config_role() -> str | None:
    from app.instance_config import get_value

    return get_value("deployment", "role", default=None)


@lru_cache(maxsize=1)
def active_roles() -> frozenset[Role]:
    raw = os.environ.get("AGNES_ROLE") or _config_role() or "all"
    tokens = [t.strip().lower() for t in raw.split(",") if t.strip()]
    if "all" in tokens:
        return _ALL
    roles: set[Role] = set()
    for tok in tokens:
        try:
            roles.add(Role(tok))
        except ValueError:
            valid = ", ".join([r.value for r in Role] + ["all"])
            raise ValueError(f"Invalid AGNES_ROLE token {tok!r} — valid tokens: {valid}") from None
    return frozenset(roles)


def role_enabled(role: Role) -> bool:
    return role in active_roles()


def is_all_in_one() -> bool:
    return active_roles() == _ALL


def reset_roles_cache() -> None:
    active_roles.cache_clear()
