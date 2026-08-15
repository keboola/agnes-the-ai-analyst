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

    def test_invalidate_prefix_drops_every_matching_key(self):
        """`invalidate` is an exact-key delete, so it never reaches the
        composite keys a policied table's schema entries live under
        (`f"{table_id}|policy:{identity!r}"`). Prefix invalidation is what
        an admin's policy edit needs to actually take effect."""
        c = TTLCache(maxsize=10, ttl_seconds=60)
        c.set("orders|policy:('u1', ('TeamA',))", 1)
        c.set("orders|policy:('u2', ('TeamB',))", 2)
        c.set("line_items|policy:('u1', ('TeamA',))", 3)

        c.invalidate_prefix("orders|")

        assert c.get("orders|policy:('u1', ('TeamA',))") is None
        assert c.get("orders|policy:('u2', ('TeamB',))") is None
        assert c.get("line_items|policy:('u1', ('TeamA',))") == 3

    def test_invalidate_prefix_is_a_literal_prefix_match(self):
        """Which is why callers pass the key delimiter, not a bare id — a
        table named `orders` must not evict `orders_archive`'s entries."""
        c = TTLCache(maxsize=10, ttl_seconds=60)
        c.set("orders_archive|policy:('u1', ())", 1)

        c.invalidate_prefix("orders|")
        assert c.get("orders_archive|policy:('u1', ())") == 1

        c.invalidate_prefix("orders")
        assert c.get("orders_archive|policy:('u1', ())") is None

    def test_invalidate_prefix_on_an_empty_cache_is_a_noop(self):
        c = TTLCache(maxsize=10, ttl_seconds=60)
        c.invalidate_prefix("orders|")
        assert c.get("orders") is None
