"""Unit tests for `app.chat.streaming_sink.StreamingSink` (V1b Task 4).

Binding correction C8: this sink must never silently drop a frame. These
tests exercise the three load-bearing properties directly: in-order
delivery, close()'s sentinel ending iteration, and the sustained-overflow
path (queue full + consumer stalled) emitting a terminal
`{"code": "stream_overflow"}` frame only after everything already queued
has drained — never dropping a frame to make room.

Uses ``asyncio.run`` rather than ``@pytest.mark.asyncio`` — this repo does
not depend on pytest-asyncio (see tests/test_cache_warmup.py for the same
pattern).
"""

from __future__ import annotations

import asyncio

from app.chat.streaming_sink import StreamingSink


def test_frames_delivered_in_order():
    async def _run():
        sink = StreamingSink()
        await sink.send_json({"type": "ready"})
        await sink.send_json({"type": "token", "content": "a"})
        await sink.send_json({"type": "token", "content": "b"})
        await sink.close()
        return [frame async for frame in sink]

    assert asyncio.run(_run()) == [
        {"type": "ready"},
        {"type": "token", "content": "a"},
        {"type": "token", "content": "b"},
    ]


def test_close_before_any_frame_ends_iteration_immediately():
    async def _run():
        sink = StreamingSink()
        await sink.close()
        return [frame async for frame in sink]

    assert asyncio.run(_run()) == []


def test_close_is_idempotent():
    async def _run():
        sink = StreamingSink()
        await sink.send_json({"type": "ready"})
        await sink.close()
        await sink.close()  # must not raise
        return [frame async for frame in sink]

    assert asyncio.run(_run()) == [{"type": "ready"}]


def test_send_json_after_close_is_a_noop():
    async def _run():
        sink = StreamingSink()
        await sink.close()
        await sink.send_json({"type": "ready"})  # must not raise or reopen the stream
        return [frame async for frame in sink]

    assert asyncio.run(_run()) == []


def test_sustained_overflow_drains_queued_frames_before_terminal_error():
    """Fill the queue to capacity, then push one more with a short timeout
    so it times out (simulating a stalled consumer). No frame accepted
    before the overflow is ever dropped — they all come out first, in
    order, THEN the terminal stream_overflow frame, then iteration ends."""

    async def _run():
        sink = StreamingSink(maxsize=2, put_timeout_s=0.05)
        await sink.send_json({"type": "token", "content": "1"})
        await sink.send_json({"type": "token", "content": "2"})

        # Queue is now full (maxsize=2) and nothing is draining it yet —
        # this put times out and marks the sink overflowed.
        await sink.send_json({"type": "token", "content": "3-dropped-attempt"})

        # A further send after overflow is a no-op (not queued, not counted).
        await sink.send_json({"type": "token", "content": "4-also-noop"})

        return [frame async for frame in sink]

    assert asyncio.run(_run()) == [
        {"type": "token", "content": "1"},
        {"type": "token", "content": "2"},
        {"type": "error", "message": "stream buffer overflow — consumer too slow", "code": "stream_overflow"},
    ]


def test_overflow_frame_yielded_exactly_once():
    async def _run():
        sink = StreamingSink(maxsize=1, put_timeout_s=0.05)
        await sink.send_json({"type": "ready"})
        await sink.send_json({"type": "token", "content": "times-out"})
        return [frame async for frame in sink]

    received = asyncio.run(_run())
    overflow_frames = [f for f in received if f.get("code") == "stream_overflow"]
    assert len(overflow_frames) == 1


def test_send_json_blocks_then_succeeds_once_consumer_drains():
    """Backpressure, not data loss: a put that would block because the
    queue is full succeeds once a concurrent reader makes room, rather than
    timing out — draining is enough to unblock it well within the
    timeout."""

    async def _run():
        sink = StreamingSink(maxsize=1, put_timeout_s=5.0)
        await sink.send_json({"type": "token", "content": "1"})

        async def _drain_one_after_delay():
            await asyncio.sleep(0.02)
            frame = await sink._queue.get()
            assert frame == {"type": "token", "content": "1"}

        drain_task = asyncio.create_task(_drain_one_after_delay())
        await sink.send_json({"type": "token", "content": "2"})  # would block without the drain
        await drain_task
        await sink.close()

        return [frame async for frame in sink]

    assert asyncio.run(_run()) == [{"type": "token", "content": "2"}]
