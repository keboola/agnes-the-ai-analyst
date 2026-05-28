"""ChatManager tests — Task 5.1: create_session (+ 5.2: attach/send/cancel/crash).

Uses asyncio.run() per the project convention (no pytest-asyncio required).
See tests/test_chat_subprocess_provider.py for precedent.
"""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import duckdb
import pytest

from src.db import _ensure_schema

from app.chat.config import ChatConfig
from app.chat.manager import ChatManager, ConcurrencyCapHit
from app.chat.persistence import ChatRepository
from app.chat.types import SessionState, Surface
from app.chat.workdir import WorkdirManager


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
        config=ChatConfig(enabled=True, require_isolation=False, concurrency_per_user=2),
    )


# ---------------------------------------------------------------------------
# Task 5.1 tests
# ---------------------------------------------------------------------------

def test_create_session_persists(manager: ChatManager):
    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        assert s.id.startswith("chat_")
        assert s.surface == Surface.WEB

    asyncio.run(_run())


def test_create_session_disabled_raises(manager: ChatManager):
    """create_session raises RuntimeError when chat.enabled is False."""
    disabled_mgr = ChatManager(
        provider=manager._provider,
        workdir_mgr=manager._workdir_mgr,
        repo=manager._repo,
        config=ChatConfig(enabled=False),
    )

    async def _run():
        with pytest.raises(RuntimeError, match="chat.enabled is false"):
            await disabled_mgr.create_session(user_email="u@x", surface=Surface.WEB)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Fake helpers for Task 5.2 tests
# ---------------------------------------------------------------------------

class FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True


class FakeHandle:
    def __init__(self) -> None:
        self.pid = 1234
        self._lines: asyncio.Queue[bytes] = asyncio.Queue()
        self._stdin_buf: list[bytes] = []
        self.killed = False

    @property
    def stdin(self):
        outer = self

        class S:
            def write(self, b: bytes) -> None:
                outer._stdin_buf.append(b)

            async def drain(self) -> None:
                return None

        return S()

    @property
    def stdout(self):
        outer = self

        class O:
            async def readline(self) -> bytes:
                return await outer._lines.get()

        return O()

    @property
    def stderr(self):
        return self.stdout

    async def wait(self) -> int:
        # block until killed
        while not self.killed:
            await asyncio.sleep(0.01)
        return 137

    async def kill(self, *, grace_sec: float = 5.0) -> None:
        self.killed = True

    # Test helpers
    def emit(self, payload: dict) -> None:
        self._lines.put_nowait((json.dumps(payload) + "\n").encode())

    def emit_eof(self) -> None:
        self._lines.put_nowait(b"")


# ---------------------------------------------------------------------------
# Task 5.2 tests
# ---------------------------------------------------------------------------

def test_attach_pumps_tokens_to_ws(manager: ChatManager):
    async def _run():
        handle = FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)

        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(manager.attach(s.id, ws))
        await asyncio.sleep(0.05)
        handle.emit({"type": "token", "text": "Hi"})
        await asyncio.sleep(0.05)
        assert {"type": "token", "text": "Hi"} in ws.sent

        await manager.kill(s.id, reason="test_done")
        handle.emit_eof()
        await attach_task

    asyncio.run(_run())


def test_send_writes_to_stdin(manager: ChatManager):
    async def _run():
        handle = FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(manager.attach(s.id, ws))
        await asyncio.sleep(0.05)
        await manager.send_user_message(s.id, "hello")
        assert any(b'"hello"' in b for b in handle._stdin_buf)
        await manager.kill(s.id, reason="test_done")
        handle.emit_eof()
        await attach_task

    asyncio.run(_run())


def test_cancel_emits_synthetic_tool_result(manager: ChatManager):
    async def _run():
        handle = FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(manager.attach(s.id, ws))
        await asyncio.sleep(0.05)
        handle.emit({"type": "tool_call", "tool": "run_query", "args": {}})
        await asyncio.sleep(0.05)
        await manager.cancel(s.id)
        await asyncio.sleep(0.05)
        cancelled = [m for m in ws.sent if m.get("type") == "cancelled"]
        assert cancelled, "expected a {'type': 'cancelled'} frame after cancel"
        await manager.kill(s.id, reason="test_done")
        handle.emit_eof()
        await attach_task

    asyncio.run(_run())


def test_crash_respawns_with_notice(manager: ChatManager):
    async def _run():
        handles = [FakeHandle(), FakeHandle()]
        spawn_calls = iter(handles)

        async def fake_spawn(**kw):
            return next(spawn_calls)

        manager._provider.spawn = fake_spawn

        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(manager.attach(s.id, ws))
        await asyncio.sleep(0.05)
        # Simulate crash by signalling EOF and non-zero exit
        handles[0].emit_eof()
        handles[0].killed = True  # makes wait() return 137 immediately
        await asyncio.sleep(0.1)
        crashed = [m for m in ws.sent if m.get("type") == "error" and m.get("kind") == "subprocess_crashed"]
        assert crashed, "expected crash notice"
        ready = [m for m in ws.sent if m.get("type") == "ready"]
        assert ready, "expected ready frame after respawn"

        await manager.kill(s.id, reason="test_done")
        handles[1].emit_eof()
        await attach_task

    asyncio.run(_run())


def test_idle_reaper_kills_sessions_older_than_max_session_seconds(tmp_path):
    """Sessions that exceed ChatConfig.max_session_seconds get killed by the
    idle reaper independently of idle TTL.

    Before this knob was wired the value lived only in instance.yaml — operators
    set it and nothing happened.
    """
    from datetime import datetime, timedelta, timezone

    from app.chat.manager import LiveSession
    from app.chat.types import SessionState

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    workdir_mgr = _make_workdir_mgr(tmp_path, repo)
    provider = MagicMock()
    provider.spawn = AsyncMock()
    cfg = ChatConfig(
        enabled=True, require_isolation=False, concurrency_per_user=5,
        # Pin a tiny wallclock cap so the test is fast and deterministic.
        max_session_seconds=1,
        idle_ttl_seconds=10**9,  # disable idle path
    )
    mgr = ChatManager(
        provider=provider, workdir_mgr=workdir_mgr, repo=repo, config=cfg,
    )

    async def _run():
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        now = datetime.now(timezone.utc)
        ws = MagicMock()
        ws.send_json = AsyncMock()
        # Inject an "old" live session — started > max_session_seconds ago.
        mgr._live[s.id] = LiveSession(
            chat_id=s.id, user_email="u@x", state=SessionState.ACTIVE,
            handle=None, ws=ws,
            started_at=now - timedelta(seconds=5),
            last_activity=now,
        )

        await mgr._reap_once()  # single sweep; no sleep loop
        assert s.id not in mgr._live, "expected stale session to be killed"

    asyncio.run(_run())


def test_crash_respawn_does_not_accumulate_pump_tasks(manager: ChatManager):
    """Each respawn must replace (not append to) the per-session pump task.

    Pre-fix: every crash respawn created a new pump task and pushed it onto
    ``live.tasks`` while leaving the old (already-exited) one on the list.
    After N crashes the manager held N+1 pump tasks of which only the
    latest read from the live handle — a leak; tests can also see it
    grow unboundedly.
    """
    async def _run():
        handles = [FakeHandle(), FakeHandle(), FakeHandle()]
        spawn_calls = iter(handles)

        async def fake_spawn(**kw):
            return next(spawn_calls)

        manager._provider.spawn = fake_spawn

        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(manager.attach(s.id, ws))
        await asyncio.sleep(0.05)
        live = manager._live[s.id]
        initial_tasks = list(live.tasks)
        assert len(initial_tasks) == 2  # pump + wait
        assert live.current_pump is not None
        assert live.current_pump in initial_tasks

        # First crash → respawn
        handles[0].emit_eof()
        handles[0].killed = True
        await asyncio.sleep(0.1)
        # After respawn, still exactly two tasks (one wait + one pump),
        # not three.  current_pump points at the NEW pump.
        post_crash_tasks = [t for t in live.tasks if not t.done()]
        assert len(post_crash_tasks) == 2, (
            f"expected 2 live tasks after crash respawn, got {len(post_crash_tasks)}"
        )
        assert live.current_pump is not None
        assert live.current_pump in post_crash_tasks

        # Second crash → respawn again
        handles[1].emit_eof()
        handles[1].killed = True
        await asyncio.sleep(0.1)
        post_crash2_tasks = [t for t in live.tasks if not t.done()]
        assert len(post_crash2_tasks) == 2, (
            f"expected 2 live tasks after 2nd respawn, got {len(post_crash2_tasks)}"
        )

        # Cleanup
        try:
            await manager.kill(s.id, reason="test_done")
        except Exception:
            pass
        for h in handles:
            h.emit_eof()
        try:
            await asyncio.wait_for(attach_task, timeout=1.0)
        except asyncio.TimeoutError:
            attach_task.cancel()

    asyncio.run(_run())


def test_double_crash_dies_after_three(manager: ChatManager):
    handles = [FakeHandle(), FakeHandle(), FakeHandle(), FakeHandle()]
    spawn_calls = iter(handles)

    async def fake_spawn(**kw):
        return next(spawn_calls)

    manager._provider.spawn = fake_spawn

    async def go():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(manager.attach(s.id, ws))
        await asyncio.sleep(0.05)
        # First crash → respawn
        handles[0].emit_eof()
        handles[0].killed = True
        await asyncio.sleep(0.1)
        # Second crash → respawn
        handles[1].emit_eof()
        handles[1].killed = True
        await asyncio.sleep(0.1)
        # Third crash → DEAD
        handles[2].emit_eof()
        handles[2].killed = True
        await asyncio.sleep(0.1)
        # Should have three crashed notices, at least three ready notices
        crashed = [m for m in ws.sent if m.get("type") == "error" and m.get("kind") == "subprocess_crashed"]
        ready = [m for m in ws.sent if m.get("type") == "ready"]
        assert len(crashed) == 3, f"expected 3 crash notices, got {len(crashed)}"
        # First ready is the initial; respawns add 2 more
        assert len(ready) >= 3, f"expected >=3 ready, got {len(ready)}"
        # Session should now be DEAD
        live = manager._live.get(s.id)
        assert live is None or live.state == SessionState.DEAD

        # Cleanup
        try:
            await manager.kill(s.id, reason="test_done")
        except Exception:
            pass
        for h in handles[:3]:
            h.emit_eof()
        try:
            await asyncio.wait_for(attach_task, timeout=1.0)
        except asyncio.TimeoutError:
            attach_task.cancel()

    asyncio.run(go())
