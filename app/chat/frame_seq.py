"""Monotonic per-session frame sequence numbers (wave-2F task 2).

Every outbound chat frame gains ``seq`` (int, monotonic per ``chat_id``) and
``id`` (``f"{chat_id}:{seq}"``). Stamping happens at the frame-emission
choke points:

- ``app.chat.manager.ChatManager._broadcast`` — the single seam ALL runner
  frames (token/tool_call/tool_result/assistant_message/done/...) and every
  manager-originated broadcast (ready/error/cancelled/session_renamed) pass
  through, fanning out to every sink (web WS, ``SlackSinkBridge``, co-drive
  joiners) alike.
- ``ChatManager._seat_sink`` / ``ChatManager.add_sink`` — the per-connection
  ``{"type": "ready"}`` frame sent to exactly the newly-seated sink (not
  broadcast, so it falls outside ``_broadcast``).
- ``app.api.chat`` — the two ``runner_not_ready`` error frames sent directly
  on the WS by the route handler before a ``LiveSession`` even exists.

Frames replayed verbatim from ``LiveSession.turn_buffer`` (mid-turn
reconnect/join) are NOT re-stamped — they already carry the ``seq``/``id``
assigned when they were first broadcast, and re-stamping would both waste a
sequence number and change the id of a frame the client may have already
seen. Historical messages reconstructed from ``chat_messages`` in
``add_sink`` are also left unstamped: they predate this mechanism entirely
(no persisted seq column) and the additive/back-compat contract is that a
client tolerates frames without ``seq``/``id`` — see ``docs`` note on the
web client side (``app/web/static/js/chat.js``).

This task (wave-2F task 2) is envelope-only: it does not build a replay
stream. A future task (wave-2F task 3) is expected to use
``FrameSequencer`` / the ``chat-seq:{chat_id}`` counter key to answer
"replay everything after seq N" on reconnect.
"""

from __future__ import annotations

from app.coordination.factory import coordination

#: Coordination-backend TTL for the per-session seq counter. Deliberately
#: NOT sized to outlive the longest possible PAUSED session
#: (``ChatConfig.paused_ttl_seconds``, default 7 days) — it only needs to
#: comfortably outlive one ACTIVE session lifetime between messages
#: (``ChatConfig.max_session_seconds``, default 4h). A session paused (or
#: otherwise silent) longer than this TTL resumes with the counter reset to
#: 1 — a narrow, documented gap: a future replay mechanism (wave-2F task 3)
#: must treat a reset/lower seq as "nothing to replay from", never as
#: license to replay the wrong frames. Widening the TTL (or refreshing it at
#: pause time) is a follow-up if this proves too narrow in practice.
_SEQ_TTL_SEC = 6 * 3600


class FrameSequencer:
    """Issues monotonic ``seq`` numbers for one chat session's outbound frames.

    Stateless wrapper over the coordination-backend counter — the actual
    counter lives in ``coordination()`` (``chat-seq:{chat_id}``), not on
    ``self``, so a fresh instance for the same ``chat_id`` (e.g. constructed
    in a brand-new process after a crash respawn or cross-gateway resume)
    continues the same sequence rather than restarting at 1. Under the
    default ``memory`` coordination backend, ``incr`` is itself in-process,
    so sequencing is still correctly monotonic within a single process —
    the same "works identically, just not HA" degrade the rest of the
    coordination layer follows.
    """

    def __init__(self, chat_id: str) -> None:
        self._chat_id = chat_id

    def next_seq(self) -> int:
        """Return the next monotonic seq number for this session (1-based)."""
        return coordination().incr(f"chat-seq:{self._chat_id}", ttl_s=_SEQ_TTL_SEC)


def stamp_frame(chat_id: str, frame: dict) -> dict:
    """Add ``seq`` + ``id`` to `frame` in place and return it.

    The sole writer of these two keys — every emit site listed in the
    module docstring calls this instead of hand-rolling the fields, so the
    envelope shape (and the underlying counter key) never drifts between
    call sites.
    """
    seq = FrameSequencer(chat_id).next_seq()
    frame["seq"] = seq
    frame["id"] = f"{chat_id}:{seq}"
    return frame
