"""Contract tests for CoordinationBackend.

The SAME assertion set (grouped into ``Test*`` classes below) is
parametrized across both implementations via the ``backend`` fixture:
:class:`~app.coordination.memory.MemoryCoordinationBackend` and
:class:`~app.coordination.redis_backend.RedisCoordinationBackend` running
against ``fakeredis`` (no real Redis server / Docker required). A
consumer written against the ABC gets identical guarantees regardless of
which backend a deployment configures.

A handful of Redis-only tests at the bottom cover behavior that has no
memory-backend equivalent (transport-failure -> ``CoordinationUnavailable``,
``ping()`` swallowing connection errors).
"""

from __future__ import annotations

import threading
import time

import pytest

fakeredis = pytest.importorskip("fakeredis")

from app.coordination.base import CoordinationUnavailable  # noqa: E402
from app.coordination.memory import MemoryCoordinationBackend  # noqa: E402
from app.coordination.redis_backend import RedisCoordinationBackend  # noqa: E402


def _wait_for(predicate, timeout: float = 2.0, interval: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    assert predicate(), "condition not met within timeout"


def _memory_backend() -> MemoryCoordinationBackend:
    return MemoryCoordinationBackend()


def _fakeredis_backend() -> RedisCoordinationBackend:
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    return RedisCoordinationBackend(client)


_BUILDERS = {"memory": _memory_backend, "fakeredis": _fakeredis_backend}


@pytest.fixture(params=["memory", "fakeredis"])
def backend(request):
    b = _BUILDERS[request.param]()
    yield b
    close = getattr(b, "close", None)
    if callable(close):
        close()


class TestKV:
    def test_set_get_roundtrip(self, backend):
        backend.kv_set("k1", "v1", ttl_s=60)
        assert backend.kv_get("k1") == "v1"

    def test_get_missing_returns_none(self, backend):
        assert backend.kv_get("nope") is None

    def test_ttl_expiry(self, backend):
        backend.kv_set("k2", "v2", ttl_s=1)
        assert backend.kv_get("k2") == "v2"
        time.sleep(1.3)
        assert backend.kv_get("k2") is None

    def test_delete_returns_value_and_removes_key(self, backend):
        backend.kv_set("k3", "v3", ttl_s=60)
        assert backend.kv_delete("k3") == "v3"
        assert backend.kv_get("k3") is None

    def test_delete_missing_returns_none(self, backend):
        assert backend.kv_delete("also-nope") is None
        # second delete of an already-consumed key is also None, not an error
        backend.kv_set("k4", "v4", ttl_s=60)
        assert backend.kv_delete("k4") == "v4"
        assert backend.kv_delete("k4") is None

    def test_delete_single_use_ticket_atomicity(self, backend):
        """Two threads race to consume the same ticket — exactly one wins."""
        backend.kv_set("ticket", "payload", ttl_s=60)
        winners: list[str] = []
        lock = threading.Lock()
        start = threading.Barrier(10)

        def _consume():
            start.wait()
            val = backend.kv_delete("ticket")
            if val is not None:
                with lock:
                    winners.append(val)

        threads = [threading.Thread(target=_consume) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert winners == ["payload"]


class TestIncr:
    def test_first_incr_starts_at_one(self, backend):
        assert backend.incr("c1", ttl_s=60) == 1

    def test_incr_accumulates(self, backend):
        backend.incr("c2", ttl_s=60)
        assert backend.incr("c2", ttl_s=60) == 2
        assert backend.incr("c2", ttl_s=60) == 3

    def test_ttl_only_set_on_first_incr(self, backend):
        backend.incr("c3", ttl_s=1)
        time.sleep(1.3)
        # window fully expired -> a fresh window starts at 1, not 2
        assert backend.incr("c3", ttl_s=60) == 1

    def test_concurrent_incr_no_lost_updates(self, backend):
        start = threading.Barrier(20)

        def _bump():
            start.wait()
            backend.incr("c4", ttl_s=60)

        threads = [threading.Thread(target=_bump) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert backend.kv_get("c4") == "20"


class TestLease:
    def test_acquire_succeeds_when_free(self, backend):
        assert backend.lease_acquire("l1", "holder-a", ttl_s=60) is True

    def test_acquire_is_exclusive(self, backend):
        assert backend.lease_acquire("l2", "holder-a", ttl_s=60) is True
        assert backend.lease_acquire("l2", "holder-b", ttl_s=60) is False
        # even the current holder can't "acquire" again — must renew instead
        assert backend.lease_acquire("l2", "holder-a", ttl_s=60) is False

    def test_renew_by_holder_only(self, backend):
        backend.lease_acquire("l3", "holder-a", ttl_s=60)
        assert backend.lease_renew("l3", "holder-b", ttl_s=60) is False
        assert backend.lease_renew("l3", "holder-a", ttl_s=60) is True

    def test_release_by_holder_only_then_free(self, backend):
        backend.lease_acquire("l4", "holder-a", ttl_s=60)
        backend.lease_release("l4", "holder-b")  # wrong holder — no-op
        assert backend.lease_acquire("l4", "holder-b", ttl_s=60) is False  # still held
        backend.lease_release("l4", "holder-a")
        assert backend.lease_acquire("l4", "holder-b", ttl_s=60) is True

    def test_steal_after_expiry(self, backend):
        backend.lease_acquire("l5", "holder-a", ttl_s=1)
        time.sleep(1.3)
        assert backend.lease_acquire("l5", "holder-b", ttl_s=60) is True

    def test_renew_fails_after_expiry(self, backend):
        backend.lease_acquire("l6", "holder-a", ttl_s=1)
        time.sleep(1.3)
        assert backend.lease_renew("l6", "holder-a", ttl_s=60) is False

    def test_only_one_of_many_concurrent_acquires_wins(self, backend):
        start = threading.Barrier(10)
        results: list[bool] = []
        lock = threading.Lock()

        def _try_acquire(holder: str):
            start.wait()
            ok = backend.lease_acquire("l7", holder, ttl_s=60)
            with lock:
                results.append(ok)

        threads = [threading.Thread(target=_try_acquire, args=(f"holder-{i}",)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results.count(True) == 1


class TestPubSub:
    def test_publish_delivers_to_subscriber(self, backend):
        received: list[str] = []
        unsubscribe = backend.subscribe("chan1", received.append)
        try:
            backend.publish("chan1", "hello")
            _wait_for(lambda: received == ["hello"])
        finally:
            unsubscribe()

    def test_unsubscribe_stops_delivery(self, backend):
        received: list[str] = []
        unsubscribe = backend.subscribe("chan2", received.append)
        backend.publish("chan2", "one")
        _wait_for(lambda: received == ["one"])
        unsubscribe()
        backend.publish("chan2", "two")
        time.sleep(0.3)
        assert received == ["one"]

    def test_publish_with_no_subscriber_is_a_noop(self, backend):
        backend.publish("nobody-home", "x")  # must not raise

    def test_multiple_subscribers_all_receive(self, backend):
        received_a: list[str] = []
        received_b: list[str] = []
        unsub_a = backend.subscribe("chan3", received_a.append)
        unsub_b = backend.subscribe("chan3", received_b.append)
        try:
            backend.publish("chan3", "broadcast")
            _wait_for(lambda: received_a == ["broadcast"] and received_b == ["broadcast"])
        finally:
            unsub_a()
            unsub_b()


class TestPingAndReset:
    def test_ping_true_when_healthy(self, backend):
        assert backend.ping() is True

    def test_flush_leaves_backend_functional(self, backend):
        """FLUSHALL-equivalent (fakeredis: real FLUSHALL; memory: clear the
        dict directly, there being no server to flush) must leave the
        backend usable afterward — not wedged."""
        backend.kv_set("pre-flush", "v", ttl_s=60)
        if isinstance(backend, RedisCoordinationBackend):
            backend._client.flushall()
        else:
            backend._kv.clear()
            backend._leases.clear()

        assert backend.kv_get("pre-flush") is None
        backend.kv_set("post-flush", "v2", ttl_s=60)
        assert backend.kv_get("post-flush") == "v2"
        assert backend.lease_acquire("post-flush-lease", "holder", ttl_s=60) is True
        assert backend.ping() is True


class TestRedisUnavailable:
    """Connection-failure behavior has no memory-backend equivalent — these
    run only against a Redis client pointed at a closed port."""

    def _unreachable_backend(self) -> RedisCoordinationBackend:
        import redis as redis_lib

        client = redis_lib.Redis(
            host="127.0.0.1",
            port=1,  # nothing listens here — connection refused, fast
            socket_connect_timeout=0.3,
            socket_timeout=0.3,
            decode_responses=True,
        )
        return RedisCoordinationBackend(client)

    def test_ping_returns_false_not_raise(self):
        backend = self._unreachable_backend()
        assert backend.ping() is False

    def test_kv_set_raises_coordination_unavailable(self):
        backend = self._unreachable_backend()
        with pytest.raises(CoordinationUnavailable):
            backend.kv_set("k", "v", ttl_s=60)

    def test_lease_acquire_raises_coordination_unavailable(self):
        backend = self._unreachable_backend()
        with pytest.raises(CoordinationUnavailable):
            backend.lease_acquire("l", "holder", ttl_s=60)

    def test_publish_raises_coordination_unavailable(self):
        backend = self._unreachable_backend()
        with pytest.raises(CoordinationUnavailable):
            backend.publish("chan", "x")


class TestRedisSubscribeFailureLeavesNoState:
    """A failed Redis-level SUBSCRIBE must not poison local subscriber
    state — see the ``subscribe()`` docstring/comment in
    ``app.coordination.redis_backend``. Before the fix, the handler was
    registered into ``self._subscribers[channel]`` before the real
    ``self._pubsub.subscribe(channel)`` call; if that raised, the local
    entry stayed behind, so every future ``subscribe()`` call for that
    channel saw ``is_new_channel=False`` and the Redis-level SUBSCRIBE was
    never retried — the channel was silently broken forever."""

    def test_transient_failure_then_retry_succeeds(self, monkeypatch):
        client = fakeredis.FakeStrictRedis(decode_responses=True)
        redis_backend = RedisCoordinationBackend(client)
        try:
            redis_backend._ensure_listener()
            real_subscribe = redis_backend._pubsub.subscribe
            calls = {"count": 0}

            def _flaky_subscribe(channel):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise ConnectionError("transient network error")
                return real_subscribe(channel)

            monkeypatch.setattr(redis_backend._pubsub, "subscribe", _flaky_subscribe)

            received: list[str] = []
            with pytest.raises(CoordinationUnavailable):
                redis_backend.subscribe("chan-flaky", received.append)

            # The failed subscribe must leave NO local state behind — no
            # channel entry, no handler, no unsubscribe callable was even
            # returned to the caller.
            assert "chan-flaky" not in redis_backend._subscribers

            # A second subscribe() call must retry the Redis-level
            # SUBSCRIBE (not believe the channel already live) and succeed,
            # with messages actually delivered — the channel is not
            # poisoned.
            unsubscribe = redis_backend.subscribe("chan-flaky", received.append)
            try:
                assert calls["count"] == 2
                redis_backend.publish("chan-flaky", "hello")
                _wait_for(lambda: received == ["hello"])
            finally:
                unsubscribe()
        finally:
            redis_backend.close()
