"""CoordinationBackend — cross-process coordination primitives.

TTL key/value (single-use tickets, operational codes), counters (rate
limits / quotas), leases (leader election, singleton sweeps), and pub/sub
(cache invalidation) behind one interface with two implementations:

- :class:`app.coordination.memory.MemoryCoordinationBackend` — in-process,
  zero-config default. Correct within one Python process only.
- :class:`app.coordination.redis_backend.RedisCoordinationBackend` — via
  redis-py, for multi-process / multi-replica deployments.

Callsites obtain the active singleton through :func:`coordination` —
never instantiate a backend class directly. Backend selection is
``coordination.backend`` in ``instance.yaml`` (``AGNES_COORDINATION_BACKEND``
env override); the Redis connection URL is ``redis.url``
(``AGNES_REDIS_URL`` env override). See :mod:`app.coordination.factory`.

``app/startup_guards.py`` treats ``coordination.backend=redis`` as a
declaration of multi-process intent even in an otherwise all-in-one
topology — see :func:`app.startup_guards.is_multi_process`.
"""

from app.coordination.base import CoordinationBackend, CoordinationUnavailable
from app.coordination.factory import coordination, reset_coordination_for_tests

__all__ = [
    "CoordinationBackend",
    "CoordinationUnavailable",
    "coordination",
    "reset_coordination_for_tests",
]
