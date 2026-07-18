"""Inbound command stream: routing a user message to the gateway that
actually owns the session's LiveSession (wave-2F task 4).

Companion to ``app.chat.replay`` (wave-2F task 3), same coordination-backend
stream primitive (``CoordinationBackend.stream_append``/``stream_read``,
wave-2F task 3), opposite direction: outbound (runner -> client) frames
replay via ``chat-out:{chat_id}``; inbound (client -> runner) messages route
via ``chat-in:{chat_id}`` here, sequenced by their own counter
(``chat-in-seq:{chat_id}``) so the two directions never share (or contend
on) a sequence space.

Why this exists: ``app.chat.routing`` (wave-2F task 1) lets any gateway
replica find out which OTHER replica currently hosts a session's live
runner, but gives no way to actually get a user's text there. A WebSocket
connection is inherently sticky to whichever gateway TCP-terminated it, so
``app.api.chat``'s ``ws_stream``/``ws_join`` routes always land on the
owning gateway already (the same connection that ran ``attach()`` and
therefore claimed the lease) -- but a Slack event webhook
(``services.slack_bot.events``) has no such stickiness: a load balancer can
hand it to ANY replica, and that replica's ``ChatManager`` may not have the
session's ``LiveSession`` locally at all. ``app.chat.manager.ChatManager.
send_user_message`` is the single choke point both callers go through, so
it is the seam this module plugs into: when ``send_user_message`` finds no
local ``LiveSession`` AND ``app.chat.routing.owner_of`` says a *different*
gateway holds the lease, it hands the message to :func:`publish_inbound`
instead of racing to spawn a second runner. The owning gateway's per-session
``ChatManager._inbound_consumer_loop`` (started in ``_spawn_live``/
``_resume_from_row``, alongside the existing pump/wait tasks) drains the
stream in seq order and feeds each entry into its local runner's stdin via
the same delivery path the direct-owner call already used.

Memory backend / single-process story: since ``app.chat.routing.
this_gateway_id()`` is stable per PROCESS, and the ``memory`` coordination
backend only ever has one process's state to consult, ``owner_of(...)`` can
never return a value different from ``this_gateway_id()`` under ``memory``
-- there is no "other gateway" to forward to. So under the default
single-process deployment this module's :func:`publish_inbound` is simply
never called; ``send_user_message`` always takes the direct-owner path,
exactly as it did before this task existed.

Failure posture, DELIBERATELY the mirror image of ``app.chat.replay``'s:
outbound frame-replay append is best-effort (a dropped replay frame just
means a client falls back to full history reload -- annoying, not lossy),
so it swallows ``CoordinationUnavailable`` and logs. An inbound USER
message that silently vanished because a coordination-backend blip ate the
publish call is a much worse failure -- the user would see their message
"sent" with no indication the runner never received it. So
:func:`publish_inbound` does NOT swallow a publish failure: it raises
:class:`InboundPublishFailed`, a clean, specific, documented exception
``ChatManager.send_user_message`` lets propagate to the caller (WS route /
Slack event handler) instead of a raw ``CoordinationUnavailable`` leaking
out or the message being dropped with no signal at all.
"""

from __future__ import annotations

import logging
from typing import Callable

from app.coordination.base import CoordinationUnavailable
from app.coordination.factory import coordination

logger = logging.getLogger(__name__)

#: TTL for the per-session inbound-seq counter (``chat-in-seq:{chat_id}``).
#: Same margin/reasoning as ``app.chat.frame_seq._SEQ_TTL_SEC`` (the
#: OUTBOUND counter's TTL): must comfortably outlive a session's entire
#: wall-clock lifetime, including a long PAUSED stretch
#: (``ChatConfig.paused_ttl_seconds``, default 7 days) plus another ACTIVE
#: stretch after resume (``ChatConfig.max_session_seconds``, default 4h) --
#: 9 days total, generous margin past the 7-day-plus-4-hour default. Not
#: wired to a live ``ChatConfig`` instance for the same reason
#: ``frame_seq``'s counter isn't: it's a tiny, cheap coordination-backend
#: key, so a large fixed TTL costs nothing.
_SEQ_TTL_SEC = 9 * 24 * 3600

#: Bounded retention for the inbound-message stream, per chat session.
#: Matches ``app.chat.replay.STREAM_MAXLEN`` (the outbound stream's bound)
#: for now -- unlike that stream, an eviction here is NOT just "client
#: reloads": an evicted, never-delivered user message is silently lost.
#: 1000 comfortably covers any plausible backlog while a session is
#: unowned/mid-handoff; revisit if a future takeover story (wave-2F task 5)
#: needs a much longer unowned window.
STREAM_MAXLEN = 1000


class InboundPublishFailed(Exception):
    """A user message could not be published to the inbound stream.

    Raised by :func:`publish_inbound` when the coordination backend is
    unavailable at publish time (``CoordinationUnavailable``) -- see the
    module docstring for why this is a raised, sender-visible error rather
    than a swallowed-and-logged best-effort failure like most of this
    codebase's other coordination-backend helpers. The message was NOT
    accepted; the caller (``ChatManager.send_user_message``) lets this
    propagate so the sender's request fails cleanly instead of silently
    vanishing or crashing on a raw transport exception.
    """


def stream_key(chat_id: str) -> str:
    return f"chat-in:{chat_id}"


def _seq_key(chat_id: str) -> str:
    return f"chat-in-seq:{chat_id}"


def notify_channel(chat_id: str) -> str:
    """Pub/sub channel a session's owner subscribes to for a prompt wake-up
    when another gateway publishes an inbound message -- purely a latency
    optimization. The stream itself (:func:`stream_key`) is the source of
    truth; a missed/undelivered notify (e.g. the owner's consumer wasn't
    subscribed yet, or a redis blip ate the publish) only costs the
    consumer's poll-interval fallback, never correctness or ordering."""
    return f"chat-in-notify:{chat_id}"


def next_inbound_seq(chat_id: str) -> int:
    """Monotonic per-session sequence number for inbound (client -> runner)
    messages -- the mirror of ``app.chat.frame_seq.FrameSequencer`` for the
    opposite direction. Stateless wrapper over the coordination-backend
    counter, same as that sibling: whichever gateway calls this next
    continues the same sequence, no matter which process issued the
    previous number."""
    return coordination().incr(_seq_key(chat_id), ttl_s=_SEQ_TTL_SEC)


async def publish_inbound(chat_id: str, text: str) -> int:
    """Append a user message to ``chat_id``'s inbound stream and best-effort
    notify any subscribed owner. Returns the assigned seq.

    Raises :class:`InboundPublishFailed` if the append itself fails
    (coordination backend unavailable) -- see module docstring. The notify
    publish, by contrast, IS best-effort (log-and-continue): a missed
    notify only delays delivery until the owner's next poll tick, it can
    never lose the message (already durably appended by that point).
    """
    try:
        seq = next_inbound_seq(chat_id)
        entry = {"seq": seq, "text": text}
        coordination().stream_append(stream_key(chat_id), entry, maxlen=STREAM_MAXLEN)
    except CoordinationUnavailable as exc:
        logger.warning("chat-in publish failed for %s; message not accepted", chat_id)
        raise InboundPublishFailed(f"could not publish inbound message for {chat_id}") from exc
    try:
        coordination().publish(notify_channel(chat_id), str(seq))
    except CoordinationUnavailable:
        logger.debug(
            "chat-in notify publish failed for %s; owner's consumer will still pick this up on its next poll",
            chat_id,
        )
    return seq


def read_new(chat_id: str, after_seq: int) -> list[dict]:
    """Inbound entries for ``chat_id`` with ``seq > after_seq``, already
    sorted by seq (see ``CoordinationBackend.stream_read``'s contract) --
    thin wrapper so callers (``ChatManager._inbound_consumer_loop``) don't
    need to know the key convention. Returns ``[]`` (never raises) on a
    coordination-backend hiccup -- the caller's poll loop just tries again
    on its next tick, same degrade-to-"nothing new" posture
    ``app.chat.replay.replay_since`` uses for its own read failures."""
    try:
        return coordination().stream_read(stream_key(chat_id), after_seq=after_seq)
    except CoordinationUnavailable:
        logger.warning("chat-in read failed for %s; will retry on next poll", chat_id)
        return []


def subscribe_notify(chat_id: str, handler: Callable[[str], None]):
    """Subscribe ``handler`` to ``chat_id``'s notify channel. Returns an
    unsubscribe callable, or ``None`` if the coordination backend is
    unavailable right now (the consumer degrades to poll-only -- see
    ``ChatManager._inbound_consumer_loop``)."""
    try:
        return coordination().subscribe(notify_channel(chat_id), handler)
    except CoordinationUnavailable:
        logger.warning("chat-in notify subscribe failed for %s; consumer will poll only", chat_id)
        return None
