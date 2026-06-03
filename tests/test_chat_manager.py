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
from app.chat.manager import ChatManager, ConcurrencyCapHit, SinkEntry
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
        config=ChatConfig(enabled=True, concurrency_per_user=2),
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


def test_create_session_web_archives_prior_empty(manager: ChatManager):
    """Clicking '+ New chat' repeatedly should never accumulate orphan
    Untitled-chat rows. create_session on the WEB surface soft-archives
    every empty session this user has, except the just-created one."""

    async def _run():
        a = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        b = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        # `a` has zero messages → should be archived by `b`'s create.
        ar = manager._repo.get_session(a.id)
        br = manager._repo.get_session(b.id)
        assert ar is not None and ar.archived is True
        assert br is not None and br.archived is False

    asyncio.run(_run())


def test_create_session_web_does_not_archive_sessions_with_messages(
    manager: ChatManager,
):
    async def _run():
        a = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        manager._repo.append_message(session_id=a.id, role="user", content="hi")
        _ = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ar = manager._repo.get_session(a.id)
        assert ar is not None and ar.archived is False

    asyncio.run(_run())


def test_create_session_slack_dm_does_not_run_empty_gc(manager: ChatManager):
    """The empty-session GC is web-only — Slack DM/thread surfaces
    de-dupe via channel/thread id at the manager layer and their
    "empty" sessions are intentionally kept for re-attach."""

    async def _run():
        # First Slack DM session, no messages.
        a = await manager.create_session(
            user_email="u@x", surface=Surface.SLACK_DM, slack_channel_id="C1",
        )
        # Create a WEB session for the same user — must not touch `a`.
        _ = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ar = manager._repo.get_session(a.id)
        assert ar is not None and ar.archived is False

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

def test_spawn_sets_agnes_server_not_agnes_api(manager: ChatManager, tmp_path, monkeypatch):
    """The runner env must carry AGNES_SERVER (the var the CLI reads) sourced
    from SERVER_URL — not the dead AGNES_API the CLI ignores."""
    monkeypatch.setenv("SERVER_URL", "https://chat.example.com")
    monkeypatch.setattr("app.auth.access.mint_session_jwt", lambda *a, **k: "tok")

    captured = {}

    async def fake_spawn(**kw):
        captured.update(kw)
        return FakeHandle()

    manager._provider.spawn = fake_spawn

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        sess = manager._repo.get_session(s.id)
        await manager._spawn_runner(sess, tmp_path)

    asyncio.run(_run())

    env = captured["env"]
    assert env["AGNES_SERVER"] == "https://chat.example.com"
    assert "AGNES_API" not in env
    assert env["AGNES_TOKEN"] == "tok"


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
        # Synthetic tool_result must be emitted before the `cancelled` frame
        # so the agent sees the cancellation in its conversation history.
        synthetic = [
            m for m in ws.sent
            if m.get("type") == "tool_result"
            and isinstance(m.get("result"), dict)
            and m["result"].get("cancelled") is True
        ]
        assert synthetic, (
            f"expected synthetic tool_result with cancelled=true; got {ws.sent}"
        )
        # And it must be persisted so crash-respawn replay sees it too.
        msgs = manager._repo.list_messages(s.id)
        persisted_cancels = [
            m for m in msgs
            if m.tool_calls and any(
                isinstance(tc, dict) and tc.get("cancelled") is True
                for tc in m.tool_calls
            )
        ]
        assert persisted_cancels, "expected persisted cancel marker in chat_messages"
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


def test_send_user_message_rejects_when_rate_limit_exceeded(tmp_path):
    """Per-user sliding-window rate limit: more than rate_messages_per_hour
    messages within the last hour from one user gets refused.
    """
    from datetime import datetime, timezone

    from app.chat.manager import LiveSession
    from app.chat.types import SessionState

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    workdir_mgr = _make_workdir_mgr(tmp_path, repo)
    provider = MagicMock()
    provider.spawn = AsyncMock()
    cfg = ChatConfig(
        enabled=True, concurrency_per_user=5,
        rate_messages_per_hour=3,
        daily_anthropic_spend_usd=10**6,
        max_session_tokens=10**9,
    )
    mgr = ChatManager(
        provider=provider, workdir_mgr=workdir_mgr, repo=repo, config=cfg,
    )

    async def _run():
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws = MagicMock()
        ws.send_json = AsyncMock()
        mgr._live[s.id] = LiveSession(
            chat_id=s.id, user_email="u@x", state=SessionState.ACTIVE,
            handle=FakeHandle(),
            started_at=datetime.now(timezone.utc),
            last_activity=datetime.now(timezone.utc),
            sinks=[SinkEntry(participant_email="u@x", sink=ws)],
        )
        # 3 messages allowed
        for i in range(3):
            await mgr.send_user_message(s.id, f"msg{i}")
        # 4th gets refused
        with pytest.raises(RuntimeError, match="rate_limit_exceeded"):
            await mgr.send_user_message(s.id, "msg3")
        kinds = [c.args[0].get("kind") for c in ws.send_json.call_args_list]
        assert "rate_limit" in kinds

    asyncio.run(_run())


def test_send_user_message_rejects_when_session_tokens_exhausted(tmp_path):
    """send_user_message must refuse new turns when the session's cumulative
    tokens exceed ChatConfig.max_session_tokens.

    Before this knob was wired the value lived only in instance.yaml.
    """
    from datetime import datetime, timezone

    from app.chat.manager import LiveSession
    from app.chat.types import SessionState

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    workdir_mgr = _make_workdir_mgr(tmp_path, repo)
    provider = MagicMock()
    provider.spawn = AsyncMock()
    cfg = ChatConfig(
        enabled=True, concurrency_per_user=5,
        max_session_tokens=100,
        daily_anthropic_spend_usd=10**6,  # disable daily cap
    )
    mgr = ChatManager(
        provider=provider, workdir_mgr=workdir_mgr, repo=repo, config=cfg,
    )

    async def _run():
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        # Stuff history past the cap.
        for _ in range(3):
            repo.append_message(
                session_id=s.id, role="assistant", content="x",
                tokens_in=30, tokens_out=30, model="fake",
            )
        ws = MagicMock()
        ws.send_json = AsyncMock()
        mgr._live[s.id] = LiveSession(
            chat_id=s.id, user_email="u@x", state=SessionState.ACTIVE,
            handle=FakeHandle(),
            started_at=datetime.now(timezone.utc),
            last_activity=datetime.now(timezone.utc),
            sinks=[SinkEntry(participant_email="u@x", sink=ws)],
        )
        with pytest.raises(RuntimeError, match="max_session_tokens_exhausted"):
            await mgr.send_user_message(s.id, "next")
        # Refusal frame surfaced to the WS.
        kinds = [
            c.args[0].get("kind") for c in ws.send_json.call_args_list
        ]
        assert "max_session_tokens" in kinds

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
        enabled=True, concurrency_per_user=5,
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
            handle=None,
            started_at=now - timedelta(seconds=5),
            last_activity=now,
            sinks=[SinkEntry(participant_email="u@x", sink=ws)],
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


def test_daily_tokens_cached_per_user(tmp_path):
    """daily_anthropic_tokens is called at most once per 60-second window.

    Two consecutive send_user_message calls for the same user must hit the
    repo only once — the second resolves from the in-instance TTL cache.
    """
    from datetime import datetime, timezone
    from unittest.mock import patch

    from app.chat.manager import LiveSession
    from app.chat.types import SessionState

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    workdir_mgr = _make_workdir_mgr(tmp_path, repo)
    provider = MagicMock()
    provider.spawn = AsyncMock()
    cfg = ChatConfig(
        enabled=True,
        concurrency_per_user=5,
        daily_anthropic_spend_usd=10**6,  # effectively unlimited
        max_session_tokens=10**9,
        rate_messages_per_hour=10**6,
    )
    mgr = ChatManager(
        provider=provider, workdir_mgr=workdir_mgr, repo=repo, config=cfg,
    )

    async def _run():
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws = MagicMock()
        ws.send_json = AsyncMock()
        mgr._live[s.id] = LiveSession(
            chat_id=s.id, user_email="u@x", state=SessionState.ACTIVE,
            handle=FakeHandle(),
            started_at=datetime.now(timezone.utc),
            last_activity=datetime.now(timezone.utc),
            sinks=[SinkEntry(participant_email="u@x", sink=ws)],
        )
        with patch.object(repo, "daily_anthropic_tokens", wraps=repo.daily_anthropic_tokens) as mock_fn:
            await mgr.send_user_message(s.id, "msg1")
            await mgr.send_user_message(s.id, "msg2")
            # Both calls should have used the cache; repo method called exactly once.
            assert mock_fn.call_count == 1, (
                f"expected daily_anthropic_tokens called once (cached); got {mock_fn.call_count}"
            )

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


def test_active_count_for_user_matches_private(monkeypatch):
    from types import SimpleNamespace
    from app.chat.manager import ChatManager
    from app.chat.types import SessionState

    mgr = ChatManager.__new__(ChatManager)  # bypass __init__; we set only _live
    mgr._live = {
        "a": SimpleNamespace(user_email="x@e.com", state=SessionState.ACTIVE),
        "b": SimpleNamespace(user_email="x@e.com", state=SessionState.IDLE),
        "c": SimpleNamespace(user_email="y@e.com", state=SessionState.ACTIVE),
        "d": SimpleNamespace(user_email="x@e.com", state=SessionState.DEAD),
    }
    assert mgr.active_count_for_user("x@e.com") == 2
    assert mgr.active_count_for_user("x@e.com") == mgr._active_count_for_user("x@e.com")
