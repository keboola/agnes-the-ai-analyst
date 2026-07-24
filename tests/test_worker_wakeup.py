"""Worker low-latency wakeup (three-plane §3.3): idle lane slots wake on a
NOTIFY-driven signal instead of always waiting out the poll interval, with a
hard poll-only fallback so it can never make things worse.

No pytest-asyncio in this repo — async bodies run via ``asyncio.run`` inside
sync test functions (mirrors tests/test_worker_runtime.py)."""

from __future__ import annotations

import asyncio

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
