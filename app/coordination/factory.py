"""Singleton :class:`app.coordination.base.CoordinationBackend` factory.

Backend selection (env overrides ``instance.yaml``, mirroring the
env-overrides-yaml shape used throughout ``app/instance_config.py``):

- ``coordination.backend`` (instance.yaml) / ``AGNES_COORDINATION_BACKEND``
  (env) — ``"memory"`` (default) or ``"redis"``.
- ``redis.url`` (instance.yaml) / ``AGNES_REDIS_URL`` (env) — the Redis
  connection URL, only consulted when the backend is ``"redis"``.

``memory`` is the zero-config default for all-in-one deployments.
``redis`` is required for multi-process deployments — see
``app/startup_guards.py``, which treats a configured ``redis`` backend as
a declaration of multi-process intent even in an otherwise all-in-one
topology.
"""

from __future__ import annotations

import os
import threading

from app.coordination.base import CoordinationBackend

_lock = threading.Lock()
_instance: CoordinationBackend | None = None

_DEFAULT_REDIS_URL = "redis://localhost:6379/0"

#: Bounded socket timeouts for the redis-py client (seconds). Every lease
#: heartbeat (acquire/renew/release — see app/coordination/leases.py) runs
#: on a continuous cadence off the asyncio event loop via `asyncio.to_thread`;
#: without a cap, a hung/slow Redis TCP connection would block that
#: worker thread indefinitely instead of surfacing as a timely
#: `redis.exceptions.TimeoutError` (which `RedisCoordinationBackend` turns
#: into `CoordinationUnavailable` — see its docstring) that the lease loop
#: already knows how to tolerate/recover from.
_REDIS_SOCKET_TIMEOUT_S = 3.0
_REDIS_SOCKET_CONNECT_TIMEOUT_S = 3.0


def resolve_backend_name() -> str:
    """Effective coordination backend name — env overrides instance.yaml.

    Shared by :func:`_build` (this module) and
    ``app.startup_guards._coordination_backend`` (a thin wrapper around this
    function), so both call sites resolve the backend the exact same way
    instead of maintaining duplicate resolution logic.
    """
    from app.instance_config import get_value

    raw = os.environ.get("AGNES_COORDINATION_BACKEND") or get_value("coordination", "backend", default="memory")
    return (raw or "memory").strip().lower()


def resolve_redis_url() -> str:
    """Effective Redis connection URL — env overrides instance.yaml.

    Shared by :func:`_build` (this module) and any other caller that needs
    the same Redis endpoint the coordination backend itself connects to
    (e.g. ``app.auth.rate_limit`` wiring slowapi's ``storage_uri`` to the
    same Redis instance when the coordination backend is ``redis``) —
    duplicating the env/yaml resolution logic in a second place would let
    the two drift.
    """
    from app.instance_config import get_value

    raw = os.environ.get("AGNES_REDIS_URL") or get_value("redis", "url", default=_DEFAULT_REDIS_URL)
    return (raw or _DEFAULT_REDIS_URL).strip()


def _build() -> CoordinationBackend:
    if resolve_backend_name() == "redis":
        import redis as redis_lib

        from app.coordination.redis_backend import RedisCoordinationBackend

        client = redis_lib.Redis.from_url(
            resolve_redis_url(),
            decode_responses=True,
            socket_timeout=_REDIS_SOCKET_TIMEOUT_S,
            socket_connect_timeout=_REDIS_SOCKET_CONNECT_TIMEOUT_S,
        )
        return RedisCoordinationBackend(client)

    from app.coordination.memory import MemoryCoordinationBackend

    return MemoryCoordinationBackend()


def coordination() -> CoordinationBackend:
    """Return the process-wide :class:`CoordinationBackend` singleton,
    building it lazily on first call (or after
    :func:`reset_coordination_for_tests`)."""
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = _build()
    return _instance


def reset_coordination_for_tests() -> None:
    """Drop the singleton so the next :func:`coordination` call re-reads
    config and builds a fresh backend.

    Also closes the outgoing instance's Redis pub/sub listener thread (if
    it has one — see :meth:`app.coordination.redis_backend.RedisCoordinationBackend.close`)
    so tests that flip backends repeatedly don't leak daemon threads.
    """
    global _instance
    with _lock:
        old = _instance
        _instance = None
    close = getattr(old, "close", None)
    if callable(close):
        close()
