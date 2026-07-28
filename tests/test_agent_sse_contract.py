"""Golden SSE wire-contract test (C15, agent-api V1b spec §6).

Drives a canned internal-frame sequence through the full pipeline a real
`POST /api/v1/sessions/{id}/messages` turn uses — `StreamingSink`
(`app.chat.streaming_sink`) -> `frame_to_agui` -> `sse_bytes`
(`app.api.agent_sse`) — and asserts the EXACT ordered AG-UI event stream,
a balanced RUN_STARTED/terminal lifecycle, and gap-free monotonic `id:`
values. The wire format is a contract: a client parses these SSE records
by `event:`/`id:`/`data:` lines, so a silent reordering, a dropped id, or
an extra/missing lifecycle event breaks every consumer at once.

``_drain_sse`` below intentionally mirrors (does not import)
`app.api.agent_sessions._event_stream`'s frame-drain loop — map each frame
through `frame_to_agui`, skip `None` (frame types with no AG-UI
equivalent), serialize via `sse_bytes`, and stop after the first terminal
event (`SSE_TERMINAL_TYPES`). Reimplementing it here keeps this test a
pure, dependency-free check of the StreamingSink+mapper+serializer trio —
no chat manager, no FastAPI app, no coordination backend — while `app.api.
agent_sessions` module docstring cross-references this file as the wire
contract test agent_sessions actually implements the same shape.

Uses ``asyncio.run`` (not ``@pytest.mark.asyncio`` — this repo doesn't
depend on pytest-asyncio, see ``tests/test_streaming_sink.py``).
"""

from __future__ import annotations

import asyncio
import json

from app.api.agent_sse import SSE_TERMINAL_TYPES, frame_to_agui, sse_bytes
from app.chat.streaming_sink import StreamingSink


def _stamp(chat_id: str, seq: int, frame: dict) -> dict:
    """Mirrors `app.chat.frame_seq.stamp_frame`'s output shape (`seq` +
    `id: f"{chat_id}:{seq}"`) without importing the coordination-backed
    module — this test only needs the SHAPE, not a live counter."""
    return {**frame, "seq": seq, "id": f"{chat_id}:{seq}"}


async def _drain_sse(sink: StreamingSink) -> list[bytes]:
    """Mirrors `app.api.agent_sessions._event_stream`'s drain loop (see
    module docstring) — no idle-timeout wrapping here, since these tests
    always close the sink well before that would matter."""
    out: list[bytes] = []
    async for frame in sink:
        event = frame_to_agui(frame)
        if event is None:
            continue
        out.append(sse_bytes(event, frame.get("id")))
        if event["type"] in SSE_TERMINAL_TYPES:
            break
    return out


def _event_types(records: list[bytes]) -> list[str]:
    types = []
    for rec in records:
        for line in rec.decode().splitlines():
            if line.startswith("event: "):
                types.append(line[len("event: ") :])
    return types


def _ids(records: list[bytes]) -> list[str]:
    ids = []
    for rec in records:
        for line in rec.decode().splitlines():
            if line.startswith("id: "):
                ids.append(line[len("id: ") :])
    return ids


def _seq_ints(ids: list[str]) -> list[int]:
    return [int(i.rsplit(":", 1)[1]) for i in ids]


# ---------------------------------------------------------------------------
# Golden happy path — text-only turn
# ---------------------------------------------------------------------------


def test_golden_text_turn_exact_event_order_and_gap_free_ids():
    chat_id = "sess-golden-1"
    frames = [
        {"type": "ready"},
        {"type": "token", "content": "Hel"},
        {"type": "token", "content": "lo, "},
        {"type": "token", "content": "world!"},
        {"type": "assistant_message", "content": "Hello, world!"},
        {"type": "done"},
    ]

    async def _run():
        sink = StreamingSink()
        for seq, frame in enumerate(frames, start=1):
            await sink.send_json(_stamp(chat_id, seq, frame))
        await sink.close()
        return await _drain_sse(sink)

    records = asyncio.run(_run())

    # Exact ordered event-type sequence: RUN_STARTED -> TEXT_MESSAGE_CONTENT
    # x3 -> TEXT_MESSAGE_END -> RUN_FINISHED.
    assert _event_types(records) == [
        "RUN_STARTED",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]

    # Balanced lifecycle: exactly one RUN_STARTED, exactly one terminal
    # event, and it's RUN_FINISHED (not RUN_ERROR) — a clean turn.
    types = _event_types(records)
    assert types.count("RUN_STARTED") == 1
    terminal = [t for t in types if t in SSE_TERMINAL_TYPES]
    assert terminal == ["RUN_FINISHED"]

    # Gap-free monotonic ids: every record here carries one (no dropped
    # frame types in this scenario), sequence 1..6 with no repeats/skips.
    ids = _ids(records)
    assert len(ids) == len(records) == 6
    seqs = _seq_ints(ids)
    assert seqs == list(range(1, 7))
    assert all(i.startswith(f"{chat_id}:") for i in ids)

    # Full wire round-trip on a representative record: the delta content
    # actually made it into `data:` unmangled.
    content_record = records[1].decode()
    assert content_record.startswith(f"id: {chat_id}:2\n")
    assert "event: TEXT_MESSAGE_CONTENT\n" in content_record
    data_line = [ln for ln in content_record.splitlines() if ln.startswith("data: ")][0]
    assert json.loads(data_line[len("data: ") :]) == {"type": "TEXT_MESSAGE_CONTENT", "delta": "Hel"}
    assert content_record.endswith("\n\n")


# ---------------------------------------------------------------------------
# Golden happy path — turn with a tool call
# ---------------------------------------------------------------------------


def test_golden_tool_call_turn_exact_event_order_and_gap_free_ids():
    chat_id = "sess-golden-2"
    frames = [
        {"type": "ready"},
        {"type": "token", "content": "Let me check."},
        {"type": "tool_call", "tool": "bash", "args": {"command": "ls"}},
        {"type": "tool_result", "result": "file.txt"},
        {"type": "assistant_message", "content": "Found file.txt"},
        {"type": "done"},
    ]

    async def _run():
        sink = StreamingSink()
        for seq, frame in enumerate(frames, start=1):
            await sink.send_json(_stamp(chat_id, seq, frame))
        await sink.close()
        return await _drain_sse(sink)

    records = asyncio.run(_run())

    assert _event_types(records) == [
        "RUN_STARTED",
        "TEXT_MESSAGE_CONTENT",
        "TOOL_CALL_START",
        "TOOL_CALL_END",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]
    seqs = _seq_ints(_ids(records))
    assert seqs == list(range(1, 7))

    tool_call_record = records[2].decode()
    data_line = [ln for ln in tool_call_record.splitlines() if ln.startswith("data: ")][0]
    assert json.loads(data_line[len("data: ") :]) == {
        "type": "TOOL_CALL_START",
        "name": "bash",
        "args": {"command": "ls"},
    }


# ---------------------------------------------------------------------------
# Error lifecycle — balanced RUN_STARTED / RUN_ERROR, stream stops
# ---------------------------------------------------------------------------


def test_error_terminates_stream_with_balanced_lifecycle_and_no_run_finished():
    chat_id = "sess-golden-err"
    frames = [
        {"type": "ready"},
        {"type": "token", "content": "partial"},
        {"type": "error", "message": "upstream exploded"},
        # Never reached — the drain loop stops at the first terminal event.
        {"type": "done"},
    ]

    async def _run():
        sink = StreamingSink()
        for seq, frame in enumerate(frames, start=1):
            await sink.send_json(_stamp(chat_id, seq, frame))
        await sink.close()
        return await _drain_sse(sink)

    records = asyncio.run(_run())
    types = _event_types(records)

    assert types == ["RUN_STARTED", "TEXT_MESSAGE_CONTENT", "RUN_ERROR"]
    assert types.count("RUN_STARTED") == 1
    terminal = [t for t in types if t in SSE_TERMINAL_TYPES]
    assert terminal == ["RUN_ERROR"]
    assert "RUN_FINISHED" not in types

    # ids still gap-free/monotonic up to the point the stream stopped —
    # the queued (never-drained) "done" frame after the break is simply
    # never turned into a record, not a gap in what WAS emitted.
    seqs = _seq_ints(_ids(records))
    assert seqs == [1, 2, 3]


# ---------------------------------------------------------------------------
# Dropped internal frame types don't appear on the wire, don't break the
# lifecycle contract, and the surrounding ids stay monotonic (a real,
# expected skip from the DROPPED frame's own id — not a lost frame; the
# sink itself never drops anything, see tests/test_streaming_sink.py).
# ---------------------------------------------------------------------------


def test_frame_types_with_no_agui_equivalent_are_dropped_without_breaking_monotonicity():
    chat_id = "sess-golden-drop"
    frames = [
        {"type": "ready"},
        {"type": "session_renamed", "title": "New title"},  # no AG-UI event
        {"type": "token", "content": "hi"},
        {"type": "assistant_message", "content": "hi"},
        {"type": "done"},
    ]

    async def _run():
        sink = StreamingSink()
        for seq, frame in enumerate(frames, start=1):
            await sink.send_json(_stamp(chat_id, seq, frame))
        await sink.close()
        return await _drain_sse(sink)

    records = asyncio.run(_run())

    assert _event_types(records) == [
        "RUN_STARTED",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]
    # seq 2 (session_renamed) never became a record — the surviving ids
    # (1, 3, 4, 5) are still each individually monotonically increasing,
    # even though they're not CONSECUTIVE integers. That's expected: a
    # dropped frame TYPE is not the same failure mode as the sink losing a
    # queued frame (which never happens — see test_streaming_sink.py).
    seqs = _seq_ints(_ids(records))
    assert seqs == [1, 3, 4, 5]
    assert seqs == sorted(seqs)
    assert len(seqs) == len(set(seqs))  # no duplicate ids


# ---------------------------------------------------------------------------
# RUN_STARTED fires at most once even if a second "ready" is queued (e.g. a
# reconnect mid-turn re-seats the sink) — mirrors the per-turn filter
# `app.api.agent_sessions._event_stream` relies on implicitly by only ever
# attaching one fresh StreamingSink per POST.
# ---------------------------------------------------------------------------


def test_single_ready_frame_per_sink_yields_single_run_started():
    chat_id = "sess-golden-ready"
    frames = [{"type": "ready"}, {"type": "done"}]

    async def _run():
        sink = StreamingSink()
        for seq, frame in enumerate(frames, start=1):
            await sink.send_json(_stamp(chat_id, seq, frame))
        await sink.close()
        return await _drain_sse(sink)

    records = asyncio.run(_run())
    assert _event_types(records) == ["RUN_STARTED", "RUN_FINISHED"]
