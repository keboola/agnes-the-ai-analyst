"""Wave 2C Task 5: cache-invalidation pub/sub + operational TTL codes.

Three areas covered:

1. ``app.api.v2_catalog.invalidate_for_table`` / ``invalidate_all`` publish a
   ``cache-invalidate`` event via the coordination backend (memory backend,
   synchronous delivery — see app.coordination.memory).
2. ``app.main._on_cache_invalidate`` (the lifespan subscriber) drops the
   local TTL caches for an incoming event, and does NOT re-publish (no echo
   loop back onto the channel).
3. CLI-auth codes (app.api.cli_auth) and Slack binding codes
   (services.slack_bot.binding) round-trip through the coordination KV when
   ``coordination.backend=redis`` (via fakeredis), while memory mode keeps
   using the DuckDB paths untouched (regression, covered by the existing
   tests in tests/test_cli_auth_server.py + tests/test_binding_hardening.py
   + tests/test_binding_scope.py, which are NOT modified by this change).
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import timedelta

import pytest

fakeredis = pytest.importorskip("fakeredis")


# ---------------------------------------------------------------------------
# 1 + 2: cache-invalidate pub/sub
# ---------------------------------------------------------------------------


def test_invalidate_for_table_publishes_event():
    from app.coordination.factory import coordination, reset_coordination_for_tests

    reset_coordination_for_tests()
    received = []
    unsub = coordination().subscribe("cache-invalidate", received.append)
    try:
        from app.api import v2_catalog

        v2_catalog.invalidate_for_table("orders")
    finally:
        unsub()
        reset_coordination_for_tests()

    assert len(received) == 1
    payload = json.loads(received[0])
    assert payload == {"scope": "table", "table": "orders"}


def test_invalidate_all_publishes_event():
    from app.coordination.factory import coordination, reset_coordination_for_tests

    reset_coordination_for_tests()
    received = []
    unsub = coordination().subscribe("cache-invalidate", received.append)
    try:
        from app.api import v2_catalog

        v2_catalog.invalidate_all()
    finally:
        unsub()
        reset_coordination_for_tests()

    assert len(received) == 1
    payload = json.loads(received[0])
    assert payload == {"scope": "all", "table": None}


def test_on_cache_invalidate_drops_local_caches_for_table_scope():
    from app.api import v2_catalog, v2_sample, v2_schema
    from app.main import _on_cache_invalidate

    v2_catalog._table_rows_cache.set("all", ["fake_row"])
    v2_schema._schema_cache.set("orders", {"columns": []})
    v2_sample._sample_cache.set("orders|10", [{"row": 1}])

    _on_cache_invalidate(json.dumps({"scope": "table", "table": "orders"}))

    assert v2_catalog._table_rows_cache.get("all") is None
    assert v2_schema._schema_cache.get("orders") is None
    assert v2_sample._sample_cache.get("orders|10") is None


def test_on_cache_invalidate_drops_local_caches_for_all_scope():
    from app.api import v2_catalog, v2_sample, v2_schema
    from app.main import _on_cache_invalidate

    v2_catalog._table_rows_cache.set("all", ["fake_row"])
    v2_schema._schema_cache.set("t1", {"columns": []})
    v2_sample._sample_cache.set("t1|10", [{"row": 1}])

    _on_cache_invalidate(json.dumps({"scope": "all"}))

    assert v2_catalog._table_rows_cache.get("all") is None
    assert v2_schema._schema_cache.get("t1") is None
    assert v2_sample._sample_cache.get("t1|10") is None


def test_on_cache_invalidate_ignores_garbage_message():
    """An unparseable / unknown-scope message must never raise — it's a
    best-effort cross-process hint, not a request the caller waits on."""
    from app.main import _on_cache_invalidate

    _on_cache_invalidate("not json")
    _on_cache_invalidate(json.dumps({"scope": "bogus"}))
    _on_cache_invalidate(json.dumps({"scope": "table"}))  # missing "table"


def test_invalidate_for_table_no_echo_loop(monkeypatch):
    """Simulates this process being subscribed to its own publish (true for
    the memory backend, and possible under redis too). The subscriber
    handler must call invalidate_for_table(_publish=False) — i.e. publish
    fires exactly once per top-level call, never recursively."""
    from app.api import v2_catalog
    from app.coordination.factory import coordination, reset_coordination_for_tests
    from app.main import _on_cache_invalidate

    reset_coordination_for_tests()
    unsub = coordination().subscribe("cache-invalidate", _on_cache_invalidate)

    calls = []
    orig = v2_catalog._publish_cache_invalidate

    def _spy(**kwargs):
        calls.append(kwargs)
        orig(**kwargs)

    monkeypatch.setattr(v2_catalog, "_publish_cache_invalidate", _spy)

    try:
        v2_catalog.invalidate_for_table("t2")
    finally:
        unsub()
        reset_coordination_for_tests()

    assert calls == [{"scope": "table", "table": "t2"}]


def test_invalidate_all_no_echo_loop(monkeypatch):
    from app.api import v2_catalog
    from app.coordination.factory import coordination, reset_coordination_for_tests
    from app.main import _on_cache_invalidate

    reset_coordination_for_tests()
    unsub = coordination().subscribe("cache-invalidate", _on_cache_invalidate)

    calls = []
    orig = v2_catalog._publish_cache_invalidate

    def _spy(**kwargs):
        calls.append(kwargs)
        orig(**kwargs)

    monkeypatch.setattr(v2_catalog, "_publish_cache_invalidate", _spy)

    try:
        v2_catalog.invalidate_all()
    finally:
        unsub()
        reset_coordination_for_tests()

    assert calls == [{"scope": "all", "table": None}]


# ---------------------------------------------------------------------------
# Shared redis-coordination fixture (fakeredis, no real Redis needed)
# ---------------------------------------------------------------------------


@pytest.fixture
def redis_coordination(monkeypatch):
    """Force the coordination() singleton to a fakeredis-backed
    RedisCoordinationBackend, mirroring how an operator would configure
    ``coordination.backend: redis`` — but without a real Redis server."""
    from app.coordination import factory as coordination_factory
    from app.coordination.redis_backend import RedisCoordinationBackend

    monkeypatch.setenv("AGNES_COORDINATION_BACKEND", "redis")
    coordination_factory.reset_coordination_for_tests()
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    backend = RedisCoordinationBackend(client)
    monkeypatch.setattr(coordination_factory, "_instance", backend)
    yield backend
    coordination_factory.reset_coordination_for_tests()


# ---------------------------------------------------------------------------
# 3a: CLI-auth codes via coordination KV (redis mode)
# ---------------------------------------------------------------------------


def test_cli_auth_code_round_trip_via_kv_redis(redis_coordination):
    from app.api.cli_auth import _consume_cli_auth_code, _create_cli_auth_code

    _create_cli_auth_code(None, "hash1", "user-1", "user1@example.com")
    claimed = _consume_cli_auth_code(None, "hash1")
    assert claimed == {"user_id": "user-1", "email": "user1@example.com"}


def test_cli_auth_code_single_use_via_kv_redis(redis_coordination):
    from app.api.cli_auth import _consume_cli_auth_code, _create_cli_auth_code

    _create_cli_auth_code(None, "hash2", "user-2", "user2@example.com")
    first = _consume_cli_auth_code(None, "hash2")
    second = _consume_cli_auth_code(None, "hash2")
    assert first is not None
    assert second is None


def test_cli_auth_code_unknown_returns_none_via_kv_redis(redis_coordination):
    from app.api.cli_auth import _consume_cli_auth_code

    assert _consume_cli_auth_code(None, "never-existed") is None


def test_cli_auth_code_ttl_expiry_via_kv_redis(redis_coordination, monkeypatch):
    from app.api import cli_auth

    monkeypatch.setattr(cli_auth, "_CODE_TTL", timedelta(seconds=1))
    cli_auth._create_cli_auth_code(None, "hash4", "user-4", "user4@example.com")
    time.sleep(1.3)
    assert cli_auth._consume_cli_auth_code(None, "hash4") is None


def test_cli_auth_redis_mode_never_touches_duckdb_conn(redis_coordination):
    """Regression guard for 'redis mode simply stops touching the file': pass
    a bare DuckDB connection that does NOT have the cli_auth_codes table —
    if the redis-mode code path fell through to the DuckDB repo it would
    raise a catalog error; it must not."""
    import duckdb

    from app.api.cli_auth import _consume_cli_auth_code, _create_cli_auth_code

    conn = duckdb.connect(":memory:")  # no cli_auth_codes table created
    _create_cli_auth_code(conn, "hash5", "user-5", "user5@example.com")
    claimed = _consume_cli_auth_code(conn, "hash5")
    assert claimed == {"user_id": "user-5", "email": "user5@example.com"}


def test_cli_auth_http_round_trip_via_kv_redis(seeded_app, redis_coordination):
    """Full HTTP flow (POST /cli/auth/start -> POST /cli/auth/exchange)
    under coordination.backend=redis — the code never touches DuckDB."""
    from urllib.parse import parse_qs, urlparse

    client = seeded_app["client"]
    token = seeded_app["admin_token"]

    r = client.post(
        "/cli/auth/start",
        data={"port": 54322, "state": "s" * 24},
        headers={"Authorization": f"Bearer {token}"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    q = parse_qs(urlparse(r.headers["location"]).query)
    code = q["code"][0]

    first = client.post("/cli/auth/exchange", json={"code": code})
    assert first.status_code == 200, first.text

    second = client.post("/cli/auth/exchange", json={"code": code})
    assert second.status_code == 400


# ---------------------------------------------------------------------------
# 3b: Slack binding codes via coordination KV (redis mode)
# ---------------------------------------------------------------------------


def test_slack_binding_code_round_trip_via_kv_redis(redis_coordination):
    import duckdb

    from services.slack_bot.binding import issue_verification_code, redeem_verification_code

    conn = duckdb.connect(":memory:")
    code = issue_verification_code(conn, slack_user_id="U_REDIS_1")
    ok = redeem_verification_code(conn, user_email="nobody@example.com", code=code)
    assert ok is True


def test_slack_binding_code_single_use_via_kv_redis(redis_coordination):
    import duckdb

    from services.slack_bot.binding import issue_verification_code, redeem_verification_code

    conn = duckdb.connect(":memory:")
    code = issue_verification_code(conn, slack_user_id="U_REDIS_2")
    first = redeem_verification_code(conn, user_email="a@example.com", code=code)
    second = redeem_verification_code(conn, user_email="a@example.com", code=code)
    assert first is True
    assert second is False


def test_slack_binding_reissue_invalidates_prior_code_via_kv_redis(redis_coordination):
    """SR-12 'one active code per user' invariant, preserved on the redis
    path: re-issuing for the same slack_user_id must invalidate the old
    code immediately, not just let it expire."""
    import duckdb

    from services.slack_bot.binding import issue_verification_code, redeem_verification_code

    conn = duckdb.connect(":memory:")
    old_code = issue_verification_code(conn, slack_user_id="U_REDIS_3")
    new_code = issue_verification_code(conn, slack_user_id="U_REDIS_3")
    assert old_code != new_code

    assert redeem_verification_code(conn, user_email="b@example.com", code=old_code) is False
    assert redeem_verification_code(conn, user_email="b@example.com", code=new_code) is True


def test_slack_binding_wrong_code_via_kv_redis(redis_coordination):
    import duckdb

    from services.slack_bot.binding import redeem_verification_code

    conn = duckdb.connect(":memory:")
    assert redeem_verification_code(conn, user_email="c@example.com", code="000000") is False


def test_slack_binding_ttl_expiry_via_kv_redis(redis_coordination, monkeypatch):
    import duckdb

    import services.slack_bot.binding as binding_mod

    monkeypatch.setattr(binding_mod, "_CODE_TTL_SECONDS", 1)
    conn = duckdb.connect(":memory:")
    code = binding_mod.issue_verification_code(conn, slack_user_id="U_REDIS_4")
    time.sleep(1.3)
    assert binding_mod.redeem_verification_code(conn, user_email="d@example.com", code=code) is False


def test_slack_binding_redis_mode_still_uses_duckdb_for_throttle_logs(redis_coordination):
    """The issuance/redeem throttle logs are durable control-plane
    bookkeeping and stay on DuckDB even under coordination.backend=redis —
    only the ephemeral code itself moves to the KV."""
    import duckdb

    from services.slack_bot.binding import issue_verification_code

    conn = duckdb.connect(":memory:")
    issue_verification_code(conn, slack_user_id="U_REDIS_5")
    count = conn.execute("SELECT count(*) FROM slack_binding_issue_log WHERE slack_user_id = 'U_REDIS_5'").fetchone()[0]
    assert count == 1
    # And the code table itself stays EMPTY — nothing was written there.
    code_rows = conn.execute("SELECT count(*) FROM slack_binding_codes").fetchone()[0]
    assert code_rows == 0


# ---------------------------------------------------------------------------
# Regression: memory backend (default) keeps using DuckDB, unaffected by any
# of the above. The existing suites (tests/test_cli_auth_server.py,
# tests/test_binding_hardening.py, tests/test_binding_scope.py,
# tests/db_pg/test_parity_slack_binding.py) already assert this in detail and
# are intentionally left unmodified; these two are a quick smoke check that
# this file's own fixtures don't leak coordination-backend state into them.
# ---------------------------------------------------------------------------


def test_cli_auth_memory_backend_still_uses_duckdb_table():
    from app.coordination.factory import resolve_backend_name

    assert resolve_backend_name() == "memory"

    import duckdb

    from src.repositories.cli_auth_codes import CliAuthCodeRepository

    conn = duckdb.connect(":memory:")
    conn.execute(
        "CREATE TABLE cli_auth_codes (code_hash VARCHAR, user_id VARCHAR, email VARCHAR, "
        "created_at TIMESTAMP, expires_at TIMESTAMP, consumed_at TIMESTAMP)"
    )

    from app.api.cli_auth import _consume_cli_auth_code, _create_cli_auth_code

    _create_cli_auth_code(conn, "h", "u", "u@example.com")
    row = conn.execute("SELECT code_hash FROM cli_auth_codes").fetchone()
    assert row is not None and row[0] == "h"
    claimed = _consume_cli_auth_code(conn, "h")
    assert claimed == {"user_id": "u", "email": "u@example.com"}
    # keep flake8/vulture happy about the unused import path exercised above
    assert CliAuthCodeRepository is not None
    assert hashlib.sha256(b"x").hexdigest()  # sanity: hashlib import used


def test_slack_binding_memory_backend_still_uses_duckdb_table():
    from app.coordination.factory import resolve_backend_name

    assert resolve_backend_name() == "memory"

    import duckdb

    from services.slack_bot.binding import issue_verification_code

    conn = duckdb.connect(":memory:")
    code = issue_verification_code(conn, slack_user_id="U_MEM")
    row = conn.execute("SELECT slack_user_id FROM slack_binding_codes WHERE code = ?", [code]).fetchone()
    assert row is not None and row[0] == "U_MEM"
