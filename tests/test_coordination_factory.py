"""Tests for the coordination() singleton factory's backend selection.

Contract behavior (TTL kv, leases, pub/sub, ...) is covered by
``tests/test_coordination_contract.py``; this file is only about wiring —
which backend class gets built, and the env-overrides-yaml resolution for
``coordination.backend`` / ``redis.url``.
"""

from __future__ import annotations

import pytest

from app.coordination.factory import coordination, reset_coordination_for_tests
from app.coordination.memory import MemoryCoordinationBackend
from app.coordination.redis_backend import RedisCoordinationBackend


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in ("AGNES_COORDINATION_BACKEND", "AGNES_REDIS_URL"):
        monkeypatch.delenv(var, raising=False)
    reset_coordination_for_tests()
    yield
    reset_coordination_for_tests()


def test_default_backend_is_memory(monkeypatch):
    monkeypatch.setattr("app.instance_config.get_value", lambda *a, **k: None)
    backend = coordination()
    assert isinstance(backend, MemoryCoordinationBackend)


def test_yaml_selects_redis(monkeypatch):
    monkeypatch.setattr(
        "app.instance_config.get_value",
        lambda *keys, default=None: "redis" if keys == ("coordination", "backend") else default,
    )
    backend = coordination()
    assert isinstance(backend, RedisCoordinationBackend)


def test_env_override_wins_over_yaml(monkeypatch):
    monkeypatch.setattr(
        "app.instance_config.get_value",
        lambda *keys, default=None: "memory" if keys == ("coordination", "backend") else default,
    )
    monkeypatch.setenv("AGNES_COORDINATION_BACKEND", "redis")
    backend = coordination()
    assert isinstance(backend, RedisCoordinationBackend)


def test_singleton_returns_same_instance(monkeypatch):
    monkeypatch.setattr("app.instance_config.get_value", lambda *a, **k: None)
    first = coordination()
    second = coordination()
    assert first is second


def test_reset_builds_a_fresh_instance(monkeypatch):
    monkeypatch.setattr("app.instance_config.get_value", lambda *a, **k: None)
    first = coordination()
    reset_coordination_for_tests()
    second = coordination()
    assert first is not second


def test_redis_url_env_override(monkeypatch):
    monkeypatch.setattr("app.instance_config.get_value", lambda *a, **k: "memory")
    monkeypatch.setenv("AGNES_COORDINATION_BACKEND", "redis")
    monkeypatch.setenv("AGNES_REDIS_URL", "redis://example-redis-host:6380/2")
    backend = coordination()
    assert isinstance(backend, RedisCoordinationBackend)
    # redis-py resolves connection_pool kwargs from the URL lazily; assert
    # against the pool's connection_kwargs rather than the client itself.
    kwargs = backend._client.connection_pool.connection_kwargs
    assert kwargs["host"] == "example-redis-host"
    assert kwargs["port"] == 6380
    assert kwargs["db"] == 2
