"""In-process CoordinationBackend — the default, single-process implementation.

Every primitive is backed by one dict guarded by a single
``threading.Lock``, with expiry tracked against ``time.monotonic()`` (immune
to wall-clock adjustments). Correct within one Python process; invisible
across processes — that's what :mod:`app.coordination.redis_backend` is
for. Zero-config default for all-in-one deployments (``coordination.backend``
unset or ``"memory"`` in ``instance.yaml``).
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from app.coordination.base import CoordinationBackend


class MemoryCoordinationBackend(CoordinationBackend):
    """See :class:`app.coordination.base.CoordinationBackend` for the contract."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # key -> (value, expiry monotonic timestamp)
        self._kv: dict[str, tuple[str, float]] = {}
        # name -> (holder_id, expiry monotonic timestamp)
        self._leases: dict[str, tuple[str, float]] = {}
        self._subscribers: dict[str, list[Callable[[str], None]]] = {}

    @staticmethod
    def _now() -> float:
        return time.monotonic()

    # -- KV -------------------------------------------------------------------

    def kv_set(self, key: str, value: str, *, ttl_s: int) -> None:
        with self._lock:
            self._kv[key] = (value, self._now() + ttl_s)

    def kv_get(self, key: str) -> Optional[str]:
        with self._lock:
            entry = self._kv.get(key)
            if entry is None:
                return None
            value, expiry = entry
            if expiry <= self._now():
                del self._kv[key]
                return None
            return value

    def kv_delete(self, key: str) -> Optional[str]:
        with self._lock:
            entry = self._kv.pop(key, None)
            if entry is None:
                return None
            value, expiry = entry
            if expiry <= self._now():
                return None
            return value

    # -- Counters ---------------------------------------------------------------

    def incr(self, key: str, *, amount: int = 1, ttl_s: int) -> int:
        with self._lock:
            now = self._now()
            entry = self._kv.get(key)
            if entry is not None and entry[1] <= now:
                entry = None  # expired window — treat as absent
            if entry is None:
                new_value = amount
                self._kv[key] = (str(new_value), now + ttl_s)
                return new_value
            value, expiry = entry
            new_value = int(value) + amount
            self._kv[key] = (str(new_value), expiry)  # TTL untouched
            return new_value

    # -- Leases -------------------------------------------------------------------

    def lease_acquire(self, name: str, holder_id: str, *, ttl_s: int) -> bool:
        with self._lock:
            current = self._leases.get(name)
            if current is not None and current[1] > self._now():
                return False  # held by someone (possibly this holder) and not expired
            self._leases[name] = (holder_id, self._now() + ttl_s)
            return True

    def lease_renew(self, name: str, holder_id: str, *, ttl_s: int) -> bool:
        with self._lock:
            current = self._leases.get(name)
            if current is None:
                return False
            current_holder, expiry = current
            if current_holder != holder_id or expiry <= self._now():
                return False
            self._leases[name] = (holder_id, self._now() + ttl_s)
            return True

    def lease_release(self, name: str, holder_id: str) -> None:
        with self._lock:
            current = self._leases.get(name)
            if current is not None and current[0] == holder_id:
                del self._leases[name]

    # -- Pub/sub ------------------------------------------------------------------

    def publish(self, channel: str, message: str) -> None:
        # Snapshot the handler list under the lock, then fire outside it —
        # a handler that (re)subscribes/unsubscribes must not deadlock on
        # our own lock.
        with self._lock:
            handlers = list(self._subscribers.get(channel, ()))
        for handler in handlers:
            handler(message)

    def subscribe(self, channel: str, handler: Callable[[str], None]) -> Callable[[], None]:
        with self._lock:
            self._subscribers.setdefault(channel, []).append(handler)

        def _unsubscribe() -> None:
            with self._lock:
                handlers = self._subscribers.get(channel)
                if handlers and handler in handlers:
                    handlers.remove(handler)

        return _unsubscribe

    def ping(self) -> bool:
        return True
