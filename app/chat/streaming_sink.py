"""``StreamingSink`` — async-queue chat sink adapting ``ChatManager``'s
duck-typed ``send_json``/``close`` fan-out (see ``app.chat.manager``'s
``_broadcast``/``_seat_sink``) to an async iterator, for the agent-as-API
SSE endpoint (``POST /api/v1/sessions/{id}/messages``, V1b Task 4).

Overflow policy (binding correction C8 on the V1b Task 4 brief): this sink
must NEVER silently drop a frame. Dropping would corrupt two things at
once — the ordered AG-UI event stream and the per-session ``id:``
(``{chat_id}:{seq}``, see ``app.chat.frame_seq``) sequence a client uses to
detect gaps. A naive bounded queue with drop-oldest-on-full avoids blocking
the manager's broadcast loop, but a caller downstream (an SSE client that
stalls mid-turn) would then silently miss frames with no way to know it.

Instead: ``send_json`` blocks (bounded by ``put_timeout_s``) when the queue
is full — backpressure, not data loss. Only on SUSTAINED overflow (the put
itself times out — a consumer that has stopped draining entirely) does this
give up: it stops accepting further frames and arranges for the terminal
``{"type": "error", "code": "stream_overflow"}`` frame (mapped by
``app.api.agent_sse.frame_to_agui`` to ``RUN_ERROR``) to be the last thing
``__aiter__`` yields, once whatever was already queued has drained in
order. No frame already accepted into the queue is ever discarded.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Optional

logger = logging.getLogger(__name__)

#: Bounded queue depth — generous headroom for a fast token stream against
#: a momentarily slow (but still-draining) SSE client.
DEFAULT_MAXSIZE = 1000

#: How long ``send_json`` blocks waiting for queue room before declaring
#: the consumer stalled and giving up. Chosen to comfortably absorb a GC
#: pause or a slow network write without giving up on a client that is
#: still, if slowly, draining.
DEFAULT_PUT_TIMEOUT_S = 5.0

#: How often ``__aiter__`` re-checks close/overflow state while the queue
#: is momentarily empty and no frame has arrived yet. Bounds a blocking
#: ``queue.get()`` so ``close()`` (or a sustained-overflow) can never leave
#: a consumer parked forever — deliberately NOT implemented via a sentinel
#: object pushed through the (bounded) queue: that approach deadlocks if
#: ``close()`` is called while the queue is already full and nothing is
#: draining it yet. Short enough that it adds no perceptible latency to a
#: real close signal; irrelevant to in-flight frame delivery, which always
#: completes ``get()`` immediately regardless of this bound.
_POLL_S = 0.1


class StreamingSink:
    """Duck-typed chat sink (``send_json``/``close``) that a consumer can
    ``async for frame in sink`` over.

    Seated onto a live chat session via ``ChatManager.attach(chat_id, sink)``
    — every frame the session's ``_broadcast`` fans out lands here via
    ``send_json``. One instance is used for exactly one turn: created right
    before ``attach()``, iterated by the SSE response generator, and
    discarded after ``detach_sink()``.
    """

    def __init__(
        self,
        *,
        maxsize: int = DEFAULT_MAXSIZE,
        put_timeout_s: float = DEFAULT_PUT_TIMEOUT_S,
    ) -> None:
        self._queue: "asyncio.Queue[Any]" = asyncio.Queue(maxsize=maxsize)
        self._put_timeout_s = put_timeout_s
        # Set once sustained overflow is detected — send_json short-circuits
        # to a no-op from that point on (the terminal error frame is already
        # queued/pending; nothing further can usefully be delivered anyway).
        self._overflowed = False
        # The terminal overflow frame, held back until the queue has fully
        # drained in order (see __aiter__) rather than jumping the queue.
        self._pending_overflow_frame: Optional[dict] = None
        self._closed = False

    async def send_json(self, frame: dict) -> None:
        """Push ``frame`` onto the queue, per the duck-typed sink contract
        every other chat sink (web WS, ``SlackSinkBridge``, ``GapReplayGate``)
        already honors — see ``app.chat.manager._broadcast``.

        Blocks (bounded by ``put_timeout_s``) when the queue is full rather
        than dropping — see the module docstring for why. A timeout marks
        this sink permanently overflowed; the caller (``_broadcast``'s
        fan-out loop) sees this return normally either way, so one stalled
        SSE consumer never blocks delivery to the session's other sinks.
        """
        if self._overflowed or self._closed:
            return
        try:
            await asyncio.wait_for(self._queue.put(frame), timeout=self._put_timeout_s)
        except asyncio.TimeoutError:
            logger.warning(
                "StreamingSink: queue full for %.1fs (consumer not draining) — "
                "emitting terminal stream_overflow and closing this sink",
                self._put_timeout_s,
            )
            self._overflowed = True
            self._pending_overflow_frame = {
                "type": "error",
                "message": "stream buffer overflow — consumer too slow",
                "code": "stream_overflow",
            }

    async def close(self) -> None:
        """Signal end-of-stream to ``__aiter__``. Idempotent.

        Just flips a flag — deliberately does NOT touch the queue (see
        ``_POLL_S``'s docstring for why pushing a sentinel through it would
        risk deadlocking against a full, undrained queue).
        """
        self._closed = True

    async def __aiter__(self) -> AsyncIterator[dict]:
        while True:
            if self._queue.empty():
                if self._overflowed:
                    # Every frame accepted before overflow has now been
                    # delivered, in order. Yield the terminal marker once,
                    # then stop — no further sends were (or will be) accepted.
                    if self._pending_overflow_frame is not None:
                        frame = self._pending_overflow_frame
                        self._pending_overflow_frame = None
                        yield frame
                    return
                if self._closed:
                    return
            try:
                frame = await asyncio.wait_for(self._queue.get(), timeout=_POLL_S)
            except asyncio.TimeoutError:
                continue
            yield frame
