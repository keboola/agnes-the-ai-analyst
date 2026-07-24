"""Worker low-latency wakeup (three-plane §3.3): idle lane slots wake on a
NOTIFY-driven signal instead of always waiting out the poll interval, with a
hard poll-only fallback so it can never make things worse.

No pytest-asyncio in this repo — async bodies run via ``asyncio.run`` inside
sync test functions (mirrors tests/test_worker_runtime.py)."""

from __future__ import annotations

import asyncio

import pytest

from app.worker import wakeup


def test_idle_wait_returns_early_on_signal():
    async def _body():
        wakeup._wake = asyncio.Event()

        async def _fire():
            await asyncio.sleep(0.02)
            wakeup.signal()

        fire = asyncio.create_task(_fire())
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        await wakeup.idle_wait(5.0)  # long interval; signal must cut it short
        elapsed = loop.time() - t0
        await fire
        return elapsed

    elapsed = asyncio.run(_body())
    assert elapsed < 1.0, f"signal did not wake idle_wait early (took {elapsed:.2f}s)"


def test_idle_wait_times_out_without_signal():
    async def _body():
        wakeup._wake = asyncio.Event()
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        await wakeup.idle_wait(0.05)
        return loop.time() - t0

    assert asyncio.run(_body()) >= 0.045


def test_idle_wait_clears_event_between_waits():
    async def _body():
        wakeup._wake = asyncio.Event()
        wakeup.signal()
        await wakeup.idle_wait(5.0)  # consumes the signal quickly
        # Event was cleared; a second wait with no new signal must time out.
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        await wakeup.idle_wait(0.05)
        return loop.time() - t0

    assert asyncio.run(_body()) >= 0.045


def test_notify_listener_noop_off_postgres(monkeypatch):
    # DuckDB backend: the listener must return immediately (no psycopg, no
    # connect attempt) so the worker stays cleanly poll-only.
    monkeypatch.setattr("src.repositories.use_pg", lambda: False)
    asyncio.run(asyncio.wait_for(wakeup.notify_listener(), timeout=1.0))


def test_notify_listener_backs_off_on_graceful_stream_end(monkeypatch):
    """The connect-failure and LISTEN-loop-exception paths both back off
    before reconnecting; ``notifies()`` ending WITHOUT raising (the server
    closed the connection cleanly) must back off the same way instead of
    reconnecting in a tight loop."""
    import sys
    import types

    real_sleep = asyncio.sleep

    # Each fake method does a real ``await`` checkpoint (not just an
    # immediately-returning coroutine): a fully synchronous fake would let a
    # regressed while-True loop spin without ever yielding to the event
    # loop, starving the ``wait_for`` deadline below of a chance to fire —
    # turning "the fix is missing" into a hang instead of a fast failure.
    class _FakeConn:
        async def execute(self, *a, **kw):
            await real_sleep(0)

        async def notifies(self):
            await real_sleep(0)
            return
            yield  # pragma: no cover - makes this an async generator

        async def close(self):
            await real_sleep(0)

    class _FakeAsyncConnection:
        @staticmethod
        async def connect(*a, **kw):
            await real_sleep(0)
            return _FakeConn()

    fake_psycopg = types.ModuleType("psycopg")
    fake_psycopg.AsyncConnection = _FakeAsyncConnection
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)

    monkeypatch.setattr("src.repositories.use_pg", lambda: True)

    class _FakeURL:
        def set(self, **kw):
            return self

        def render_as_string(self, **kw):
            return "postgresql://fake"

    class _FakeEngine:
        url = _FakeURL()

    fake_db_pg = types.ModuleType("src.db_pg")
    fake_db_pg.get_engine = lambda: _FakeEngine()
    monkeypatch.setitem(sys.modules, "src.db_pg", fake_db_pg)

    sleeps: list[float] = []

    async def _fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 1:
            raise asyncio.CancelledError()
        await real_sleep(0)

    monkeypatch.setattr(wakeup.asyncio, "sleep", _fake_sleep)

    # The wait_for bound is what turns "fix regressed" into a fast failure
    # instead of a hang: a regression that drops the backoff never calls the
    # mocked sleep, so nothing else here stops the loop.
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(asyncio.wait_for(wakeup.notify_listener(), timeout=2.0))

    assert sleeps == [wakeup._RECONNECT_BACKOFF_S]
