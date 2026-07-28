"""Wiring tests for wave-2C task 4: the auth-endpoint slowapi ``Limiter``'s
storage backend follows the coordination backend selection.

Contract: with ``coordination.backend=redis``, the ``Limiter`` must be
constructed with ``storage_uri`` pointed at the resolved Redis URL, so
buckets are shared across every app process (see the module docstring in
``app/auth/rate_limit.py``). With the default ``memory`` backend, the
``Limiter`` keeps slowapi's own in-memory storage — unchanged construction.

This is a wiring-level test only (asserting ``limiter._storage_uri`` /
``limiter._storage`` after construction) — no real or fake Redis server is
involved, since slowapi/``limits`` builds its ``RedisStorage`` lazily and
doesn't connect at construction time. Enforcement against a live Redis is
covered by the m-tier smoke test, not this suite.
"""

from __future__ import annotations

import pytest

from app.auth.rate_limit import _build_limiter


@pytest.fixture(autouse=True)
def _clean_coordination_env(monkeypatch):
    for var in ("AGNES_COORDINATION_BACKEND", "AGNES_REDIS_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("app.instance_config.get_value", lambda *a, **k: None)
    yield


def test_memory_backend_builds_limiter_without_storage_uri():
    limiter = _build_limiter()
    # No storage_uri kwarg passed at all — slowapi/limits falls back to its
    # own default in-memory storage, identical to construction before this
    # task (unchanged behavior for the default coordination backend).
    assert limiter._storage_uri is None


def test_redis_backend_env_wires_storage_uri_to_resolved_redis_url(monkeypatch):
    monkeypatch.setenv("AGNES_COORDINATION_BACKEND", "redis")
    monkeypatch.setenv("AGNES_REDIS_URL", "redis://example-redis-host:6380/2")
    limiter = _build_limiter()
    assert limiter._storage_uri == "redis://example-redis-host:6380/2"


def test_redis_backend_yaml_wires_storage_uri(monkeypatch):
    monkeypatch.setattr(
        "app.instance_config.get_value",
        lambda *keys, default=None: (
            "redis"
            if keys == ("coordination", "backend")
            else ("redis://yaml-redis:6379/0" if keys == ("redis", "url") else default)
        ),
    )
    limiter = _build_limiter()
    assert limiter._storage_uri == "redis://yaml-redis:6379/0"


def test_default_redis_url_used_when_backend_is_redis_but_no_url_configured(monkeypatch):
    monkeypatch.setenv("AGNES_COORDINATION_BACKEND", "redis")
    limiter = _build_limiter()
    assert limiter._storage_uri == "redis://localhost:6379/0"
