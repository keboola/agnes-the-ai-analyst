"""`GET /api/health` is the auth-free liveness probe hit by the LB, the
docker-compose healthcheck, and the on-VM watchdog (every few seconds /
minutes). It must:

1. Keep its body contract — `status` + `db_schema` + `current` — because the
   watchdog parses the schema number straight out of this body to emit the
   "DB: schema bump" info event (no extra DB access on the VM, #647), and the
   docker smoke test asserts `db_schema == "ok"`.

2. Never block the event loop on the DuckDB read. The handler is `async def`;
   doing the schema `SELECT` synchronously serializes every probe behind any
   in-flight orchestrator rebuild (which writes `sync_state` on the same
   system connection). Under that contention the probe times out and the
   watchdog fires a false `HEALTH: /api/health not returning 200`. The read
   is offloaded to a worker thread and memoized so repeated probes don't
   re-hit the DB (the schema only changes at startup migration).
"""

from __future__ import annotations

import asyncio
import time

import httpx

import app.api.health as health_mod


def _reset_cache() -> None:
    health_mod._schema_cache = None
    health_mod._schema_cache_at = 0.0


def test_health_body_contract(seeded_app):
    """Liveness body keeps the fields the watchdog + docker smoke depend on."""
    _reset_cache()
    r = seeded_app["client"].get("/api/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok", body
    assert body["db_schema"] == "ok", body
    assert "current" in body, body
    assert "vault_key_configured" in body, body


def test_schema_check_memoized(seeded_app, monkeypatch):
    """Repeated probes must not re-query the DB — the schema only changes at
    startup migration. Two sequential probes => one underlying read."""
    _reset_cache()
    calls = {"n": 0}
    orig = health_mod._check_db_schema

    def counting():
        calls["n"] += 1
        return orig()

    monkeypatch.setattr(health_mod, "_check_db_schema", counting)
    c = seeded_app["client"]
    assert c.get("/api/health").status_code == 200
    assert c.get("/api/health").status_code == 200
    assert calls["n"] == 1, f"schema read not memoized: {calls['n']} DB hits"


def test_unreachable_result_not_cached(seeded_app, monkeypatch):
    """A transient `unreachable` (DB momentarily busy) must not get pinned in
    the cache — the next probe retries and recovers to `ok`."""
    _reset_cache()
    state = {"first": True}

    def flaky():
        if state["first"]:
            state["first"] = False
            return {"db_schema": "unreachable", "detail": "busy"}
        return {"db_schema": "ok", "current": 1, "expected": 1}

    monkeypatch.setattr(health_mod, "_check_db_schema", flaky)
    c = seeded_app["client"]
    r1 = c.get("/api/health")
    assert r1.json()["db_schema"] == "unreachable", r1.text
    r2 = c.get("/api/health")
    assert r2.json()["db_schema"] == "ok", r2.text


def test_health_does_not_block_event_loop(seeded_app, monkeypatch):
    """Two concurrent probes against a slow schema read must overlap, proving
    the synchronous DuckDB call runs off the event loop. If it blocked the
    loop the calls would serialize to ~2x the single-call latency."""
    _reset_cache()
    app = seeded_app["client"].app
    orig = health_mod._check_db_schema

    def slow():
        time.sleep(0.5)  # simulate rebuild-lock contention on the system conn
        return orig()

    monkeypatch.setattr(health_mod, "_check_db_schema", slow)

    async def fire():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
            t0 = time.monotonic()
            r1, r2 = await asyncio.gather(ac.get("/api/health"), ac.get("/api/health"))
            return time.monotonic() - t0, r1, r2

    elapsed, r1, r2 = asyncio.run(fire())
    assert r1.status_code == 200 and r2.status_code == 200
    assert elapsed < 0.9, f"probes serialized ({elapsed:.2f}s) — event loop blocked"


def test_detailed_schema_check_does_not_block_event_loop(seeded_app, monkeypatch):
    """`/api/health/detailed?include=schema` must not do its synchronous PG
    round-trip on the event loop. Two concurrent authenticated probes against a
    slow schema read must overlap, proving the read runs off the loop (via
    `asyncio.to_thread`). If it blocked, they'd serialize to ~2x latency."""
    _reset_cache()
    app = seeded_app["client"].app
    token = seeded_app["admin_token"]
    orig = health_mod._check_db_schema

    def slow():
        time.sleep(0.5)
        return orig()

    monkeypatch.setattr(health_mod, "_check_db_schema", slow)

    async def fire():
        transport = httpx.ASGITransport(app=app)
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
            t0 = time.monotonic()
            r1, r2 = await asyncio.gather(
                ac.get("/api/health/detailed?include=schema", headers=headers),
                ac.get("/api/health/detailed?include=schema", headers=headers),
            )
            return time.monotonic() - t0, r1, r2

    elapsed, r1, r2 = asyncio.run(fire())
    assert r1.status_code == 200 and r2.status_code == 200
    assert elapsed < 0.9, f"detailed probes serialized ({elapsed:.2f}s) — event loop blocked"
