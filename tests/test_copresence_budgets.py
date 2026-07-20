"""Co-presence budget, rate-limit, and teardown tests (SR-5, SR-9, SR-10, SR-11).

Covers Tasks 11-13: co-aware spawn (no seed fallback), per-sender caps,
zero-frames-after-leave gate.

Uses asyncio.run() per project convention (no pytest-asyncio).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
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
from app.coordination.factory import coordination, reset_coordination_for_tests


@pytest.fixture(autouse=True)
def _reset_coordination():
    """Rate/quota counters (chat-msgs:.../chat-tokens:...) now live in the
    coordination-backend singleton, which persists across tests in this
    file that reuse the same owner/collaborator emails — reset it so one
    test's message-rate/token usage never bleeds into another's."""
    reset_coordination_for_tests()
    yield
    reset_coordination_for_tests()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_repo(conn=None):
    if conn is None:
        conn = duckdb.connect(":memory:")
        _ensure_schema(conn)
    return ChatRepository(conn)


def _make_workdir_mgr(tmp_path: Path, repo) -> WorkdirManager:
    bundled = tmp_path / "bundled"
    bundled.mkdir(parents=True, exist_ok=True)
    (bundled / ".claude").mkdir(parents=True, exist_ok=True)
    (bundled / "CLAUDE.md").write_text("d")
    return WorkdirManager(
        data_dir=tmp_path / "data",
        repo=repo,
        bundled_template_dir=bundled,
        server_url="https://example.com",
        agnes_version="0.0.0-test",
        get_marketplace_sha=lambda: "sha",
        get_template_status=lambda: None,
    )


def _make_manager(tmp_path: Path) -> ChatManager:
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = _make_repo(conn)
    wdm = _make_workdir_mgr(tmp_path, repo)
    provider = MagicMock()
    provider.spawn = AsyncMock()
    return ChatManager(
        provider=provider,
        workdir_mgr=wdm,
        repo=repo,
        config=ChatConfig(enabled=True, concurrency_per_user=5),
    )


class _FakeSink:
    """Fake WebSocket sink that records frames."""

    def __init__(self):
        self.frames: list[dict] = []
        self.closed = False

    async def send_json(self, frame):
        self.frames.append(frame)

    async def close(self):
        self.closed = True


class _FakeHandle:
    """Fake SandboxHandle with in-memory stdin/stdout pipes."""

    def __init__(self):
        self.killed = False
        self.pid = 7
        self.sandbox_id = "fake-co-sbx"  # SandboxHandle protocol (v73 refs)
        self._stdin_buf: list[bytes] = []
        self._stdout_buf: asyncio.Queue = None  # lazily set in async context
        self.stdin = self
        self.syncs_workspace = True

    def write(self, data: bytes):
        self._stdin_buf.append(data)

    async def drain(self):
        pass

    @property
    def stdout(self):
        return self

    async def readline(self):
        return b""

    async def wait(self):
        return 0

    async def kill(self, grace_sec: float = 5.0):
        self.killed = True


# ---------------------------------------------------------------------------
# co_manager fixture — used by Tasks 11-13
# ---------------------------------------------------------------------------


@pytest.fixture
def co_manager(tmp_path):
    """ChatManager + a co-session. Returns (mgr, co_session, session_dir)."""
    mgr = _make_manager(tmp_path)
    repo = mgr._repo
    s0 = repo.create_session(user_email="a@example.com", surface=Surface.WEB)
    co = repo.fork_session_as_co_session(
        s0.id,
        owner_email="a@example.com",
        owner_user_id="ua",
        invitee_email="b@example.com",
        invitee_user_id="ub",
    )
    session_dir = tmp_path / "session_dir"
    session_dir.mkdir(parents=True, exist_ok=True)
    return mgr, co, session_dir


# ---------------------------------------------------------------------------
# co_manager_live fixture — used by Tasks 12-13
# ---------------------------------------------------------------------------


@pytest.fixture
def co_manager_live(tmp_path):
    """ChatManager with a fake live co-session pre-inserted into _live.

    Returns (mgr, live, owner_email, collab_email).
    Uses rate_messages_per_hour=5 so per-sender rate tests can pre-fill the window.
    """
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = _make_repo(conn)
    wdm = _make_workdir_mgr(tmp_path, repo)
    provider = MagicMock()
    provider.spawn = AsyncMock(side_effect=lambda **kw: _FakeHandle())
    mgr = ChatManager(
        provider=provider,
        workdir_mgr=wdm,
        repo=repo,
        config=ChatConfig(enabled=True, concurrency_per_user=5, rate_messages_per_hour=5),
    )
    repo = mgr._repo
    s0 = repo.create_session(user_email="a@example.com", surface=Surface.WEB)
    co = repo.fork_session_as_co_session(
        s0.id,
        owner_email="a@example.com",
        owner_user_id="ua",
        invitee_email="b@example.com",
        invitee_user_id="ub",
    )
    owner_email = "a@example.com"
    collab_email = "b@example.com"

    handle = _FakeHandle()
    owner_sink = _FakeSink()
    collab_sink = _FakeSink()

    live = LiveSession(
        chat_id=co.id,
        user_email=owner_email,
        state=SessionState.ACTIVE,
        handle=handle,
        started_at=datetime.now(timezone.utc),
        last_activity=datetime.now(timezone.utc),
        sinks=[
            SinkEntry(participant_email=owner_email, sink=owner_sink),
            SinkEntry(participant_email=collab_email, sink=collab_sink),
        ],
        participant_emails=[owner_email, collab_email],
    )
    mgr._live[co.id] = live
    return mgr, live, owner_email, collab_email


# ---------------------------------------------------------------------------
# Task 11 — co-aware spawn: co JWT, no seed fallback (SR-5)
# ---------------------------------------------------------------------------


def test_co_session_spawn_uses_co_jwt_no_seed_fallback(monkeypatch, co_manager, tmp_path):
    mgr, co_session, session_dir = co_manager
    monkeypatch.setenv("AGNES_SESSION_JWT_SEED", "SEED")

    def boom(*a, **k):
        raise ValueError("nope")  # force mint_co_session_jwt to fail -> must re-raise, never SEED

    import app.auth.access as access_mod

    monkeypatch.setattr(access_mod, "mint_co_session_jwt", boom)

    async def _run():
        with pytest.raises(ValueError):
            await mgr._spawn_runner(co_session, session_dir)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Task 12 — per-sender rate limiting (SR-10)
# ---------------------------------------------------------------------------


def test_capped_collaborator_rejected_owner_passes(co_manager_live):
    mgr, live, owner, collab = co_manager_live
    # Config already has rate_messages_per_hour=5 (set in fixture). The
    # window now lives in the coordination backend keyed by sender + UTC
    # hour bucket (see ChatManager._msg_window_key) — pre-fill collab's
    # current-hour counter to the cap instead of poking an in-process deque.
    coordination().incr(mgr._msg_window_key(collab), amount=5, ttl_s=3600)

    async def _run():
        with pytest.raises(RuntimeError):
            await mgr.send_user_message(live.chat_id, "hi", sender_email=collab)
        # owner turn must pass (its own window is empty)
        await mgr.send_user_message(live.chat_id, "hi", sender_email=owner)

    asyncio.run(_run())


def test_active_count_counts_every_participant(co_manager_live):
    mgr, live, owner, collab = co_manager_live
    assert mgr._active_count_for_user(owner) >= 1
    assert mgr._active_count_for_user(collab) >= 1


# ---------------------------------------------------------------------------
# Task 13 — leave teardown: SR-9 zero-frames-after-leave gate
# ---------------------------------------------------------------------------


def test_leaver_sink_receives_zero_frames_after_leave(co_manager_live):
    mgr, live, owner, collab = co_manager_live
    collab_sink = next(s.sink for s in live.sinks if s.participant_email == collab)
    collab_sink.frames.clear()

    async def _run():
        await mgr.leave_session(live.chat_id, collab)  # stamps left_at + removes+closes sink
        await mgr._broadcast(live, {"type": "assistant_message", "content": "x"})
        assert collab_sink.frames == []
        assert all(s.participant_email != collab for s in live.sinks)

    asyncio.run(_run())


def test_add_sink_rejects_non_participant(co_manager_live):
    mgr, live, owner, collab = co_manager_live

    class _Sink:
        frames: list = []

        async def send_json(self, f):
            self.frames.append(f)

        async def close(self):
            pass

    async def _run():
        with pytest.raises(PermissionError):
            await mgr.add_sink(live.chat_id, _Sink(), "stranger@example.com")

    asyncio.run(_run())


class _BlockingHandle:
    """Handle whose wait() blocks until kill(), then returns a non-zero rc —
    so a parked _wait_for_exit_and_respawn would treat the kill as a crash."""

    def __init__(self):
        self._killed = asyncio.Event()
        self.stdin = self
        self.syncs_workspace = True
        self.pid = 99
        self.sandbox_id = "fake-blocking-sbx"  # SandboxHandle protocol (v73 refs)

    def write(self, data: bytes):
        pass

    async def drain(self):
        pass

    @property
    def stdout(self):
        return self

    async def readline(self):
        return b""

    async def wait(self):
        await self._killed.wait()
        return 137

    async def kill(self, grace_sec: float = 5.0):
        self._killed.set()


def test_leave_does_not_double_respawn(co_manager_live, monkeypatch, tmp_path):
    """Regression: a collaborator leave triggers exactly ONE intentional
    respawn. The running crash-respawn wait task must be cancelled before the
    old handle is killed, otherwise it sees the kill as a crash and respawns a
    second time (double-respawn race → multiple concurrent runners)."""
    mgr, live, owner, collab = co_manager_live
    live.handle = _BlockingHandle()

    spawned: list = []

    async def fake_spawn(session, session_dir):
        h = _BlockingHandle()
        spawned.append(h)
        return h

    monkeypatch.setattr(mgr, "_spawn_runner", fake_spawn)
    monkeypatch.setattr(
        mgr._workdir_mgr,
        "prepare_ephemeral_session_dir",
        lambda *a, **k: tmp_path / "co_dir",
    )

    async def _run():
        # Mimic attach(): a live crash-respawn wait task parked on handle.wait().
        wait_task = asyncio.create_task(mgr._wait_for_exit_and_respawn(live, tmp_path / "orig_dir"))
        live.current_wait = wait_task
        live.tasks = [wait_task]
        await asyncio.sleep(0)  # let it park on the blocking wait()

        await mgr.leave_session(live.chat_id, collab)
        # Give any errant crash-driven respawn a chance to fire if the bug exists.
        await asyncio.sleep(0.05)

        # The original wait task must have been cancelled by the respawn.
        assert wait_task.done()
        # Teardown so the fresh wait task / pump don't leak.
        live.state = SessionState.DEAD
        if live.handle is not None:
            await live.handle.kill()
        for t in list(live.tasks):
            t.cancel()
        await asyncio.gather(*live.tasks, return_exceptions=True)

    asyncio.run(_run())

    assert len(spawned) == 1, f"expected exactly one respawn, got {len(spawned)}"
    assert live.crash_count == 0
