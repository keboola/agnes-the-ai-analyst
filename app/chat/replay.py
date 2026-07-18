"""Outbound frame replay on WS reconnect (wave-2F task 3).

Builds on the seq envelope from ``app.chat.frame_seq`` (wave-2F task 2):
every frame ``ChatManager._broadcast`` stamps with ``seq``/``id`` is ALSO
appended to a bounded coordination-backend stream keyed
``chat-out:{chat_id}`` (:func:`append_frame`). When a client (re)connects
with the highest ``seq`` it last saw, :func:`replay_since` answers "what,
if anything, do I need to resend before resuming live delivery" — see
``app.api.chat.ws_stream`` / ``ws_join`` for the caller side.

Failure posture, deliberately asymmetric between the two halves:

- :func:`append_frame` is best-effort — a coordination-backend hiccup
  degrades replay (a gap the client may not be able to recover without a
  full refresh) but must NEVER break LIVE delivery, which is the whole
  point of the chat feature. Log and continue.
- :func:`replay_since` degrades the same way on a coordination blip: it
  skips the replay attempt (returns no frames, no full-refresh signal)
  rather than raising out of the WS route handler. A transiently
  unavailable backend at reconnect time is treated the same as "nothing to
  replay" — the alternative (forcing every blip into a full_refresh) would
  make an ordinary reconnect noisier than a genuine seq-reset for no
  benefit, since the backend being unavailable already means the ticket
  consume moments earlier in the same route would have failed and closed
  the WS with 4503 in the far more common case.

Memory-backend note: the "stream" is an in-process bounded ``deque`` (see
``app.coordination.memory.MemoryCoordinationBackend``) — replay only works
within the SAME process that produced the frames (no cross-restart
persistence), which is the same single-process-only caveat every other
``memory``-backend coordination primitive already carries.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

from app.chat.frame_seq import peek_seq
from app.coordination.base import CoordinationUnavailable
from app.coordination.factory import coordination

logger = logging.getLogger(__name__)

#: Bounded retention for the outbound-frame replay stream, per chat session.
#: Sized to comfortably cover a reconnect after a brief network blip or
#: gateway failover without needing unbounded memory/Redis growth — a
#: client that has fallen more than 1000 frames behind gets a
#: ``full_refresh`` instead (see :func:`replay_since`), which is the
#: correct outcome anyway (that much backlog means "just reload").
STREAM_MAXLEN = 1000


def _stream_key(chat_id: str) -> str:
    return f"chat-out:{chat_id}"


async def append_frame(chat_id: str, frame: dict) -> None:
    """Append ``frame`` (already stamped with ``seq``/``id``) to this
    session's replay stream. Best-effort — see module docstring."""
    try:
        await asyncio.to_thread(
            coordination().stream_append,
            _stream_key(chat_id),
            frame,
            maxlen=STREAM_MAXLEN,
        )
    except CoordinationUnavailable:
        logger.warning(
            "chat-out stream append failed for %s; reconnect replay for this frame will be degraded",
            chat_id,
        )


@dataclass
class ReplayOutcome:
    """Result of :func:`replay_since`.

    Exactly one of these is the caller's instruction:

    - ``full_refresh=True`` — the gap between the client's ``last_seq`` and
      what the stream can prove was delivered could not be closed
      confidently (evicted past ``STREAM_MAXLEN``, or the counter/stream
      was reset out from under the client, e.g. a coordination-backend
      ``FLUSHALL``). ``frames`` is always empty in this case — sending a
      partial/wrong replay would be worse than telling the client to
      reload.
    - ``full_refresh=False`` — ``frames`` (possibly empty) is exactly what
      to resend, in order, before resuming live delivery. Empty means the
      client is already caught up; nothing needs to be sent.
    """

    frames: list[dict] = field(default_factory=list)
    full_refresh: bool = False


async def replay_since(chat_id: str, last_seq: int) -> ReplayOutcome:
    """Compute what to replay to a (re)connecting client that last saw
    ``last_seq`` for ``chat_id``.

    ``last_seq <= 0`` means the client has no baseline for this session
    (a brand-new WS connection that never received a frame yet, e.g. the
    very first open of a session — history for that case comes from the
    REST ``GET /sessions/{id}/messages`` load the client already does
    before opening the WS) — no replay is attempted and this is NOT a
    full_refresh; it is exactly correct to proceed straight to live.

    full_refresh decision is EVICTION-based, not contiguity-based
    (2026-07-18 hardening): some frames are deliberately stamped (consuming
    a seq — see ``app.chat.frame_seq.stamp_frame``) but never appended to
    this stream — the per-sink ``ready``/``runner_not_ready`` frames
    ``ChatManager._seat_sink``/``add_sink``/``app.api.chat`` send directly
    to exactly one connection are never broadcast, so they're never handed
    to :func:`append_frame`. Those are legitimate holes in the stream's seq
    numbering: a client that never received one of those frames (they were
    private to a *different* connection) has nothing to recover — checking
    ``entries[0].seq == last_seq + 1`` (strict contiguity) would false-
    positive a ``full_refresh`` on every such hole even though nothing was
    actually lost. The only thing that legitimately forces a full refresh
    is EVICTION: frames that WERE broadcast (and so WOULD have reached this
    client live) have aged out past ``STREAM_MAXLEN`` and can no longer be
    proven delivered. That's ``last_seq + 1 < min_retained_seq`` — the
    client's next-expected frame is older than anything the stream still
    holds. A hole from a private frame never trips this: the oldest
    retained entry doesn't move just because a seq number in between was
    never appended.
    """
    if last_seq <= 0:
        return ReplayOutcome(frames=[], full_refresh=False)

    try:
        current_seq = await asyncio.to_thread(peek_seq, chat_id)
    except CoordinationUnavailable:
        logger.warning("chat-out replay peek failed for %s; skipping replay attempt", chat_id)
        return ReplayOutcome(frames=[], full_refresh=False)

    if current_seq < last_seq:
        # The counter is BEHIND what the client already saw — it was reset
        # (e.g. FLUSHALL wiped the coordination backend). No amount of
        # stream replay can recover from this; the client must reload.
        return ReplayOutcome(frames=[], full_refresh=True)
    if current_seq == last_seq:
        # Caught up already — nothing happened for this session while the
        # client was away.
        return ReplayOutcome(frames=[], full_refresh=False)

    try:
        # Unfiltered read (no after_seq) — we need the OLDEST entry the
        # stream still retains (min_retained_seq below) to make the
        # eviction call, not just what's after last_seq. Both backends
        # return entries sorted by their own "seq" field (see
        # app.coordination.memory / app.coordination.redis_backend), so
        # entries[0] is always the oldest-retained entry.
        all_entries = await asyncio.to_thread(coordination().stream_read, _stream_key(chat_id))
    except CoordinationUnavailable:
        logger.warning("chat-out replay read failed for %s; skipping replay attempt", chat_id)
        return ReplayOutcome(frames=[], full_refresh=False)

    if not all_entries:
        # current_seq > last_seq proves something WAS stamped since the
        # client's baseline, yet the stream holds nothing at all retained.
        # This can't be distinguished from a stream-level reset (a partial
        # coordination-backend wipe that clears the stream but not the
        # counter) — conservative: can't prove nothing was lost.
        return ReplayOutcome(frames=[], full_refresh=True)

    min_retained_seq = all_entries[0].get("seq", 0)
    if last_seq + 1 < min_retained_seq:
        # Genuine eviction: the oldest frame the client still needs has
        # already aged out past STREAM_MAXLEN. No amount of holes-are-fine
        # tolerance can recover this — the client must reload.
        return ReplayOutcome(frames=[], full_refresh=True)

    frames = [e for e in all_entries if e.get("seq", 0) > last_seq]
    return ReplayOutcome(frames=frames, full_refresh=False)


class GapReplayGate:
    """Wraps a live sink during the reconnect gap-replay window (CRITICAL
    fix, 2026-07-18 — closes the reconnect replay silent-gap race).

    The bug: the reconnect path used to compute the gap replay (a snapshot
    of ``replay_since``) and only AFTERWARDS seat the reconnecting
    connection as a live sink via ``ChatManager.attach``/``add_sink``. Any
    frame broadcast in that window landed in neither the snapshot (already
    read) nor live delivery (not seated yet) — silently lost, and the
    client has no way to detect the gap (it only dedups by seq, it can't
    invent a missing frame).

    The fix: seat the connection as a live sink FIRST — so nothing
    broadcast from this point on can be lost — but route everything
    through this gate instead of the socket directly, buffering it, until
    the caller has finished computing and is ready to deliver the gap
    replay. :meth:`release` then merges the buffered (live-during-the-
    window) frames with the gap-replay frames the caller read from the
    stream, sorts the combination by ``seq`` (a frame that was stamped but
    never appended to the stream — see ``replay_since``'s module note —
    can otherwise end up buffered "behind" a later, stream-sourced frame
    that reaches the socket first), de-duplicates any frame that shows up
    in both sources (the same broadcast can legitimately be seen by both:
    appended to the stream AND delivered directly to this now-seated
    sink), and sends the result in one consistent, gap-free, in-order pass
    before going fully passthrough for the rest of the connection's life.

    IMPORTANT fix (2026-07-18, narrower race): ``release``'s flush loop is
    ``await``-heavy (one socket write per buffered/replayed frame). The
    first version flipped ``_buffering`` to ``False`` BEFORE that loop ran,
    so a concurrent ``send_json`` call (a live frame arriving via
    ``ChatManager._broadcast`` while the flush is still in flight) would
    see ``_buffering is False`` and write straight to ``_real``, landing
    in the middle of the still-in-progress ordered flush — e.g. buffer
    holds seq [5, 6], live seq 7 arrives mid-flush, socket sees [5, 7, 6]:
    still gap-free at the transport level, but delivered out of order, and
    the client's dedup-by-seq logic drops 6 as a stale duplicate. Same bug
    class as the module docstring above, narrower window; existing tests
    didn't catch it because ``tests.chat_fakes.FakeWS.send_json`` appends
    synchronously with no ``await`` point, so the old flush loop never
    actually yielded control mid-iteration.

    The fix: an ``asyncio.Lock`` (``_lock``) makes buffering-vs-flush a
    single atomic decision instead of two (check-then-append raced against
    check-then-passthrough). ``send_json`` only ever appends to the buffer
    or does one passthrough send while holding the lock — never both, and
    never a long-lived hold. ``release`` does NOT hold the lock across its
    own socket sends (that would serialize every concurrent live frame for
    this session behind this flush's I/O via ``live._broadcast_lock``,
    since ``_broadcast`` calls ``gate.send_json`` while already holding
    that lock — see ``ChatManager._broadcast``). Instead ``release`` keeps
    ``_buffering`` at ``True`` and drains the buffer in a loop — anything
    that lands mid-flush (``_buffering`` still ``True`` at the instant
    ``send_json`` takes the lock) gets appended to the buffer rather than
    written directly, so the next drain iteration picks it up in seq
    order — and only flips ``_buffering`` to ``False`` once a lock-held
    check finds the buffer empty, i.e. no frame arrived between the last
    drain and the flip. A frame that arrives in the narrow window between
    that empty-check and the flip cannot exist: both are done under the
    same lock acquisition, so ``send_json`` either completed its append
    before the check (and got drained) or is still blocked on the lock
    (and will see ``_buffering is False`` only after the flip, going
    passthrough correctly — after, not during or before, the flush).
    """

    def __init__(self, real_sink) -> None:
        self._real = real_sink
        self._buffering = True
        self._buffer: list[dict] = []
        self._lock = asyncio.Lock()

    async def send_json(self, frame: dict) -> None:
        # Buffering path: append only — no await other than the (short)
        # lock acquisition, so this never blocks behind a slow socket
        # write. Passthrough path: the send itself happens under the lock
        # so it can never interleave with a release() flush iteration
        # (which also takes the lock to swap the buffer) — see class
        # docstring.
        async with self._lock:
            if self._buffering:
                self._buffer.append(frame)
                return
            await self._real.send_json(frame)

    async def close(self) -> None:
        await self._real.close()

    async def release(self, extra_frames: Optional[list[dict]] = None) -> None:
        """Stop buffering and flush everything captured, in seq order.

        ``extra_frames`` are the gap-replay frames the caller read from
        the stream (older than anything seated live) — merged with
        whatever landed in this gate's buffer while it was seating +
        computing the replay, de-duplicated by ``seq`` where present, and
        sorted so delivery order matches assignment order even when a
        private (never-appended) frame's seq falls between two
        stream-sourced ones. Frames without an int ``seq`` (e.g. add_sink's
        unstamped persisted-history replay) sort before every seq'd frame,
        preserving their relative arrival order via a stable sort.

        Drains in a loop rather than one shot: ``_buffering`` stays
        ``True`` (so any frame arriving mid-flush keeps landing in the
        buffer instead of racing straight to the socket — see class
        docstring) until a lock-held check finds nothing left to drain,
        at which point it flips to ``False`` atomically with that check.
        """
        seen_seqs: set[int] = set()

        def _order(frames: list[dict]) -> list[dict]:
            ordered: list[tuple[int, int, dict]] = []
            for idx, frame in enumerate(frames):
                seq = frame.get("seq")
                if isinstance(seq, int):
                    if seq in seen_seqs:
                        continue  # same frame surfaced via both sources
                    seen_seqs.add(seq)
                    sort_key = seq
                else:
                    sort_key = -1  # unstamped frames sort first, tie-broken by idx
                ordered.append((sort_key, idx, frame))
            ordered.sort(key=lambda t: (t[0], t[1]))
            return [frame for _, _, frame in ordered]

        # First batch: the caller's stream-replay frames plus whatever had
        # already landed in the buffer by the time release() was invoked.
        async with self._lock:
            pending, self._buffer = self._buffer, []
        for frame in _order(list(extra_frames or []) + pending):
            await self._real.send_json(frame)

        # Drain anything that arrived WHILE the batch above was being sent
        # (still buffered, because _buffering was still True). Keep going
        # until a lock-held snapshot finds the buffer empty, then flip
        # atomically — no send_json call can slip a frame in between that
        # check and the flip, since both happen under the same lock.
        while True:
            async with self._lock:
                if not self._buffer:
                    self._buffering = False
                    break
                pending, self._buffer = self._buffer, []
            for frame in _order(pending):
                await self._real.send_json(frame)
