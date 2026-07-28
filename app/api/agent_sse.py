"""AG-UI SSE event vocabulary mapper (pure module, no I/O).

Maps internal chat frame dicts (as emitted by ``app.chat.manager``'s
runner/broadcast pipeline — see the ``_broadcast`` seam and
``app.chat.frame_seq.stamp_frame``) to the AG-UI event vocabulary, and
serializes AG-UI events as Server-Sent Events records.

Consumed by the agent-as-API streaming sink (V1b Task 4), which attaches to
a live/replay chat session and turns its frame stream into an SSE response.

Internal frame -> AG-UI event mapping:

- ``ready``             -> ``RUN_STARTED``
- ``token``             -> ``TEXT_MESSAGE_CONTENT`` (delta)
- ``assistant_message`` -> ``TEXT_MESSAGE_END`` (full content)
- ``tool_call``         -> ``TOOL_CALL_START`` (name, args)
- ``tool_result``       -> ``TOOL_CALL_END`` (result)
- ``done``              -> ``RUN_FINISHED``
- ``error``             -> ``RUN_ERROR`` (message)
- ``cancelled``         -> ``RUN_ERROR`` (message="cancelled", code="cancelled")
- anything else (incl. ``session_renamed``) -> dropped (``None``)

Note on ``tool_call`` field names: the real runner frame carries ``tool``
and ``args`` (verified against ``app.chat.manager``'s audit-log block,
which reads ``frame.get("tool")`` / ``frame.get("args", {})`` off the same
frame) — NOT ``name``/``input``. Only the *AG-UI event* uses the key name
``"name"`` (per the AG-UI vocabulary); the *source frame* field is
``"tool"``.
"""

from __future__ import annotations

import json
from typing import Any, Optional

#: AG-UI event types after which the SSE stream generator closes.
SSE_TERMINAL_TYPES = {"RUN_FINISHED", "RUN_ERROR"}


def frame_to_agui(frame: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Map one internal chat frame dict to one AG-UI event dict.

    Returns ``None`` for frame types with no AG-UI equivalent (e.g.
    ``session_renamed``) or an unrecognized/missing ``type`` — the caller
    drops these rather than emitting an SSE record.
    """
    ftype = frame.get("type")

    if ftype == "ready":
        return {"type": "RUN_STARTED"}
    if ftype == "token":
        return {"type": "TEXT_MESSAGE_CONTENT", "delta": frame.get("content")}
    if ftype == "assistant_message":
        return {"type": "TEXT_MESSAGE_END", "content": frame.get("content")}
    if ftype == "tool_call":
        return {
            "type": "TOOL_CALL_START",
            "name": frame.get("tool"),
            "args": frame.get("args"),
        }
    if ftype == "tool_result":
        return {"type": "TOOL_CALL_END", "result": frame.get("result")}
    if ftype == "done":
        return {"type": "RUN_FINISHED"}
    if ftype == "error":
        return {"type": "RUN_ERROR", "message": frame.get("message")}
    if ftype == "cancelled":
        return {"type": "RUN_ERROR", "message": "cancelled", "code": "cancelled"}

    return None


def sse_bytes(event: dict[str, Any], frame_id: Optional[str]) -> bytes:
    """Serialize one AG-UI event dict as a single SSE record.

    Emits ``id: {frame_id}`` only when ``frame_id`` is not ``None`` (the
    per-session ``{chat_id}:{seq}`` id from ``stamp_frame`` — historical/
    unstamped frames have none), then ``event: {event['type']}``, then
    ``data: {json}``, followed by the blank line that terminates an SSE
    record. Encoded as utf-8.
    """
    lines = []
    if frame_id is not None:
        lines.append(f"id: {frame_id}")
    lines.append(f"event: {event['type']}")
    lines.append(f"data: {json.dumps(event, ensure_ascii=False)}")
    return ("\n".join(lines) + "\n\n").encode("utf-8")
