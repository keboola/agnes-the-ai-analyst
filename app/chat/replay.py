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
        entries = await asyncio.to_thread(
            coordination().stream_read,
            _stream_key(chat_id),
            last_seq,
        )
    except CoordinationUnavailable:
        logger.warning("chat-out replay read failed for %s; skipping replay attempt", chat_id)
        return ReplayOutcome(frames=[], full_refresh=False)

    # current_seq > last_seq, so at least one frame SHOULD be retained. An
    # empty result, or one whose oldest entry isn't immediately after
    # last_seq, means the frames we need were evicted past STREAM_MAXLEN
    # (or the stream was reset, e.g. FLUSHALL cleared it while the counter
    # itself either wasn't touched or was reset to the same relative
    # offset) — a gap we cannot fill confidently.
    if not entries or entries[0].get("seq") != last_seq + 1:
        return ReplayOutcome(frames=[], full_refresh=True)

    return ReplayOutcome(frames=entries, full_refresh=False)
