"""Desktop/browser notification fan-out via the coordination pub/sub channel.

Wave-2F task 6: this replaces the old standalone `services/ws_gateway`
process (an aiohttp server on 127.0.0.1:8765 + a Unix-socket HTTP dispatch
endpoint, with notifications routed through a module-level `connections`
dict). Producers — the Telegram bot's on-demand script runner, and any
future callers — publish a notification for a user here; the GATEWAY-role
process(es) that hold a live desktop WebSocket for that user receive it via
:mod:`app.coordination` pub/sub and fan out to their local sockets (see
``app/api/notifications_ws.py`` for the consumer side).

Channel naming: one channel per user (``notify:{user}``) rather than one
global channel — a gateway process only subscribes to channels for users it
actually has a live connection for, so idle users generate no fan-out
traffic on any other gateway replica.

Memory-mode coordination backend: ``publish()`` calls every subscribed
handler synchronously in the caller's own thread/task — i.e. today's
single-process, same-call-stack delivery, unchanged. Redis-backed
coordination: ``publish()`` is a network call to Redis, and delivery reaches
every replica subscribed to the channel (cross-replica fan-out).
"""

from __future__ import annotations

import json
import logging

from app.coordination.base import CoordinationUnavailable
from app.coordination.factory import coordination

logger = logging.getLogger(__name__)

_CHANNEL_PREFIX = "notify:"


def notify_channel(user: str) -> str:
    """Return the coordination pub/sub channel name for ``user``."""
    return f"{_CHANNEL_PREFIX}{user}"


def publish_notification(user: str, payload: dict) -> None:
    """Publish a desktop/browser notification for ``user``.

    ``payload`` is an arbitrary JSON-serializable dict — the WS consumer
    wraps it as ``{"type": "notification", **payload}`` before sending to
    the client (see ``app/api/notifications_ws.py::_deliver``).

    Log-and-continue on :class:`CoordinationUnavailable` (e.g. Redis
    unreachable): a lost desktop notification is an acceptable degradation
    — the analyst still has the underlying data (Telegram message, script
    output, app history) — so this must never raise into a producer's
    request/response cycle or background dispatch thread.
    """
    message = json.dumps(payload)
    try:
        coordination().publish(notify_channel(user), message)
    except CoordinationUnavailable:
        logger.warning(
            "notification dropped for %s: coordination backend unavailable",
            user,
        )
