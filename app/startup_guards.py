"""Refuse unsafe multi-process topologies at boot.

Multi-process = role split (AGNES_ROLE != all) OR UVICORN_WORKERS > 1 OR
coordination.backend=redis. The third trigger closes a deferred wave-1
finding: configuring the Redis coordination backend is itself a
declaration of multi-process intent, even in an otherwise all-in-one
topology (e.g. staging a redis-backed rollout ahead of the actual role
split) — such a deployment must ALSO satisfy the other multi-process
requirements below, not just gain the coordination backend.

Multi-process deployments require: Postgres app-state, explicit shared
secrets, and a Redis coordination backend. Single-process ``all`` mode
with the default memory coordination backend has no new requirements.
Spec §3.2/§3.7.
"""

import os

from app.roles import is_all_in_one


class DeploymentConfigError(RuntimeError):
    pass


def _use_pg() -> bool:
    from src.repositories import use_pg

    return use_pg()


def _coordination_backend() -> str:
    """Effective coordination backend — same resolution as
    ``app.coordination.factory._backend_name`` (env overrides instance.yaml)
    so this guard reacts to the exact backend the process will actually use,
    not just the yaml-only view of it."""
    from app.instance_config import get_value

    raw = os.environ.get("AGNES_COORDINATION_BACKEND") or get_value("coordination", "backend", default="memory")
    return (raw or "memory").strip().lower()


def _workers() -> int:
    try:
        return int(os.environ.get("UVICORN_WORKERS", "1"))
    except ValueError:
        return 1


def is_multi_process() -> bool:
    return (not is_all_in_one()) or _workers() > 1 or _coordination_backend() == "redis"


def validate_deployment() -> None:
    if not is_multi_process():
        return
    problems: list[str] = []
    if not _use_pg():
        problems.append("app-state backend must be Postgres (set DATABASE_URL or instance.yaml::database.backend)")
    for var in ("JWT_SECRET_KEY", "SESSION_SECRET"):
        if not os.environ.get(var):
            problems.append(f"{var} must be set explicitly (no per-node autogeneration)")
    if _coordination_backend() != "redis":
        problems.append("coordination.backend must be 'redis' (instance.yaml::coordination.backend)")
    if problems:
        raise DeploymentConfigError(
            "Multi-process deployment (AGNES_ROLE split or UVICORN_WORKERS>1) "
            "is not safely configured:\n  - " + "\n  - ".join(problems) + "\nSee docs/DEPLOYMENT.md#multi-process."
        )
