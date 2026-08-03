"""God-mode observability — ``can_access``'s Admin short-circuit logs a
deduplicated ``god_mode_bypass`` line when it grants a resource the admin
holds no explicit group grant for.

Observability only: the access decision itself must be unchanged (that is
pinned by the existing TestAdminBypass suite in test_access_control.py).
"""

import logging

import pytest

from app.auth import access
from src.db import get_system_db


@pytest.fixture(autouse=True)
def _reset_dedup():
    access._god_mode_logged.clear()
    access._god_mode_grants.clear()
    yield
    access._god_mode_logged.clear()
    access._god_mode_grants.clear()


@pytest.fixture
def system_conn(seeded_app):
    conn = get_system_db()
    try:
        yield conn
    finally:
        conn.close()


def _bypass_lines(caplog):
    return [r for r in caplog.records if "god_mode_bypass" in r.getMessage()]


def test_bypass_logged_for_ungranted_resource(system_conn, caplog):
    with caplog.at_level(logging.INFO, logger="app.auth.access"):
        assert access.can_access("admin1", "table", "keboola.ungranted", conn=system_conn)
    lines = _bypass_lines(caplog)
    assert len(lines) == 1
    msg = lines[0].getMessage()
    assert "admin1" in msg and "table" in msg and "keboola.ungranted" in msg


def test_repeat_hit_is_deduplicated(system_conn, caplog):
    with caplog.at_level(logging.INFO, logger="app.auth.access"):
        assert access.can_access("admin1", "table", "keboola.dedup", conn=system_conn)
        assert access.can_access("admin1", "table", "keboola.dedup", conn=system_conn)
    assert len(_bypass_lines(caplog)) == 1


def test_distinct_resources_each_logged(system_conn, caplog):
    with caplog.at_level(logging.INFO, logger="app.auth.access"):
        access.can_access("admin1", "table", "keboola.a", conn=system_conn)
        access.can_access("admin1", "table", "keboola.b", conn=system_conn)
    assert len(_bypass_lines(caplog)) == 2


def test_no_log_when_admin_has_explicit_grant(system_conn, caplog):
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    group = UserGroupsRepository(system_conn).ensure("god-mode-audit-grantees")
    UserGroupMembersRepository(system_conn).add_member("admin1", group["id"], source="admin")
    ResourceGrantsRepository(system_conn).ensure_grant(group["id"], "table", "keboola.granted")

    with caplog.at_level(logging.INFO, logger="app.auth.access"):
        assert access.can_access("admin1", "table", "keboola.granted", conn=system_conn)
    assert _bypass_lines(caplog) == []


def test_non_admin_never_logs(system_conn, caplog):
    with caplog.at_level(logging.INFO, logger="app.auth.access"):
        assert not access.can_access("analyst1", "table", "keboola.ungranted", conn=system_conn)
    assert _bypass_lines(caplog) == []


def test_lookup_failure_does_not_break_authorization(system_conn, caplog, monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("grant lookup down")

    monkeypatch.setattr(access, "_allowed_ids_for_user", _boom)
    with caplog.at_level(logging.INFO, logger="app.auth.access"):
        assert access.can_access("admin1", "table", "keboola.x", conn=system_conn)
    # a warning is fine; no bypass INFO line, and the decision still passed
    assert all(r.levelno != logging.INFO for r in _bypass_lines(caplog))


def test_lookup_failure_is_retried_not_cached_as_logged(system_conn, caplog, monkeypatch):
    """A transient grant-lookup failure must NOT mark the key as seen — the
    next request retries instead of the cooldown swallowing the audit line
    for the whole window (review finding on #1143)."""
    calls = {"n": 0}
    real = access._allowed_ids_for_user

    def _flaky(user_id, resource_type, conn=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient blip")
        return real(user_id, resource_type, conn=conn)

    monkeypatch.setattr(access, "_allowed_ids_for_user", _flaky)
    # first call: lookup raises → decision still True, nothing logged, key NOT cached
    with caplog.at_level(logging.INFO, logger="app.auth.access"):
        assert access.can_access("admin1", "table", "keboola.retry", conn=system_conn)
    assert not [r for r in caplog.records if "god_mode_bypass: admin" in r.getMessage()]
    # second call: retried (key wasn't cooldown-suppressed) → now it logs
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.auth.access"):
        assert access.can_access("admin1", "table", "keboola.retry", conn=system_conn)
    assert len(_bypass_lines(caplog)) == 1


def test_grant_lookup_memoized_per_user_type(system_conn, monkeypatch):
    """N distinct resource_ids in a burst pay ONE grant query, not N
    (review finding on #1143 — no per-item DB round trip)."""
    calls = {"n": 0}
    real = access._allowed_ids_for_user

    def _counting(user_id, resource_type, conn=None):
        calls["n"] += 1
        return real(user_id, resource_type, conn=conn)

    monkeypatch.setattr(access, "_allowed_ids_for_user", _counting)
    for i in range(10):
        assert access.can_access("admin1", "table", f"keboola.t{i}", conn=system_conn)
    # 10 distinct ids, same (user, type) within the TTL → one underlying query
    assert calls["n"] == 1


def test_cache_failure_does_not_break_authorization(system_conn, monkeypatch):
    """The whole observability body is guarded — a dedup-cache race (e.g.
    RuntimeError from concurrent mutation during the eviction sweep, this
    runs on FastAPI's thread pool) must degrade to a lost log line, never
    to an exception out of can_access."""

    class _ExplodingCache(dict):
        def get(self, *a, **kw):
            raise RuntimeError("dictionary changed size during iteration")

    monkeypatch.setattr(access, "_god_mode_logged", _ExplodingCache())
    assert access.can_access("admin1", "table", "keboola.race", conn=system_conn)
