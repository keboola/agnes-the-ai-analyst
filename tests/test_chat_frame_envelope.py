"""Tests for the monotonic frame envelope (wave-2F task 2).

Covers ``app.chat.frame_seq.FrameSequencer`` / ``stamp_frame`` directly, and
the emit path through ``ChatManager`` (attach → runner frame → WS sink) to
confirm the choke point (``ChatManager._broadcast``) actually stamps every
outbound frame.

Uses asyncio.run() per the project convention (no pytest-asyncio required) —
see tests/test_chat_manager.py.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import duckdb
import pytest

from src.db import _ensure_schema
from tests.chat_fakes import FakeHandle, FakeWS

from app.chat.config import ChatConfig
from app.chat.frame_seq import _SEQ_TTL_SEC, FrameSequencer, stamp_frame
from app.chat.manager import ChatManager, LiveSession, SinkEntry
from app.chat.persistence import ChatRepository
from app.chat.types import SessionState, Surface
from app.chat.workdir import WorkdirManager
from app.coordination.factory import reset_coordination_for_tests


@pytest.fixture(autouse=True)
def _reset_coordination():
    """The seq counter (``chat-seq:{chat_id}``) lives in the coordination
    backend singleton, which persists across tests unless reset — same
    rationale as tests/test_chat_manager.py's identical fixture."""
    reset_coordination_for_tests()
    yield
    reset_coordination_for_tests()


def _make_workdir_mgr(tmp_path: Path, repo: ChatRepository) -> WorkdirManager:
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "CLAUDE.md").write_text("d")
    return WorkdirManager(
        data_dir=tmp_path / "data",
        repo=repo,
        bundled_template_dir=bundled,
        server_url="https://example",
        agnes_version="0.55.0",
        get_marketplace_sha=lambda: "sha-1",
        get_template_status=lambda: None,
    )


def _make_manager(tmp_path: Path) -> ChatManager:
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    workdir_mgr = _make_workdir_mgr(tmp_path, repo)
    provider = MagicMock()
    provider.spawn = AsyncMock()
    return ChatManager(
        provider=provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        config=ChatConfig(enabled=True, concurrency_per_user=2),
    )


# ---------------------------------------------------------------------------
# FrameSequencer / stamp_frame — unit level
# ---------------------------------------------------------------------------


def test_seq_increases_monotonically_per_session():
    seq = FrameSequencer("chat_a")
    assert seq.next_seq() == 1
    assert seq.next_seq() == 2
    assert seq.next_seq() == 3


def test_seq_independent_across_sessions():
    a = FrameSequencer("chat_a")
    b = FrameSequencer("chat_b")
    assert a.next_seq() == 1
    assert a.next_seq() == 2
    # A different chat_id's counter is untouched by chat_a's increments.
    assert b.next_seq() == 1


def test_seq_persists_across_simulated_respawn():
    """The counter must live in the coordination backend, not on the
    FrameSequencer instance — a brand-new instance for the SAME chat_id
    (as a fresh process would construct after a crash respawn or
    cross-gateway resume) must continue the sequence, not restart at 1.

    Deliberately does NOT call reset_coordination_for_tests() between the
    two instances — that would defeat the point of this test, which is
    that recreating the *sequencer* (not the backend) preserves state.
    """
    seq1 = FrameSequencer("chat_respawn")
    assert seq1.next_seq() == 1
    assert seq1.next_seq() == 2

    # Simulate a respawn: a fresh instance, same chat_id.
    seq2 = FrameSequencer("chat_respawn")
    assert seq2.next_seq() == 3
    assert seq2.next_seq() == 4


def test_stamp_frame_adds_seq_and_id_and_mutates_in_place():
    frame = {"type": "token", "text": "hi"}
    stamped = stamp_frame("chat_stamp", frame)
    assert stamped is frame
    assert frame["seq"] == 1
    assert frame["id"] == "chat_stamp:1"

    frame2 = stamp_frame("chat_stamp", {"type": "token", "text": "yo"})
    assert frame2["seq"] == 2
    assert frame2["id"] == "chat_stamp:2"


# ---------------------------------------------------------------------------
# Emit path — through ChatManager (attach → runner frame → WS sink)
# ---------------------------------------------------------------------------


def test_emit_path_frame_carries_seq_and_id(tmp_path: Path):
    manager = _make_manager(tmp_path)

    async def _run():
        handle = FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)

        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(manager.attach(s.id, ws))
        await asyncio.sleep(0.05)
        handle.emit({"type": "token", "text": "Hi"})
        await asyncio.sleep(0.05)

        tokens = [m for m in ws.sent if m.get("type") == "token"]
        assert tokens, "expected at least one token frame on the sink"
        frame = tokens[-1]
        assert isinstance(frame.get("seq"), int) and frame["seq"] >= 1
        assert frame.get("id") == f"{s.id}:{frame['seq']}"

        await manager.kill(s.id, reason="test_done")
        handle.emit_eof()
        await attach_task

    asyncio.run(_run())


def test_emit_path_seq_increases_across_frames(tmp_path: Path):
    """Multiple frames in the same session get strictly increasing seq —
    the ready frame from _seat_sink, then two token frames from the pump."""
    manager = _make_manager(tmp_path)

    async def _run():
        handle = FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)

        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(manager.attach(s.id, ws))
        await asyncio.sleep(0.05)
        handle.emit({"type": "token", "text": "a"})
        await asyncio.sleep(0.02)
        handle.emit({"type": "token", "text": "b"})
        await asyncio.sleep(0.05)

        seqs = [m["seq"] for m in ws.sent if isinstance(m.get("seq"), int)]
        assert len(seqs) >= 2
        # Strictly increasing, no repeats — every stamped frame this sink
        # observed (ready + tokens) got its own seq.
        assert seqs == sorted(set(seqs))
        assert len(seqs) == len(set(seqs))

        await manager.kill(s.id, reason="test_done")
        handle.emit_eof()
        await attach_task

    asyncio.run(_run())


def test_emit_path_seq_independent_across_sessions(tmp_path: Path):
    """Two different live sessions' frames must not share a seq counter."""
    manager = _make_manager(tmp_path)

    async def _run():
        handle_a = FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle_a)
        s_a = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws_a = FakeWS()
        attach_a = asyncio.create_task(manager.attach(s_a.id, ws_a))
        await asyncio.sleep(0.05)
        handle_a.emit({"type": "token", "text": "a1"})
        await asyncio.sleep(0.05)

        handle_b = FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle_b)
        s_b = await manager.create_session(user_email="v@y", surface=Surface.WEB)
        ws_b = FakeWS()
        attach_b = asyncio.create_task(manager.attach(s_b.id, ws_b))
        await asyncio.sleep(0.05)
        handle_b.emit({"type": "token", "text": "b1"})
        await asyncio.sleep(0.05)

        ids_a = {m["id"] for m in ws_a.sent if "id" in m}
        ids_b = {m["id"] for m in ws_b.sent if "id" in m}
        assert ids_a.isdisjoint(ids_b)
        assert all(i.startswith(f"{s_a.id}:") for i in ids_a)
        assert all(i.startswith(f"{s_b.id}:") for i in ids_b)

        await manager.kill(s_a.id, reason="test_done")
        handle_a.emit_eof()
        await attach_a
        await manager.kill(s_b.id, reason="test_done")
        handle_b.emit_eof()
        await attach_b

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# _broadcast concurrency — per-session lock serializes stamp+send
# ---------------------------------------------------------------------------


def _make_bare_live(chat_id: str) -> LiveSession:
    """A minimal LiveSession for exercising ``ChatManager._broadcast``
    directly, without going through the full spawn/attach lifecycle (which
    ``_broadcast`` doesn't need — it only reads/mutates
    ``chat_id``/``sinks``/``state``)."""
    now = datetime.now(timezone.utc)
    return LiveSession(
        chat_id=chat_id,
        user_email="u@x",
        state=SessionState.ACTIVE,
        handle=None,
        started_at=now,
        last_activity=now,
    )


class _SlowSink:
    """Records the ``seq`` of every frame it receives, in arrival order.

    The FIRST call to ``send_json`` yields control for a while (simulating
    a slow WS write); every subsequent call returns immediately. Fired
    concurrently against ``ChatManager._broadcast`` for the SAME session,
    this is exactly the race the Important finding describes: the first
    ``_broadcast`` call stamps seq=1 and then blocks on a slow send, giving
    every other concurrently-scheduled ``_broadcast`` call a chance to stamp
    a higher seq and finish first — unless the per-session lock forces them
    to queue behind the slow one instead.
    """

    def __init__(self) -> None:
        self.received_seqs: list[int] = []
        self._first = True

    async def send_json(self, data: dict) -> None:
        if self._first:
            self._first = False
            await asyncio.sleep(0.05)
        self.received_seqs.append(data["seq"])


def test_broadcast_serializes_stamp_and_send_under_concurrency(tmp_path: Path):
    """N concurrent ``_broadcast`` calls for one LiveSession must deliver
    frames to the sink in strictly increasing seq order — i.e. seq
    assignment order always matches delivery order, even when the first
    call's send is slow and later calls' sends are instant. This is the
    invariant the future replay mechanism (wave-2F task 3) depends on."""
    manager = _make_manager(tmp_path)
    live = _make_bare_live("concurrent-session-1")
    sink = _SlowSink()
    live.sinks = [SinkEntry(participant_email="u@x", sink=sink)]

    n = 8

    async def _run():
        await asyncio.gather(*[manager._broadcast(live, {"type": "token", "n": i}) for i in range(n)])

    asyncio.run(_run())

    assert sink.received_seqs == list(range(1, n + 1)), (
        "frames must arrive in strictly increasing seq order == send order"
    )


# ---------------------------------------------------------------------------
# seq counter TTL — must outlive a session's full (possibly paused) lifetime
# ---------------------------------------------------------------------------


def test_seq_ttl_exceeds_paused_plus_active_session_lifetime():
    """The seq counter's TTL must comfortably outlive
    ``paused_ttl_seconds + max_session_seconds`` under default config —
    otherwise a session paused close to (or an operator-configured session
    living longer than) the old 6h TTL would see the counter expire and
    reset to seq=1 mid-life, producing a duplicate seq/id."""
    cfg = ChatConfig()
    assert _SEQ_TTL_SEC >= cfg.paused_ttl_seconds + cfg.max_session_seconds
    # The old value (6h) was sized only for the ACTIVE half of a session's
    # life and did not survive a single default paused_ttl_seconds (7 days).
    assert _SEQ_TTL_SEC >= 8 * 24 * 3600


def test_frame_sequencer_passes_hardened_ttl_to_coordination_incr(monkeypatch):
    """``FrameSequencer.next_seq`` must hand the hardened ``_SEQ_TTL_SEC``
    (not some smaller/legacy value) to ``coordination().incr`` — simulates a
    long-lived session by asserting the TTL actually used, rather than
    relying on wall-clock time passing in the test."""
    captured: dict[str, int] = {}

    class _FakeCoordination:
        def incr(self, key: str, *, amount: int = 1, ttl_s: int) -> int:
            captured["ttl_s"] = ttl_s
            return amount or 1

    monkeypatch.setattr("app.chat.frame_seq.coordination", lambda: _FakeCoordination())

    FrameSequencer("chat_longlived").next_seq()

    assert captured["ttl_s"] == _SEQ_TTL_SEC
    assert captured["ttl_s"] >= ChatConfig().paused_ttl_seconds
