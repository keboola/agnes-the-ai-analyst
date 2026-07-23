"""Headless (no-WebSocket) one-shot chat runs — Task 9's
``POST /api/v1/agents/{slug}/responses``.

``HeadlessSink`` is a duck-typed frame sink (``send_json``/``close``,
exactly what ``ChatManager.attach``/``_broadcast`` expect — see
``app/chat/manager.py``) that collects a single turn's frames in memory
instead of writing them to a live WebSocket.

**Frame shape note (verified against ``app/chat/manager.py``, not
assumed):** the task brief's sketch reads an ``assistant_message`` frame's
``text``/``content`` field. The real frame — see
``_pump_subprocess_to_ws``'s ``self._repo.append_message(... content=
frame.get("content", "") ...)`` and ``add_sink``'s history-replay frames
(``{"type": "assistant_message", "content": ..., "sender_email": ...}``)
— only ever carries the text under ``content``, never ``text``. This
sink reads ``content`` only (no ``text`` fallback) to match the real
producer exactly; a differently-shaped duck-typed frame from some future
producer would just leave ``answer`` at its previous value rather than
raise.

Turn completion is the ``"done"`` frame type (``manager.py``'s
``_pump_subprocess_to_ws``, ``ftype == "done"`` branch) — NOT
``assistant_message`` itself, since a turn can in principle emit more than
one frame before the runner signals it's finished (e.g. tool calls interleave
with the answer). ``done_event`` is only set on ``"done"``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from app.chat.types import Surface

logger = logging.getLogger(__name__)


class HeadlessSink:
    """Duck-typed frame sink (``send_json``/``close``) collecting a
    one-shot run's frames in memory.

    ``answer`` tracks the most recent ``assistant_message`` frame's
    ``content`` seen so far — for a normal one-shot turn there is exactly
    one, but taking "most recent" rather than "first" is harmless and
    matches how a live WS client would render sequential frames.
    """

    def __init__(self) -> None:
        self.frames: list[dict] = []
        self.done_event = asyncio.Event()
        self.answer: str = ""

    async def send_json(self, frame: dict) -> None:
        self.frames.append(frame)
        if frame.get("type") == "assistant_message":
            content = frame.get("content")
            if content:
                self.answer = content
        if frame.get("type") == "done":
            self.done_event.set()

    async def close(self) -> None:
        # Mirrors a live WS's disconnect: unblock any waiter rather than
        # hang forever if the sink is torn down (e.g. session killed)
        # before a "done" frame ever arrived.
        self.done_event.set()


async def _wait_for_sink(manager, chat_id: str, sink: "HeadlessSink", timeout_s: int) -> bool:
    """Await ``sink.done_event`` up to ``timeout_s``, always detaching the
    sink afterward. Returns ``True`` if the wait timed out.

    Detaching on timeout is deliberate, not a "give up on the run" signal —
    the sandbox keeps running the turn regardless (see the module
    docstring's timeout-vs-kill contract). ``ChatManager.detach_sink``
    starts the normal linger→pause countdown when this was the last sink;
    a later re-``attach()`` (the sync-timeout-degrades-to-background-job
    path in ``app.api.agent_runtime``) cancels that countdown the same way
    a reconnecting WS client would.
    """
    timed_out = False
    try:
        await asyncio.wait_for(sink.done_event.wait(), timeout=timeout_s)
    except asyncio.TimeoutError:
        timed_out = True
    finally:
        try:
            await manager.detach_sink(chat_id, sink)
        except Exception:
            logger.exception("headless: detach_sink failed for %s — sink leak, non-fatal", chat_id)
    return timed_out


async def run_one_shot(
    manager,
    *,
    user_email: str,
    agent_id: Optional[str],
    prompt: str,
    timeout_s: int,
) -> dict[str, Any]:
    """Create a FRESH session, send ``prompt``, and wait up to
    ``timeout_s`` seconds for the turn to complete.

    Returns ``{"chat_id": ..., "answer": ..., "timed_out": bool}``.
    ``answer`` is whatever was collected before the wait ended — the empty
    string if no ``assistant_message`` frame ever arrived (e.g. an
    immediate timeout). A timeout does NOT stop the run: the sandbox keeps
    processing the turn after this function returns; see
    ``app.api.agent_runtime`` for how the caller degrades a sync timeout
    into a background job that resumes waiting on the same ``chat_id``
    (via :func:`await_completion`) instead of re-sending the prompt.
    """
    session = await manager.create_session(
        user_email=user_email,
        surface=Surface.API,
        agent_id=agent_id,
    )
    chat_id = session.id
    sink = HeadlessSink()
    await manager.attach(chat_id, sink, is_primary=True)
    await manager.send_user_message(chat_id, prompt, sender_email=user_email)
    timed_out = await _wait_for_sink(manager, chat_id, sink, timeout_s)
    return {"chat_id": chat_id, "answer": sink.answer, "timed_out": timed_out}


def _last_assistant_message(manager, chat_id: str) -> Optional[str]:
    """Best-effort read of the most recent persisted assistant message for
    ``chat_id`` — the fallback path in :func:`await_completion` for a turn
    that already finished (and had its ``turn_buffer`` cleared) before the
    job worker attached, so no ``"done"`` frame is ever coming for a fresh
    sink to see.

    Reaches into ``ChatManager._repo`` (private) rather than a public
    accessor — ``ChatManager`` has none for a single "last assistant
    message" read today. Documented adaptation (Task 9): acceptable here
    because ``headless.py`` lives in the same ``app.chat`` package as
    ``manager.py``, and a failure (attribute missing, repo error) is
    swallowed — this is a best-effort fallback, not the primary path.
    """
    repo = getattr(manager, "_repo", None)
    if repo is None:
        return None
    try:
        messages = repo.list_messages(chat_id)
    except Exception:
        logger.exception("headless: _last_assistant_message lookup failed for %s", chat_id)
        return None
    for msg in reversed(messages):
        if getattr(msg, "role", None) == "assistant":
            return getattr(msg, "content", None) or ""
    return None


async def await_completion(
    manager,
    *,
    chat_id: str,
    timeout_s: int,
) -> dict[str, Any]:
    """Resume waiting on an ALREADY-RUNNING (or paused) session — no
    ``send_user_message`` call, so the original prompt is never resent.

    Used by the ``agent_response`` job worker when a sync call's
    ``run_one_shot`` hit its wait timeout: the job re-``attach()``es a
    fresh ``HeadlessSink`` to the same ``chat_id`` (this reseats the sink
    on an ACTIVE session, or resumes a PAUSED one — see
    ``ChatManager.attach``) and waits again, this time with the job's own
    (typically much longer) timeout.

    **Race the sink can't see (documented adaptation):** the turn may have
    already finished — ``"done"`` frame broadcast, ``turn_buffer`` cleared
    — in the gap between the sync call's timeout and the worker picking up
    the job. A sink attached AFTER that point replays an empty
    ``turn_buffer`` (see ``ChatManager._seat_sink``) and would otherwise
    wait out the full job timeout for a frame that already happened. Right
    after attaching, this checks ``LiveSession.turn_in_flight`` (via
    ``manager.list_live()``); if the turn is not in flight and the sink
    collected nothing, the answer is read straight from persisted storage
    (:func:`_last_assistant_message`) instead of waiting. The same fallback
    also covers the (rarer) case where the wait genuinely timed out but the
    turn actually completed in the interim.

    Returns the same shape as :func:`run_one_shot` minus the answer having
    necessarily come from a prompt sent in THIS call.
    """
    sink = HeadlessSink()
    await manager.attach(chat_id, sink, is_primary=False)

    live = next((entry for entry in manager.list_live() if entry.chat_id == chat_id), None)
    if live is not None and not live.turn_in_flight and not sink.frames:
        answer = _last_assistant_message(manager, chat_id)
        if answer is not None:
            await manager.detach_sink(chat_id, sink)
            return {"chat_id": chat_id, "answer": answer, "timed_out": False}

    timed_out = await _wait_for_sink(manager, chat_id, sink, timeout_s)
    if timed_out and not sink.answer:
        fallback = _last_assistant_message(manager, chat_id)
        if fallback:
            return {"chat_id": chat_id, "answer": fallback, "timed_out": False}
    return {"chat_id": chat_id, "answer": sink.answer, "timed_out": timed_out}
