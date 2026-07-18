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
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import duckdb
import pytest

from src.db import _ensure_schema
from tests.chat_fakes import FakeHandle, FakeWS

from app.chat.config import ChatConfig
from app.chat.frame_seq import FrameSequencer, stamp_frame
from app.chat.manager import ChatManager
from app.chat.persistence import ChatRepository
from app.chat.types import Surface
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
