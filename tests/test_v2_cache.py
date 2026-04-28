import pytest
import time

from app.api.v2_cache import TTLCache


class TestTTLCache:
    def test_set_get(self):
        c = TTLCache(maxsize=10, ttl_seconds=60)
        c.set("k", "v")
        assert c.get("k") == "v"

    def test_get_missing_returns_default(self):
        c = TTLCache(maxsize=10, ttl_seconds=60)
        assert c.get("missing") is None
        assert c.get("missing", default="x") == "x"

    def test_expiry(self, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr("app.api.v2_cache._now", lambda: now[0])
        c = TTLCache(maxsize=10, ttl_seconds=10)
        c.set("k", "v")
        assert c.get("k") == "v"
        now[0] += 11
        assert c.get("k") is None  # expired

    def test_lru_eviction(self):
        c = TTLCache(maxsize=2, ttl_seconds=60)
        c.set("a", 1)
        c.set("b", 2)
        c.set("c", 3)  # should evict 'a' (LRU)
        assert c.get("a") is None
        assert c.get("b") == 2
        assert c.get("c") == 3

    def test_invalidate(self):
        c = TTLCache(maxsize=10, ttl_seconds=60)
        c.set("k", "v")
        c.invalidate("k")
        assert c.get("k") is None

    def test_clear(self):
        c = TTLCache(maxsize=10, ttl_seconds=60)
        c.set("a", 1)
        c.set("b", 2)
        c.clear()
        assert c.get("a") is None
        assert c.get("b") is None
