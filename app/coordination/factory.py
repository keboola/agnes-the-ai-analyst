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


def _backend_name() -> str:
    from app.instance_config import get_value

    raw = os.environ.get("AGNES_COORDINATION_BACKEND") or get_value("coordination", "backend", default="memory")
    return (raw or "memory").strip().lower()


def _redis_url() -> str:
    from app.instance_config import get_value

    raw = os.environ.get("AGNES_REDIS_URL") or get_value("redis", "url", default=_DEFAULT_REDIS_URL)
    return (raw or _DEFAULT_REDIS_URL).strip()


def _build() -> CoordinationBackend:
    if _backend_name() == "redis":
        import redis as redis_lib

        from app.coordination.redis_backend import RedisCoordinationBackend

        client = redis_lib.Redis.from_url(_redis_url(), decode_responses=True)
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
