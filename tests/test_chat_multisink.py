"""ChatManager multi-sink fan-out, stdin serialization, sender_email (Phase 5a).

Uses asyncio.run() per the project convention (see tests/test_chat_manager.py).
"""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import duckdb
import pytest

from src.db import _ensure_schema

from app.chat.config import ChatConfig
from app.chat.manager import ChatManager, LiveSession, SinkEntry
from app.chat.persistence import ChatRepository
from app.chat.types import SessionState, Surface
from app.chat.workdir import WorkdirManager


class FakeSink:
    """Duck-typed sink: records frames and a participant_email."""
    def __init__(self):
        self.frames = []
        self.closed = False

    async def send_json(self, frame):
        self.frames.append(frame)

    async def close(self):
        self.closed = True


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


@pytest.fixture
def manager(tmp_path: Path) -> ChatManager:
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
        config=ChatConfig(enabled=True, concurrency_per_user=5),
    )


def _attach_fake_live(manager: ChatManager, chat_id: str, user_email: str, sink) -> LiveSession:
    """Insert a LiveSession with a fake handle + one sink, bypassing _spawn_runner."""
    from datetime import datetime, timezone
    handle = MagicMock()
    handle.stdin = MagicMock()
    handle.stdin.drain = AsyncMock()
    live = LiveSession(
        chat_id=chat_id,
        user_email=user_email,
        state=SessionState.ACTIVE,
        handle=handle,
        started_at=datetime.now(timezone.utc),
        last_activity=datetime.now(timezone.utc),
        sinks=[SinkEntry(participant_email=user_email, sink=sink)],
    )
    manager._live[chat_id] = live
    return live


def test_pump_broadcasts_to_all_sinks_persist_once(manager: ChatManager):
    async def _run():
        s = await manager.create_session(user_email="o@x", surface=Surface.WEB)
        s1, s2 = FakeSink(), FakeSink()
        live = _attach_fake_live(manager, s.id, "o@x", s1)
        live.sinks.append(SinkEntry(participant_email="c@x", sink=s2))

        frames = [
            json.dumps({"type": "assistant_message", "content": "hello",
                        "tokens_in": 1, "tokens_out": 2}).encode() + b"\n",
            b"",  # EOF
        ]
        live.handle.stdout = MagicMock()
        live.handle.stdout.readline = AsyncMock(side_effect=frames)

        await manager._pump_subprocess_to_ws(live)

        # Both sinks received the assistant frame.
        assert any(f.get("type") == "assistant_message" for f in s1.frames)
        assert any(f.get("type") == "assistant_message" for f in s2.frames)
        # Persistence is singular: exactly one assistant row.
        msgs = manager._repo.list_messages(s.id)
        assert sum(1 for m in msgs if m.role == "assistant") == 1

    asyncio.run(_run())


def test_send_user_message_records_sender_email(manager: ChatManager):
    async def _run():
        s = await manager.create_session(user_email="o@x", surface=Surface.WEB)
        _attach_fake_live(manager, s.id, "o@x", FakeSink())
        await manager.send_user_message(s.id, "hi from collaborator", sender_email="c@x")
        rows = manager._repo.list_messages(s.id)
        user_rows = [m for m in rows if m.role == "user"]
        assert user_rows[-1].sender_email == "c@x"

    asyncio.run(_run())


def test_send_user_message_defaults_sender_to_owner(manager: ChatManager):
    async def _run():
        s = await manager.create_session(user_email="o@x", surface=Surface.WEB)
        _attach_fake_live(manager, s.id, "o@x", FakeSink())
        await manager.send_user_message(s.id, "hi")
        user_rows = [m for m in manager._repo.list_messages(s.id) if m.role == "user"]
        assert user_rows[-1].sender_email == "o@x"

    asyncio.run(_run())


def test_stdin_writes_are_serialized(manager: ChatManager):
    """Two concurrent sends must not interleave: each write+drain pair is
    atomic w.r.t. the event loop under _stdin_lock. We assert the bytes
    written are whole JSON lines (no partial-line interleaving)."""
    async def _run():
        s = await manager.create_session(user_email="o@x", surface=Surface.WEB)
        live = _attach_fake_live(manager, s.id, "o@x", FakeSink())
        written: list[bytes] = []
        live.handle.stdin.write = lambda b: written.append(b)

        async def slow_drain():
            await asyncio.sleep(0)  # yield, inviting interleave if unlocked

        live.handle.stdin.drain = slow_drain
        await asyncio.gather(
            manager.send_user_message(s.id, "AAAA", sender_email="a@x"),
            manager.send_user_message(s.id, "BBBB", sender_email="b@x"),
        )
        # Each written chunk is exactly one complete JSON line.
        for chunk in written:
            line = chunk.decode().rstrip("\n")
            json.loads(line)  # raises if a chunk is a partial line
        assert len(written) == 2

    asyncio.run(_run())


def test_add_sink_replays_history_before_appending(manager: ChatManager):
    async def _run():
        s = await manager.create_session(user_email="o@x", surface=Surface.WEB)
        manager._repo.append_message(session_id=s.id, role="user", content="q", sender_email="o@x")
        manager._repo.append_message(session_id=s.id, role="assistant", content="a")
        live = _attach_fake_live(manager, s.id, "o@x", FakeSink())

        late = FakeSink()
        await manager.add_sink(s.id, late, "c@x")

        # Late joiner saw the persisted history + a ready frame, and is now in sinks.
        assert "a" in [f.get("content") for f in late.frames if f.get("content")]
        assert any(f.get("type") == "ready" for f in late.frames)
        assert any(e.sink is late for e in live.sinks)

    asyncio.run(_run())
