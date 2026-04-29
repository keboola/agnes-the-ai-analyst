"""Simple thread-safe LRU + TTL cache for v2 endpoints (spec §3.6)."""

from __future__ import annotations
import threading
import time
from collections import OrderedDict
from typing import Any


def _now() -> float:  # patched in tests
    return time.monotonic()


class TTLCache:
    def __init__(self, *, maxsize: int, ttl_seconds: float):
        self._max = maxsize
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._data: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return default
            expiry, value = entry
            if _now() > expiry:
                del self._data[key]
                return default
            self._data.move_to_end(key)  # mark as recently used
            return value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            expiry = _now() + self._ttl
            if key in self._data:
                self._data.move_to_end(key)
            self._data[key] = (expiry, value)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
