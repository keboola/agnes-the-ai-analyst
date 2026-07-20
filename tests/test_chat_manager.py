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
from tests.chat_fakes import FakeHandle, FakeWS

from app.chat.config import ChatConfig
from app.chat.manager import ChatManager, SinkEntry
from app.chat.persistence import ChatRepository
from app.chat.types import SessionState, Surface
from app.chat.workdir import WorkdirManager
from app.coordination.factory import reset_coordination_for_tests


async def _wait_until(predicate, *, timeout: float = 3.0, interval: float = 0.01) -> bool:
    """Poll ``predicate()`` until true or ``timeout`` elapses (default 3s).

    Replaces a bare ``await asyncio.sleep(0.05)`` before asserting on state a
    background task (e.g. ``manager.attach``) is expected to have set. Under
    CI's pytest-xdist parallel CPU contention a fixed 50 ms sleep can elapse
    before the task's coroutine reaches that mutation, flaking the assert;
    polling is deterministic under any load.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


@pytest.fixture(autouse=True)
def _reset_coordination():
    """The per-sender message-rate window and daily-token counters
    (wave-2C task 4) now live in the coordination-backend singleton, which
    persists across tests in this file that reuse the same "u@x" identity
    — reset it so one test's rate/quota usage never bleeds into another's."""
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
            user_email="u@x",
            surface=Surface.SLACK_DM,
            slack_channel_id="C1",
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


# FakeHandle and FakeWS live in tests/chat_fakes (imported above).

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
    # Chat sandbox secret broker (2026-07-14): the real session JWT is never
    # forwarded into the sandbox env — it flows to the runner via a
    # ticket_push stdin frame instead (see tests/test_chat_manager.py's
    # secret-broker section below).
    assert "AGNES_TOKEN" not in env


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
        # Frames now also carry seq/id (wave-2F task 2 envelope) — assert on
        # the original fields via subset containment rather than exact dict
        # equality so this test doesn't couple to the envelope's shape.
        tokens = [m for m in ws.sent if m.get("type") == "token"]
        assert any(m.get("text") == "Hi" for m in tokens)

        await manager.kill(s.id, reason="test_done")
        handle.emit_eof()
        await attach_task

    asyncio.run(_run())


def test_kill_revokes_broker_tickets(manager: ChatManager):
    """kill() revokes the session's broker tickets so no stale rows linger in
    the DB past teardown (Devin review on #849)."""
    from src.repositories import ticket_repo

    async def _run():
        manager._provider.spawn = AsyncMock(return_value=FakeHandle())
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        tok = ticket_repo().mint(s.id, "main")
        assert ticket_repo().resolve(tok) is not None
        await manager.kill(s.id, reason="test_done")
        assert ticket_repo().resolve(tok) is None

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


def test_send_user_message_emits_chat_message_usage_event(manager: ChatManager, monkeypatch):
    """Every user chat turn lands one chat.message row in usage_events (via
    the server-event emitter) so /admin/telemetry and the adoption DAU count
    web + Slack chat activity, not just desktop CC sessions."""
    import app.chat.manager as manager_mod

    emitted: list[dict] = []

    class _FakeUsageRepo:
        def emit_server_event(self, **kw):
            emitted.append(kw)
            return "evt-1"

    class _FakeUsersRepo:
        def get_by_email(self, email):
            return {"id": "user-123", "email": email}

    monkeypatch.setattr(manager_mod, "usage_repo", lambda: _FakeUsageRepo())
    monkeypatch.setattr(manager_mod, "users_repo", lambda: _FakeUsersRepo())

    async def _run():
        handle = FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(manager.attach(s.id, ws))
        await asyncio.sleep(0.05)
        await manager.send_user_message(s.id, "hello")
        await manager.kill(s.id, reason="test_done")
        handle.emit_eof()
        await attach_task
        return s.id

    sid = asyncio.run(_run())
    assert len(emitted) == 1
    ev = emitted[0]
    assert ev["event_type"] == "chat.message"
    assert ev["username"] == "u@x"
    assert ev["user_id"] == "user-123"
    assert ev["props"] == {"surface": "web", "session_id": sid}


def test_send_user_message_emit_failure_does_not_break_send(manager: ChatManager, monkeypatch):
    """A broken telemetry backend must never block a chat turn."""
    import app.chat.manager as manager_mod

    def _boom():
        raise RuntimeError("telemetry down")

    monkeypatch.setattr(manager_mod, "usage_repo", _boom)

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


def test_send_user_message_emits_slack_surface(manager: ChatManager, monkeypatch):
    """Slack DM turns carry surface='slack_dm' in the emitted event props."""
    import app.chat.manager as manager_mod

    emitted: list[dict] = []

    class _FakeUsageRepo:
        def emit_server_event(self, **kw):
            emitted.append(kw)
            return "evt-1"

    monkeypatch.setattr(manager_mod, "usage_repo", lambda: _FakeUsageRepo())
    monkeypatch.setattr(manager_mod, "users_repo", lambda: (_ for _ in ()).throw(RuntimeError("no users db")))

    async def _run():
        handle = FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)
        s = await manager.create_session(user_email="u@x", surface=Surface.SLACK_DM, slack_channel_id="D123")
        ws = FakeWS()
        attach_task = asyncio.create_task(manager.attach(s.id, ws))
        await asyncio.sleep(0.05)
        await manager.send_user_message(s.id, "hello")
        await manager.kill(s.id, reason="test_done")
        handle.emit_eof()
        await attach_task

    asyncio.run(_run())
    assert len(emitted) == 1
    assert emitted[0]["props"]["surface"] == "slack_dm"
    # users lookup failed → falls back to user_id=None, username still set
    assert emitted[0]["user_id"] is None
    assert emitted[0]["username"] == "u@x"


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
            m
            for m in ws.sent
            if m.get("type") == "tool_result"
            and isinstance(m.get("result"), dict)
            and m["result"].get("cancelled") is True
        ]
        assert synthetic, f"expected synthetic tool_result with cancelled=true; got {ws.sent}"
        # And it must be persisted so crash-respawn replay sees it too.
        msgs = manager._repo.list_messages(s.id)
        persisted_cancels = [
            m
            for m in msgs
            if m.tool_calls and any(isinstance(tc, dict) and tc.get("cancelled") is True for tc in m.tool_calls)
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
        enabled=True,
        concurrency_per_user=5,
        rate_messages_per_hour=3,
        daily_anthropic_spend_usd=10**6,
        max_session_tokens=10**9,
    )
    mgr = ChatManager(
        provider=provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        config=cfg,
    )

    async def _run():
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws = MagicMock()
        ws.send_json = AsyncMock()
        mgr._live[s.id] = LiveSession(
            chat_id=s.id,
            user_email="u@x",
            state=SessionState.ACTIVE,
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
        enabled=True,
        concurrency_per_user=5,
        max_session_tokens=100,
        daily_anthropic_spend_usd=10**6,  # disable daily cap
    )
    mgr = ChatManager(
        provider=provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        config=cfg,
    )

    async def _run():
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        # Stuff history past the cap.
        for _ in range(3):
            repo.append_message(
                session_id=s.id,
                role="assistant",
                content="x",
                tokens_in=30,
                tokens_out=30,
                model="fake",
            )
        ws = MagicMock()
        ws.send_json = AsyncMock()
        mgr._live[s.id] = LiveSession(
            chat_id=s.id,
            user_email="u@x",
            state=SessionState.ACTIVE,
            handle=FakeHandle(),
            started_at=datetime.now(timezone.utc),
            last_activity=datetime.now(timezone.utc),
            sinks=[SinkEntry(participant_email="u@x", sink=ws)],
        )
        with pytest.raises(RuntimeError, match="max_session_tokens_exhausted"):
            await mgr.send_user_message(s.id, "next")
        # Refusal frame surfaced to the WS.
        kinds = [c.args[0].get("kind") for c in ws.send_json.call_args_list]
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
        enabled=True,
        concurrency_per_user=5,
        # Pin a tiny wallclock cap so the test is fast and deterministic.
        max_session_seconds=1,
        idle_ttl_seconds=10**9,  # disable idle path
        # Deliberately the DEFAULT pause policy: max_session_seconds is a hard
        # ceiling and must KILL even when on_detach="pause" — pausing would
        # re-trip on every post-resume sweep (infinite pause/resume loop,
        # PR #605 review finding).
        on_detach="pause",
    )
    mgr = ChatManager(
        provider=provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        config=cfg,
    )

    async def _run():
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        now = datetime.now(timezone.utc)
        ws = MagicMock()
        ws.send_json = AsyncMock()
        # Inject an "old" live session — active_seconds_accum already past the cap.
        # No sinks: with no attached browser the reaper kills outright (no pause).
        live = LiveSession(
            chat_id=s.id,
            user_email="u@x",
            state=SessionState.ACTIVE,
            handle=None,
            started_at=now - timedelta(seconds=5),
            last_activity=now,
            sinks=[],
        )
        # Set accumulated active time past max_session_seconds=1 so the reaper fires.
        live.active_seconds_accum = 5.0
        mgr._live[s.id] = live

        await mgr._reap_once()  # single sweep; no sleep loop
        assert s.id not in mgr._live, "expected stale session to be killed"
        # Killed for real — not paused: no sandbox refs left to resume from.
        row = repo.get_session(s.id)
        assert row.sandbox_paused_at is None and row.sandbox_id is None

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
        assert len(post_crash_tasks) == 2, f"expected 2 live tasks after crash respawn, got {len(post_crash_tasks)}"
        assert live.current_pump is not None
        assert live.current_pump in post_crash_tasks

        # Second crash → respawn again
        handles[1].emit_eof()
        handles[1].killed = True
        await asyncio.sleep(0.1)
        post_crash2_tasks = [t for t in live.tasks if not t.done()]
        assert len(post_crash2_tasks) == 2, f"expected 2 live tasks after 2nd respawn, got {len(post_crash2_tasks)}"

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


def test_daily_token_budget_uses_shared_coordination_counter(tmp_path):
    """Wave-2C task 4: the daily-spend check no longer hits the DB aggregate
    on every send — it reads coordination-backend counters that
    ``_record_daily_tokens`` keeps up to date as turns complete (see
    ``ChatManager._daily_token_totals``). The very first check of the day
    for a user is a ``(0, 0)`` counter reading, which is ambiguous (fresh
    quota vs. restart-lost history — see
    ``ChatManager._seed_daily_tokens_from_db_if_needed``) so it DOES consult
    ``repo.daily_anthropic_tokens`` once as a fallback seed; a second,
    same-day send must NOT call it again (the per-day "seeded" marker skips
    the DB round trip once a bucket has been checked). And — the actual
    point of routing this through the coordination backend — a second
    ChatManager instance (standing in for another app process) must see the
    same running total once a turn's tokens are recorded, not an
    independent zero.
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
        provider=provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        config=cfg,
    )

    async def _run():
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws = MagicMock()
        ws.send_json = AsyncMock()
        mgr._live[s.id] = LiveSession(
            chat_id=s.id,
            user_email="u@x",
            state=SessionState.ACTIVE,
            handle=FakeHandle(),
            started_at=datetime.now(timezone.utc),
            last_activity=datetime.now(timezone.utc),
            sinks=[SinkEntry(participant_email="u@x", sink=ws)],
        )
        with patch.object(repo, "daily_anthropic_tokens", wraps=repo.daily_anthropic_tokens) as mock_fn:
            await mgr.send_user_message(s.id, "msg1")
            await mgr.send_user_message(s.id, "msg2")
            assert mock_fn.call_count == 1, (
                f"expected the DB aggregate consulted exactly once (the first-ever miss's "
                f"restart-fallback seed), never again once the day-bucket is marked seeded; "
                f"got {mock_fn.call_count} calls"
            )

        # A completed turn's tokens are recorded against the running
        # counters (this is what _pump_subprocess_to_ws does for a real
        # assistant_message frame).
        mgr._record_daily_tokens("u@x", 1000, 2000)

        # Another ChatManager instance (another process, sharing the same
        # coordination backend) must see the identical accumulated total —
        # not its own independent, zeroed cache.
        mgr2 = ChatManager(provider=provider, workdir_mgr=workdir_mgr, repo=repo, config=cfg)
        assert mgr2._daily_token_totals("u@x") == (1000, 2000)

    asyncio.run(_run())


def test_daily_token_totals_seeds_from_db_after_restart(tmp_path):
    """Restart-forgets-spend regression (review finding): a process restart
    under the default ``memory`` coordination backend wipes the running
    daily-token counters. Without a DB fallback this silently reset a
    user's spend to 0, re-opening the full daily budget on a routine
    mid-day deploy even though ``chat_messages`` still held the real
    spend. Record real spend directly in the DB (the durable source of
    truth), simulate a restart by wiping the coordination backend, then
    confirm the very next check seeds from the DB aggregate
    (``ChatRepository.daily_anthropic_tokens``) and still blocks an
    over-budget user instead of handing them a fresh, forgotten quota.
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
        enabled=True,
        concurrency_per_user=5,
        daily_anthropic_spend_usd=1.0,  # low cap — 100k output tokens blows well past it
        max_session_tokens=10**9,
        rate_messages_per_hour=10**6,
    )
    mgr = ChatManager(provider=provider, workdir_mgr=workdir_mgr, repo=repo, config=cfg)

    async def _seed_history():
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        # A turn's tokens landed in chat_messages before the "restart" —
        # this is what ChatRepository.daily_anthropic_tokens still sees.
        repo.append_message(
            session_id=s.id,
            role="assistant",
            content="hi",
            tokens_in=0,
            tokens_out=100_000,
        )
        return s

    s = asyncio.run(_seed_history())
    assert repo.daily_anthropic_tokens("u@x") == (0, 100_000)

    # Simulate a process restart: the memory coordination backend's
    # running counters (and any "seeded" marker) are wiped. The DB row
    # above is untouched — it lives in a separate, durable store.
    reset_coordination_for_tests()

    async def _check_after_restart():
        ws = MagicMock()
        ws.send_json = AsyncMock()
        mgr._live[s.id] = LiveSession(
            chat_id=s.id,
            user_email="u@x",
            state=SessionState.ACTIVE,
            handle=FakeHandle(),
            started_at=datetime.now(timezone.utc),
            last_activity=datetime.now(timezone.utc),
            sinks=[SinkEntry(participant_email="u@x", sink=ws)],
        )
        with pytest.raises(RuntimeError, match="daily_budget_exhausted"):
            await mgr.send_user_message(s.id, "msg-after-restart")

    asyncio.run(_check_after_restart())

    # The seed is durable for the rest of the day-bucket, not just the one
    # check: the running counter itself now reflects the DB aggregate.
    assert mgr._daily_token_totals("u@x") == (0, 100_000)


def test_daily_token_totals_seed_race_has_single_winner(tmp_path):
    """Double-seed race regression: two requests racing on the exact same
    first-ever ``(0, 0)`` miss must not both seed the coordination counter
    from the DB aggregate — that would double-count today's real spend.
    The short-lived seed lease
    (``ChatManager._seed_daily_tokens_from_db_if_needed``) must make
    exactly one of them perform the seed; the persisted counter must land
    on the DB aggregate exactly once, never doubled, regardless of which
    thread "wins".
    """
    import threading

    from app.coordination.factory import coordination

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    workdir_mgr = _make_workdir_mgr(tmp_path, repo)
    provider = MagicMock()
    provider.spawn = AsyncMock()
    mgr = ChatManager(provider=provider, workdir_mgr=workdir_mgr, repo=repo, config=ChatConfig(enabled=True))

    async def _seed_history():
        s = await mgr.create_session(user_email="race@x", surface=Surface.WEB)
        repo.append_message(session_id=s.id, role="assistant", content="hi", tokens_in=500, tokens_out=700)

    asyncio.run(_seed_history())
    assert repo.daily_anthropic_tokens("race@x") == (500, 700)

    barrier = threading.Barrier(2)
    results: list[tuple[int, int]] = []
    results_lock = threading.Lock()

    def _check():
        barrier.wait(timeout=5)
        result = mgr._daily_token_totals("race@x")
        with results_lock:
            results.append(result)

    threads = [threading.Thread(target=_check) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(results) == 2

    key_in, key_out = mgr._daily_token_keys("race@x")
    final_in = coordination().incr(key_in, amount=0, ttl_s=60)
    final_out = coordination().incr(key_out, amount=0, ttl_s=60)
    assert (final_in, final_out) == (500, 700), (
        f"expected the counter seeded exactly once from the DB aggregate (500, 700); "
        f"got ({final_in}, {final_out}) — looks double-seeded"
    )


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


# ---------------------------------------------------------------------------
# Task 7 tests: per-turn frame buffer — mid-turn sink replay + partial save
# ---------------------------------------------------------------------------


def _attach_fake_live_with_fake_handle(mgr: ChatManager, chat_id: str, user_email: str, sink):
    """Insert a LiveSession with FakeHandle (has emit/readline) and one sink."""
    from datetime import datetime, timezone
    from app.chat.manager import LiveSession

    handle = FakeHandle()
    live = LiveSession(
        chat_id=chat_id,
        user_email=user_email,
        state=SessionState.ACTIVE,
        handle=handle,
        started_at=datetime.now(timezone.utc),
        last_activity=datetime.now(timezone.utc),
        sinks=[SinkEntry(participant_email=user_email, sink=sink)],
    )
    mgr._live[chat_id] = live
    return live


def test_midturn_sink_gets_buffered_frames_replayed(manager: ChatManager):
    """A sink added mid-turn receives the buffered token frames exactly once;
    ws1 does not receive duplicates."""

    async def _run():
        from tests.chat_fakes import FakeWS

        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws1 = FakeWS()
        live = _attach_fake_live_with_fake_handle(manager, s.id, "u@x", ws1)

        # Simulate a user message to set turn_in_flight=True, turn_buffer cleared
        await manager.send_user_message(s.id, "hello")

        # Pump two token frames (no assistant_message yet)
        pump_task = asyncio.create_task(manager._pump_subprocess_to_ws(live))
        live.handle.emit({"type": "token", "text": "Hel"})
        live.handle.emit({"type": "token", "text": "lo"})
        await asyncio.sleep(0.05)  # let pump process frames

        # Now add ws2 mid-turn — must see the two buffered token frames
        ws2 = FakeWS()
        await manager.add_sink(s.id, ws2, "u@x")

        token_frames_ws2 = [f for f in ws2.sent if f.get("type") == "token"]
        assert len(token_frames_ws2) == 2, f"ws2 should see 2 buffered token frames, got {token_frames_ws2}"

        # ws1 must NOT see duplicates: still exactly 2 tokens (already received before add_sink)
        token_frames_ws1 = [f for f in ws1.sent if f.get("type") == "token"]
        assert len(token_frames_ws1) == 2, f"ws1 should see 2 tokens without duplicates, got {token_frames_ws1}"

        # Cleanup
        pump_task.cancel()
        try:
            await pump_task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())


def test_turn_buffer_cleared_after_assistant_message(manager: ChatManager):
    """After a full turn completes (assistant_message frame), a newly added
    sink must NOT receive any token replay."""

    async def _run():
        from tests.chat_fakes import FakeWS

        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws1 = FakeWS()
        live = _attach_fake_live_with_fake_handle(manager, s.id, "u@x", ws1)

        await manager.send_user_message(s.id, "hello")

        # Pump token + assistant_message (full turn)
        pump_task = asyncio.create_task(manager._pump_subprocess_to_ws(live))
        live.handle.emit({"type": "token", "text": "Hi"})
        live.handle.emit(
            {
                "type": "assistant_message",
                "content": "Hi",
                "tokens_in": 1,
                "tokens_out": 1,
            }
        )
        await asyncio.sleep(0.05)

        # Add a sink after the turn completed — should see no token replay
        ws2 = FakeWS()
        await manager.add_sink(s.id, ws2, "u@x")

        token_frames_ws2 = [f for f in ws2.sent if f.get("type") == "token"]
        assert len(token_frames_ws2) == 0, (
            f"buffer should be cleared after assistant_message; ws2 got {token_frames_ws2}"
        )
        assert not live.turn_in_flight, "turn_in_flight should be False after assistant_message"
        assert live.turn_buffer == [], "turn_buffer should be empty after assistant_message"

        pump_task.cancel()
        try:
            await pump_task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())


def test_kill_midturn_persists_partial_assistant_message(manager: ChatManager):
    """kill() mid-turn must persist accumulated token text as an interrupted
    assistant message with tool_calls=[{interrupted: True, reason: ...}]."""

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        live = _attach_fake_live_with_fake_handle(manager, s.id, "u@x", ws)

        await manager.send_user_message(s.id, "hello")

        # Pump two token frames — no assistant_message (mid-turn)
        pump_task = asyncio.create_task(manager._pump_subprocess_to_ws(live))
        live.handle.emit({"type": "token", "text": "Hel"})
        live.handle.emit({"type": "token", "text": "lo"})
        await asyncio.sleep(0.05)

        # Kill mid-turn
        await manager.kill(s.id, reason="idle_ttl")

        pump_task.cancel()
        try:
            await pump_task
        except asyncio.CancelledError:
            pass

        msgs = manager._repo.list_messages(s.id)
        assistant_rows = [m for m in msgs if m.role == "assistant"]
        assert assistant_rows, "expected at least one assistant row after kill mid-turn"
        partial = assistant_rows[-1]
        assert partial.content == "Hello", f"expected 'Hello' content, got {partial.content!r}"
        assert partial.tool_calls is not None, "expected tool_calls metadata"
        interrupted = [tc for tc in partial.tool_calls if isinstance(tc, dict) and tc.get("interrupted") is True]
        assert interrupted, f"expected interrupted=True in tool_calls, got {partial.tool_calls}"
        assert interrupted[0].get("reason") == "idle_ttl"

    asyncio.run(_run())


def test_kill_between_turns_persists_nothing_extra(manager: ChatManager):
    """kill() after a completed turn must NOT add an extra interrupted row."""

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        live = _attach_fake_live_with_fake_handle(manager, s.id, "u@x", ws)

        await manager.send_user_message(s.id, "hello")

        # Complete a full turn
        pump_task = asyncio.create_task(manager._pump_subprocess_to_ws(live))
        live.handle.emit(
            {
                "type": "assistant_message",
                "content": "A",
                "tokens_in": 1,
                "tokens_out": 1,
            }
        )
        await asyncio.sleep(0.05)

        # Kill between turns (buffer should be empty)
        await manager.kill(s.id, reason="test_done")

        pump_task.cancel()
        try:
            await pump_task
        except asyncio.CancelledError:
            pass

        msgs = manager._repo.list_messages(s.id)
        assistant_rows = [m for m in msgs if m.role == "assistant"]
        assert len(assistant_rows) == 1, f"expected exactly 1 assistant row, got {len(assistant_rows)}"
        # No interrupted marker
        if assistant_rows[0].tool_calls:
            interrupted = [
                tc for tc in assistant_rows[0].tool_calls if isinstance(tc, dict) and tc.get("interrupted") is True
            ]
            assert not interrupted, "no interrupted marker expected after full turn"

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Task 8 tests: manager owns session lifecycle — detach/linger/pause/resume
# ---------------------------------------------------------------------------

from tests.chat_fakes import FakeProvider  # noqa: E402


def _make_pause_manager(tmp_path, linger_seconds=0):
    """ChatManager with FakeProvider and on_detach='pause'."""
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    workdir_mgr = _make_workdir_mgr(tmp_path, repo)
    provider = FakeProvider()
    return ChatManager(
        provider=provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        config=ChatConfig(
            enabled=True,
            concurrency_per_user=5,
            on_detach="pause",
            detach_linger_seconds=linger_seconds,
            paused_ttl_seconds=7 * 24 * 3600,
            idle_ttl_seconds=10**9,
        ),
    )


def _make_kill_manager(tmp_path):
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    workdir_mgr = _make_workdir_mgr(tmp_path, repo)
    provider = FakeProvider()
    return ChatManager(
        provider=provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        config=ChatConfig(
            enabled=True,
            concurrency_per_user=5,
            on_detach="kill",
            detach_linger_seconds=0,
            paused_ttl_seconds=7 * 24 * 3600,
            idle_ttl_seconds=10**9,
        ),
    )


def monkeypatch_workdir(mgr: ChatManager) -> None:
    """Bypass the real WorkdirManager filesystem operations for testing."""
    import unittest.mock as mock

    mgr._workdir_mgr.ensure_user_workdir = mock.MagicMock()
    mgr._workdir_mgr.prepare_session_dir = mock.MagicMock(return_value=Path("/tmp/fake-session-dir"))


def test_detach_last_sink_does_not_kill(tmp_path):
    """With on_detach='pause', removing the last sink must NOT kill the session;
    it stays in _live with state ACTIVE through the linger window."""

    async def _run():
        mgr = _make_pause_manager(tmp_path, linger_seconds=999)
        monkeypatch_workdir(mgr)
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws))
        await asyncio.sleep(0.05)
        assert s.id in mgr._live
        await mgr.detach_sink(s.id, ws)
        await asyncio.sleep(0.05)
        # Session must still be alive (linger window is 999 s)
        assert s.id in mgr._live, "session killed immediately on detach — expected linger"
        live = mgr._live[s.id]
        assert live.state == SessionState.ACTIVE
        # Cleanup
        await mgr.kill(s.id, reason="test_done")
        attach_task.cancel()
        try:
            await attach_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_run())


def test_linger_then_pause_persists_refs(tmp_path):
    """linger_seconds=0: provider.pause called, repo row has sandbox refs, state PAUSED."""

    async def _run():
        mgr = _make_pause_manager(tmp_path, linger_seconds=0)
        monkeypatch_workdir(mgr)
        provider = mgr._provider
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws))
        await asyncio.sleep(0.05)
        await mgr.detach_sink(s.id, ws)
        await asyncio.sleep(0.15)  # linger=0, pause should have fired
        # Provider should have paused the sandbox
        assert provider.paused, "expected sandbox to be paused in provider"
        # Repo row should reflect the pause
        session = mgr._repo.get_session(s.id)
        assert session is not None
        assert session.sandbox_id is not None
        assert session.runner_pid is not None
        assert session.sandbox_paused_at is not None
        # Live entry state should be PAUSED
        live = mgr._live.get(s.id)
        assert live is None or live.state == SessionState.PAUSED

        attach_task.cancel()
        try:
            await attach_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_run())


def test_reattach_during_linger_cancels_pause(tmp_path):
    """A new sink arriving inside the linger window must cancel the pause task."""

    async def _run():
        mgr = _make_pause_manager(tmp_path, linger_seconds=999)
        monkeypatch_workdir(mgr)
        provider = mgr._provider
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws1 = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws1))
        await asyncio.sleep(0.05)
        await mgr.detach_sink(s.id, ws1)
        await asyncio.sleep(0.02)  # inside linger window
        # Re-attach before linger expires
        ws2 = FakeWS()
        await mgr.add_sink(s.id, ws2, "u@x")
        await asyncio.sleep(0.05)
        # Pause must NOT have been called
        assert not provider.paused, "pause should not fire when sink returned during linger"
        live = mgr._live.get(s.id)
        assert live is not None and live.state == SessionState.ACTIVE

        await mgr.kill(s.id, reason="test_done")
        attach_task.cancel()
        try:
            await attach_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_run())


def test_pause_waits_for_inflight_turn(tmp_path):
    """When turn_in_flight is True, _linger_then_pause waits for it to clear."""

    async def _run():
        mgr = _make_pause_manager(tmp_path, linger_seconds=0)
        monkeypatch_workdir(mgr)
        provider = mgr._provider
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws))
        await asyncio.sleep(0.05)
        live = mgr._live[s.id]
        # Mark a turn in flight before detaching
        live.turn_in_flight = True
        await mgr.detach_sink(s.id, ws)
        await asyncio.sleep(0.15)  # linger=0 but turn still in flight
        # Pause should NOT have fired yet
        assert not provider.paused, "pause should wait for in-flight turn"
        # Simulate turn completing
        live.turn_in_flight = False
        await asyncio.sleep(0.15)
        # Now pause should fire
        assert provider.paused, "expected pause after turn_in_flight cleared"

        attach_task.cancel()
        try:
            await attach_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_run())


def test_linger_bails_when_runner_dies_during_inflight_turn(tmp_path):
    """Regression — Devin Review BUG_0001 follow-up on #605.

    If a runner dies (3× crash → SessionState.DEAD) while a turn is
    in-flight and no sink is attached, `_linger_then_pause` used to spin
    forever on `while live.turn_in_flight` (no pump alive to clear it)
    and the `_live` entry leaked (the reaper skips DEAD sessions). The
    fix adds a state check inside the spin so the linger task bails out
    cleanly when the session has died."""

    async def _run():
        mgr = _make_pause_manager(tmp_path, linger_seconds=0)
        monkeypatch_workdir(mgr)
        provider = mgr._provider
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws))
        await asyncio.sleep(0.05)
        live = mgr._live[s.id]
        # Set up: turn in flight, no sinks, runner declared DEAD
        # (the 3× crash terminal state — _wait_for_exit_and_respawn sets
        # this without ever emitting a `done` frame to clear the flag).
        live.turn_in_flight = True
        await mgr.detach_sink(s.id, ws)
        live.state = SessionState.DEAD
        # The linger task should bail out promptly rather than spinning
        # forever waiting for the turn to "complete." We grant it a few
        # tick cycles to notice the state transition.
        await asyncio.sleep(0.2)
        assert live.linger_task is None or live.linger_task.done(), (
            "linger task must complete (not spin) when runner died mid-turn with no sinks attached"
        )
        # And no pause was issued (state was already DEAD, not ACTIVE).
        assert not provider.paused, "DEAD session must not be paused"

        attach_task.cancel()
        try:
            await attach_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_run())


def test_attach_to_paused_resumes_same_handle(tmp_path):
    """attach() to a PAUSED live session resumes it; state becomes ACTIVE,
    paused_at cleared, and the pump delivers frames to the new sink."""

    async def _run():
        mgr = _make_pause_manager(tmp_path, linger_seconds=0)
        monkeypatch_workdir(mgr)
        provider = mgr._provider
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws1 = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws1))
        await asyncio.sleep(0.05)
        await mgr.detach_sink(s.id, ws1)
        await asyncio.sleep(0.15)  # let pause fire
        assert provider.paused

        # Re-attach: should resume
        ws2 = FakeWS()
        attach_task2 = asyncio.create_task(mgr.attach(s.id, ws2))
        await asyncio.sleep(0.1)
        live = mgr._live.get(s.id)
        assert live is not None
        assert live.state == SessionState.ACTIVE
        session = mgr._repo.get_session(s.id)
        assert session.sandbox_paused_at is None

        # Emit a frame and verify ws2 receives it
        live.handle.emit({"type": "token", "text": "hi"})
        await asyncio.sleep(0.05)
        token_frames = [f for f in ws2.sent if f.get("type") == "token"]
        assert token_frames, "resumed session should deliver frames to new sink"

        await mgr.kill(s.id, reason="test_done")
        for t in [attach_task, attach_task2]:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    asyncio.run(_run())


def test_attach_to_live_session_does_not_spawn_second_runner(tmp_path):
    """attach() to an already-ACTIVE session must not spawn a new handle."""

    async def _run():
        mgr = _make_pause_manager(tmp_path, linger_seconds=999)
        monkeypatch_workdir(mgr)
        provider = mgr._provider
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws1 = FakeWS()
        attach_task1 = asyncio.create_task(mgr.attach(s.id, ws1))
        await asyncio.sleep(0.05)
        assert len(provider.spawned) == 1

        ws2 = FakeWS()
        attach_task2 = asyncio.create_task(mgr.attach(s.id, ws2))
        await asyncio.sleep(0.05)
        assert len(provider.spawned) == 1, "second attach must NOT spawn another runner"

        await mgr.kill(s.id, reason="test_done")
        for t in [attach_task1, attach_task2]:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    asyncio.run(_run())


def test_resume_failure_falls_back_to_fresh_spawn(tmp_path):
    """When provider.resume raises, clear_sandbox_ref + fresh spawn + state ACTIVE."""

    async def _run():
        mgr = _make_pause_manager(tmp_path, linger_seconds=0)
        monkeypatch_workdir(mgr)
        provider = mgr._provider
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws1 = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws1))
        await asyncio.sleep(0.05)
        await mgr.detach_sink(s.id, ws1)
        await asyncio.sleep(0.15)  # pause fires
        assert provider.paused
        # Make resume fail
        provider.fail_resume = True

        ws2 = FakeWS()
        attach_task2 = asyncio.create_task(mgr.attach(s.id, ws2))
        await asyncio.sleep(0.2)
        live = mgr._live.get(s.id)
        assert live is not None
        assert live.state == SessionState.ACTIVE, "should fall back to fresh spawn"
        assert len(provider.spawned) == 2, "expected a second spawn on resume failure"
        session = mgr._repo.get_session(s.id)
        assert session.sandbox_paused_at is None

        await mgr.kill(s.id, reason="test_done")
        for t in [attach_task, attach_task2]:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    asyncio.run(_run())


def test_send_user_message_resumes_paused_session(tmp_path):
    """send_user_message to a PAUSED session resumes it first (Slack path)."""

    async def _run():
        mgr = _make_pause_manager(tmp_path, linger_seconds=0)
        monkeypatch_workdir(mgr)
        provider = mgr._provider
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws1 = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws1))
        await asyncio.sleep(0.05)
        await mgr.detach_sink(s.id, ws1)
        await asyncio.sleep(0.15)  # pause fires
        assert provider.paused

        # send_user_message should resume first
        await mgr.send_user_message(s.id, "hello after pause")
        live = mgr._live.get(s.id)
        assert live is not None
        assert live.state == SessionState.ACTIVE

        await mgr.kill(s.id, reason="test_done")
        attach_task.cancel()
        try:
            await attach_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_run())


def test_attach_after_restart_resumes_from_repo_row(tmp_path):
    """Post-restart: _live cleared, but repo row has sandbox refs — attach resumes."""

    async def _run():
        mgr = _make_pause_manager(tmp_path, linger_seconds=0)
        monkeypatch_workdir(mgr)
        provider = mgr._provider
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws1 = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws1))
        await asyncio.sleep(0.05)
        await mgr.detach_sink(s.id, ws1)
        await asyncio.sleep(0.15)  # pause fires, refs persisted in repo
        assert provider.paused

        # Simulate server restart: clear in-memory _live
        mgr._live.clear()

        # Re-attach must resume purely from repo row
        ws2 = FakeWS()
        attach_task2 = asyncio.create_task(mgr.attach(s.id, ws2))
        await asyncio.sleep(0.15)
        live = mgr._live.get(s.id)
        assert live is not None
        assert live.state == SessionState.ACTIVE

        await mgr.kill(s.id, reason="test_done")
        for t in [attach_task, attach_task2]:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    asyncio.run(_run())


def test_on_detach_kill_preserves_legacy_behavior(tmp_path):
    """on_detach='kill': last-sink detach must kill the session immediately."""

    async def _run():
        mgr = _make_kill_manager(tmp_path)
        monkeypatch_workdir(mgr)
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws))
        await asyncio.sleep(0.05)
        await mgr.detach_sink(s.id, ws)
        await asyncio.sleep(0.1)
        assert s.id not in mgr._live, "on_detach=kill: session should be dead after detach"

        attach_task.cancel()
        try:
            await attach_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Task 9 tests: reaper pauses, paused-TTL GC, active-time cap, shutdown pauses
# ---------------------------------------------------------------------------


def test_idle_ttl_pauses_instead_of_kills(tmp_path):
    """Reaper on an idle ACTIVE session with no sinks → PAUSED, sandbox alive in provider."""

    async def _run():
        mgr = _make_pause_manager(tmp_path, linger_seconds=0)
        monkeypatch_workdir(mgr)
        provider = mgr._provider
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws))
        await asyncio.sleep(0.05)
        live = mgr._live[s.id]
        # Empty sinks without triggering linger
        live.sinks = []
        # Force last_activity into the past
        from datetime import datetime as _dt, timedelta, timezone as _tz

        live.last_activity = _dt.now(_tz.utc) - timedelta(seconds=1)

        # Patch config to use idle_ttl=0 for fast reap
        original_config = mgr._config

        class _PatchedConfig:
            def __getattr__(self, name):
                if name == "idle_ttl_seconds":
                    return 0
                return getattr(original_config, name)

        mgr._config = _PatchedConfig()
        await mgr._reap_once()
        mgr._config = original_config

        live = mgr._live.get(s.id)
        assert live is None or live.state == SessionState.PAUSED
        assert provider.paused, "sandbox must be in provider.paused after idle reaper"

        attach_task.cancel()
        try:
            await attach_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_run())


def test_paused_ttl_really_kills(tmp_path):
    """Repo row paused before cutoff → provider.destroy called + sandbox refs cleared."""

    async def _run():
        mgr = _make_pause_manager(tmp_path, linger_seconds=0)
        monkeypatch_workdir(mgr)
        provider = mgr._provider
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws))
        await asyncio.sleep(0.05)
        await mgr.detach_sink(s.id, ws)
        await asyncio.sleep(0.15)  # pause fires
        assert provider.paused

        # Push sandbox_paused_at into the past past the TTL
        from datetime import datetime as _dt, timedelta, timezone as _tz

        mgr._repo.set_sandbox_paused_at(
            s.id,
            _dt.now(_tz.utc) - timedelta(seconds=mgr._config.paused_ttl_seconds + 1),
        )
        # Clear _live so it tests the repo-row-only path
        mgr._live.clear()

        await mgr._reap_once()

        session = mgr._repo.get_session(s.id)
        assert session.sandbox_id is None, "sandbox ref must be cleared after paused-TTL GC"
        assert session.runner_pid is None
        assert session.sandbox_paused_at is None
        assert provider.destroyed, "provider.destroy must be called for expired paused sandbox"

        attach_task.cancel()
        try:
            await attach_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_run())


def test_max_session_seconds_counts_active_time_only(tmp_path):
    """max_session_seconds uses accumulated active time; pause stops the clock."""

    async def _run():
        import time as _time

        mgr = _make_pause_manager(tmp_path, linger_seconds=0)
        monkeypatch_workdir(mgr)
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws))
        await asyncio.sleep(0.05)
        live = mgr._live[s.id]
        # 1 hour accumulated, currently paused (active_since barely recent)
        live.active_seconds_accum = 3600.0
        live.active_since = _time.monotonic() - 10  # only 10 s since last resume
        live.state = SessionState.PAUSED  # pause stops the clock

        # max_session_seconds=4h: total active ≈ 1h 10s ≪ 4h → should NOT reap
        original_config = mgr._config

        class _PatchedConfig:
            def __getattr__(self, name):
                if name == "max_session_seconds":
                    return 4 * 3600
                if name == "idle_ttl_seconds":
                    return 10**9  # disable idle path
                return getattr(original_config, name)

        mgr._config = _PatchedConfig()
        await mgr._reap_once()
        assert s.id in mgr._live, "paused session with only ~1h active time must not be reaped at 4h cap"

        # Now exceed the cap
        live.active_seconds_accum = 4 * 3600 + 1
        await mgr._reap_once()
        remaining = mgr._live.get(s.id)
        assert remaining is None or remaining.state in (
            SessionState.DEAD,
            SessionState.PAUSED,
        ), "session past active cap must be killed or paused"

        mgr._config = original_config
        attach_task.cancel()
        try:
            await attach_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_run())


def test_shutdown_pauses_active_sessions(tmp_path):
    """shutdown() with on_detach='pause' pauses ACTIVE sessions instead of killing."""

    async def _run():
        mgr = _make_pause_manager(tmp_path, linger_seconds=999)
        monkeypatch_workdir(mgr)
        provider = mgr._provider
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws))
        await asyncio.sleep(0.05)
        assert s.id in mgr._live

        await mgr.shutdown()

        assert provider.paused, "shutdown with on_detach=pause should pause active sandboxes"
        session = mgr._repo.get_session(s.id)
        assert session.sandbox_paused_at is not None, "sandbox_paused_at must be set after shutdown-pause"

        attach_task.cancel()
        try:
            await attach_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_run())


def test_keepalive_heartbeat_extends_timeout_while_sinks_attached(tmp_path):
    """The reaper tick calls provider.keepalive for ACTIVE sessions with sinks.

    Awaits ``mgr.attach`` directly rather than firing it via
    ``asyncio.create_task`` + a fixed sleep (the pre-existing pattern here):
    ``attach()`` fully completes seat_sink (and thus registers the sink)
    before returning — it doesn't need the pump/wait tasks it kicks off to
    finish — so there's no concurrency to simulate and nothing to race. A
    fixed 50ms sleep before checking ``live.sinks`` was flaky under a loaded
    CI runner (8 shards x pytest -n auto), matching the direct-await pattern
    already used by sibling non-concurrent attach tests in this file (e.g.
    test_broadcast_dead_sink_sweep_triggers_detach_policy,
    test_seat_sink_does_not_replay_persisted_history)."""

    async def _run():
        mgr = _make_pause_manager(tmp_path, linger_seconds=999)
        monkeypatch_workdir(mgr)
        provider = mgr._provider
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        await mgr.attach(s.id, ws)
        live = mgr._live.get(s.id)
        assert live is not None and live.sinks  # has a sink

        await mgr._reap_once()
        assert provider.keepalive_calls, "keepalive should be called for ACTIVE session with sinks"

        await mgr.kill(s.id, reason="test_done")

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# PR #605 review regressions (Devin findings)
# ---------------------------------------------------------------------------


def test_seat_sink_does_not_replay_persisted_history(manager: ChatManager):
    """The primary WS must NOT receive persisted messages on attach — the web
    client already loaded them via REST; replaying duplicates every bubble.
    Only the in-progress turn buffer (+ ready) goes over the wire."""

    async def _run():
        handle = FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        manager._repo.append_message(session_id=s.id, role="user", content="q?")
        manager._repo.append_message(session_id=s.id, role="assistant", content="a!")
        ws = FakeWS()
        await manager.attach(s.id, ws)
        types = [f.get("type") for f in ws.sent]
        assert "assistant_message" not in types
        assert "user_msg" not in types
        assert types[-1] == "ready"
        await manager.kill(s.id, reason="test_done")

    asyncio.run(_run())


def test_broadcast_dead_sink_sweep_triggers_detach_policy(manager: ChatManager):
    """When _broadcast's dead-sink removal empties live.sinks, the on-detach
    policy must fire (linger task scheduled) — a joiner whose socket died
    without a clean detach must not leave the session ownerless until the
    idle reaper."""

    async def _run():
        handle = FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        await manager.attach(s.id, ws)
        live = manager._live[s.id]
        assert live.linger_task is None

        class DeadSink:
            async def send_json(self, data):
                raise RuntimeError("socket gone")

            async def close(self):
                pass

        live.sinks = [SinkEntry(participant_email="u@x", sink=DeadSink())]
        await manager._broadcast(live, {"type": "token", "text": "x"})
        assert not live.sinks
        assert live.linger_task is not None
        await manager.kill(s.id, reason="test_done")

    asyncio.run(_run())


def test_resume_from_row_co_session_uses_ephemeral_dir(manager: ChatManager, monkeypatch):
    """Cold-start resume of a co-session must rebuild the ephemeral
    grant-intersection workspace (SR-6), not a personal one.

    A cold-start ``_resume_from_row`` call is, by definition, a session this
    process has no ``_known_protocol_sessions`` record for (nothing spawned
    or ticket-pushed it in this process) — so per AC-G-resume-legacy it now
    goes through the fresh-spawn path (``_spawn_live``), not
    ``provider.resume()``. That path shares the same co-session ephemeral-dir
    selection this test guards."""
    import app.chat.manager as manager_mod

    monkeypatch.setattr(manager_mod, "ticket_repo", lambda: _FakeTicketRepo())

    async def _run():
        s = await manager.create_session(user_email="owner@x", surface=Surface.WEB)
        manager._repo._conn.execute("UPDATE chat_sessions SET is_co_session = TRUE WHERE id = ?", [s.id])
        manager._repo.add_session_participant(session_id=s.id, user_email="owner@x", user_id="u1", role="owner")
        manager._repo.add_session_participant(session_id=s.id, user_email="peer@x", user_id="u2", role="collaborator")
        manager._repo.set_sandbox_ref(s.id, sandbox_id="sbx-co", runner_pid=42)

        handle = FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)
        monkeypatch.setattr(
            "src.grant_intersection.compute_grant_intersection",
            lambda emails, conn: {},
        )
        eph = MagicMock(return_value=Path("/tmp/eph-dir"))
        personal = MagicMock()
        monkeypatch.setattr(manager._workdir_mgr, "prepare_ephemeral_session_dir", eph)
        monkeypatch.setattr(manager._workdir_mgr, "prepare_session_dir", personal)

        session = manager._repo.get_session(s.id)
        live = await manager._resume_from_row(session)
        assert live is not None
        eph.assert_called_once()
        personal.assert_not_called()
        assert sorted(live.participant_emails) == ["owner@x", "peer@x"]
        await manager.kill(s.id, reason="test_done")

    asyncio.run(_run())


def test_crash_respawn_refreshes_sandbox_refs(manager: ChatManager):
    """A crash-respawn must persist the NEW sandbox's refs — otherwise a later
    pause/resume reconnects the dead sandbox and silently loses the agent's
    in-memory context (PR #605 review finding)."""

    async def _run():
        h1, h2 = FakeHandle(), FakeHandle()
        h1.sandbox_id, h2.sandbox_id = "sbx-old", "sbx-new"
        manager._provider.spawn = AsyncMock(side_effect=[h1, h2])
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        await manager.attach(s.id, ws)
        assert manager._repo.get_session(s.id).sandbox_id == "sbx-old"

        # Crash: first handle exits non-zero → _wait_for_exit_and_respawn
        # spawns h2 and must refresh the persisted refs.
        h1.exit_code = 1
        h1.killed = True  # FakeHandle.wait() returns once killed flips
        for _ in range(100):
            await asyncio.sleep(0.02)
            if manager._live[s.id].handle is h2:
                break
        row = manager._repo.get_session(s.id)
        assert row.sandbox_id == "sbx-new"
        assert row.runner_pid == h2.pid
        await manager.kill(s.id, reason="test_done")

    asyncio.run(_run())


def test_reaper_gcs_dead_sessions(manager: ChatManager):
    """DEAD entries (3x-crash leftovers) must be GC'd by the reaper — the
    crash path marks state=DEAD without popping _live, and the reaper used
    to skip non-ACTIVE/IDLE states, leaking one entry per crashed session."""

    async def _run():
        handle = FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        await manager.attach(s.id, ws)
        manager._live[s.id].state = SessionState.DEAD
        await manager._reap_once()
        assert s.id not in manager._live

    asyncio.run(_run())


def test_spawn_agnes_server_falls_back_to_internal_url(manager: ChatManager, tmp_path, monkeypatch):
    """Plain-HTTP deployments keep SERVER_URL unset (or unusable) and point
    the sandbox data rails at AGNES_INTERNAL_URL instead."""
    monkeypatch.delenv("SERVER_URL", raising=False)
    monkeypatch.setenv("AGNES_INTERNAL_URL", "http://10.0.0.5:8000")
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

    assert captured["env"]["AGNES_SERVER"] == "http://10.0.0.5:8000"


def test_agnes_server_url_resolution_chain(monkeypatch):
    """SERVER_URL → AGNES_INTERNAL_URL → loopback; the same chain feeds both
    the sandbox env (AGNES_SERVER) and the workspace seed (WorkdirManager)."""
    from app.chat.manager import agnes_server_url

    monkeypatch.setenv("SERVER_URL", "https://agnes.example.com/")
    monkeypatch.setenv("AGNES_INTERNAL_URL", "http://10.0.0.5:8000")
    assert agnes_server_url() == "https://agnes.example.com"

    monkeypatch.delenv("SERVER_URL", raising=False)
    assert agnes_server_url() == "http://10.0.0.5:8000"

    monkeypatch.delenv("AGNES_INTERNAL_URL", raising=False)
    assert agnes_server_url() == "http://127.0.0.1:8000"

    # Empty string is "unset", not a value — .env files with SERVER_URL= must
    # not produce an empty rails URL.
    monkeypatch.setenv("SERVER_URL", "")
    monkeypatch.setenv("AGNES_INTERNAL_URL", "http://10.0.0.5:8000")
    assert agnes_server_url() == "http://10.0.0.5:8000"


# ---------------------------------------------------------------------------
# Chat sandbox secret broker (2026-07-14): ticket mint + stdin push at
# spawn/resume, real-secret-free env, legacy-runner force-respawn.
# ---------------------------------------------------------------------------


class _FakeTicketRepo:
    """Stand-in for src.repositories.ticket_repo() — records mint/revoke
    calls instead of touching a real chat_broker_tickets table."""

    def __init__(self) -> None:
        self.minted: list[tuple[str, str]] = []
        self.revoked: list[str] = []

    def mint(self, session_id: str, scope: str, ttl_seconds: int = 3600) -> str:
        self.minted.append((session_id, scope))
        return f"ticket-{scope}-{len(self.minted)}"

    def revoke_session(self, session_id: str) -> None:
        self.revoked.append(session_id)


def test_spawn_env_has_no_real_secret(manager: ChatManager, monkeypatch):
    """The sandbox spawn env must never carry the real Anthropic key or the
    real Agnes session JWT — both are brokered via tickets pushed over
    stdin instead of injected as env vars (AC-F-nosecret)."""
    monkeypatch.setattr("app.auth.access.mint_session_jwt", lambda *a, **k: "real-jwt-must-not-leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-real-must-not-leak")

    import app.chat.manager as manager_mod

    monkeypatch.setattr(manager_mod, "ticket_repo", lambda: _FakeTicketRepo())

    captured = {}

    async def fake_spawn(**kw):
        captured.update(kw)
        return FakeHandle()

    manager._provider.spawn = fake_spawn

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        sess = manager._repo.get_session(s.id)
        await manager._spawn_runner(sess, Path("/tmp"))

    asyncio.run(_run())

    env = captured["env"]
    assert env.get("ANTHROPIC_API_KEY") in (None, "", "sk-dummy-broker")
    assert "AGNES_TOKEN" not in env


def test_spawn_pushes_ticket_frame(manager: ChatManager, monkeypatch):
    """A fresh spawn must mint main+mcp tickets and push a ticket_push frame
    over stdin, under _stdin_lock, before the session is considered ready."""
    monkeypatch.setattr("app.auth.access.mint_session_jwt", lambda *a, **k: "tok")
    import app.chat.manager as manager_mod

    fake_tickets = _FakeTicketRepo()
    monkeypatch.setattr(manager_mod, "ticket_repo", lambda: fake_tickets)

    handle = FakeHandle()
    manager._provider.spawn = AsyncMock(return_value=handle)

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(manager.attach(s.id, ws))
        await asyncio.sleep(0.05)
        await manager.kill(s.id, reason="test_done")
        attach_task.cancel()
        try:
            await attach_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_run())

    frames = [json.loads(b) for b in handle._stdin_buf]
    ticket_frames = [f for f in frames if f.get("type") == "ticket_push"]
    assert ticket_frames, f"expected a ticket_push frame on stdin; got {frames}"
    assert ticket_frames[0]["main"] and ticket_frames[0]["mcp"]
    scopes = {scope for (_sid, scope) in fake_tickets.minted}
    assert scopes == {"main", "mcp"}


def test_resume_pushes_fresh_ticket_before_messages(tmp_path, monkeypatch):
    """Resuming a PAUSED in-memory session (current-protocol runner) mints
    fresh tickets, revokes the old ones, and pushes a ticket_push frame over
    stdin under _stdin_lock before any further message is forwarded
    (AC-G-resume-fresh)."""
    import app.chat.manager as manager_mod

    fake_tickets = _FakeTicketRepo()
    monkeypatch.setattr(manager_mod, "ticket_repo", lambda: fake_tickets)

    mgr = _make_pause_manager(tmp_path, linger_seconds=0)
    monkeypatch_workdir(mgr)
    provider = mgr._provider

    async def _run():
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws))
        await asyncio.sleep(0.05)
        await mgr.detach_sink(s.id, ws)
        await asyncio.sleep(0.15)  # pause fires
        assert provider.paused

        # Discard the initial-spawn mint calls/frame — only the resume matters.
        fake_tickets.minted.clear()
        fake_tickets.revoked.clear()
        parked = next(iter(provider.paused.values()))
        parked._stdin_buf.clear()

        ws2 = FakeWS()
        attach_task2 = asyncio.create_task(mgr.attach(s.id, ws2))
        await asyncio.sleep(0.15)

        live = mgr._live.get(s.id)
        assert live is not None and live.state == SessionState.ACTIVE

        frames = [json.loads(b) for b in parked._stdin_buf]
        assert frames and frames[0]["type"] == "ticket_push", (
            f"expected the FIRST stdin frame after resume to be ticket_push; got {frames}"
        )
        scopes = {scope for (_sid, scope) in fake_tickets.minted}
        assert scopes == {"main", "mcp"}
        assert fake_tickets.revoked == [s.id]

        await mgr.kill(s.id, reason="test_done")
        for t in [attach_task, attach_task2]:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    asyncio.run(_run())


def test_legacy_runner_force_respawned(tmp_path, monkeypatch):
    """A PAUSED session this process has no record of ever having pushed a
    current-protocol ticket to must be force-respawned on resume — never
    reconnected: an old runner may not understand the ticket_push stdin
    frame (AC-G-resume-legacy)."""
    import app.chat.manager as manager_mod

    fake_tickets = _FakeTicketRepo()
    monkeypatch.setattr(manager_mod, "ticket_repo", lambda: fake_tickets)

    mgr = _make_pause_manager(tmp_path, linger_seconds=0)
    monkeypatch_workdir(mgr)
    provider = mgr._provider

    async def _run():
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws))
        await asyncio.sleep(0.05)
        await mgr.detach_sink(s.id, ws)
        await asyncio.sleep(0.15)  # pause fires
        assert provider.paused
        assert len(provider.spawned) == 1

        # Simulate a pre-broker (legacy) runner: this process never recorded
        # having pushed it a current-protocol ticket.
        mgr._known_protocol_sessions.discard(s.id)

        ws2 = FakeWS()
        attach_task2 = asyncio.create_task(mgr.attach(s.id, ws2))
        await asyncio.sleep(0.15)

        assert len(provider.spawned) == 2, "legacy session must be force-respawned, not resumed"
        # provider.resume() was never invoked (fresh spawn instead), AND the old
        # paused sandbox is destroyed rather than orphaned — resuming a legacy
        # session must not leak a billable microVM (Devin review on #849).
        assert len(provider.destroyed) == 1, "the old paused sandbox must be destroyed on legacy respawn"
        assert not provider.paused, "no paused sandbox may be left orphaned after a legacy respawn"
        # The paused session's old broker tickets must be revoked on the legacy
        # respawn, not left redeemable until TTL (Devin review on #851).
        assert s.id in fake_tickets.revoked, "old broker tickets must be revoked on legacy respawn"
        live = mgr._live.get(s.id)
        assert live is not None and live.state == SessionState.ACTIVE
        assert s.id in mgr._known_protocol_sessions, "the fresh spawn must record the new protocol marker"

        await mgr.kill(s.id, reason="test_done")
        for t in [attach_task, attach_task2]:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    asyncio.run(_run())


def test_legacy_resume_destroys_old_sandbox_before_clearing(tmp_path):
    """Post-restart (legacy) resume must destroy the old paused sandbox BEFORE
    clearing its ref — clearing first NULLs sandbox_paused_at so the reaper can
    never reap it, leaking a billable microVM per session per restart (§11)."""
    import unittest.mock as mock

    async def _run():
        mgr = _make_pause_manager(tmp_path)
        monkeypatch_workdir(mgr)
        session = mgr._repo.create_session(user_email="leak@test.com", surface=Surface.WEB)
        mgr._repo.set_sandbox_ref(session.id, sandbox_id="old-sbx-123", runner_pid=999)
        row = mgr._repo.get_session(session.id)

        order: list = []
        orig_destroy = mgr._provider.destroy

        async def _destroy(*, sandbox_id):
            order.append(("destroy", sandbox_id))
            return await orig_destroy(sandbox_id=sandbox_id)

        mgr._provider.destroy = _destroy
        orig_clear = mgr._repo.clear_sandbox_ref

        def _clear(sid):
            order.append(("clear", sid))
            return orig_clear(sid)

        mgr._repo.clear_sandbox_ref = _clear
        mgr._spawn_live = mock.AsyncMock(return_value=mock.MagicMock())

        assert session.id not in mgr._known_protocol_sessions  # legacy path
        await mgr._resume_from_row(row)

        assert ("destroy", "old-sbx-123") in order, order
        assert order.index(("destroy", "old-sbx-123")) < order.index(("clear", session.id)), order
        mgr._spawn_live.assert_awaited_once()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Wave-2C task 3: paused-sandbox sweep leader lease
# ---------------------------------------------------------------------------

from app.chat.manager import _PAUSED_SWEEP_LEASE_NAME  # noqa: E402
from app.coordination.factory import coordination  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_coordination_for_sweep_tests():
    reset_coordination_for_tests()
    yield
    reset_coordination_for_tests()


def _paused_expired_session(mgr):
    """Create a session whose sandbox_paused_at is already past the TTL —
    the exact repo-row shape _reap_once's paused sweep looks for."""
    from datetime import datetime as _dt, timedelta, timezone as _tz

    session = mgr._repo.create_session(user_email="sweep@test.com", surface=Surface.WEB)
    mgr._repo.set_sandbox_ref(session.id, sandbox_id="sbx-sweep", runner_pid=123)
    mgr._repo.set_sandbox_paused_at(
        session.id,
        _dt.now(_tz.utc) - timedelta(seconds=mgr._config.paused_ttl_seconds + 1),
    )
    return session


def test_paused_sweep_skips_when_lease_held_elsewhere(tmp_path):
    """If another replica holds the paused-sandbox-sweep lease, this
    replica's _reap_once must NOT destroy/clear the sandbox this tick —
    it should defer to whoever holds the lease and try again next tick."""

    async def _run():
        mgr = _make_pause_manager(tmp_path, linger_seconds=0)
        monkeypatch_workdir(mgr)
        session = _paused_expired_session(mgr)

        # Simulate another replica already holding the sweep lease.
        assert coordination().lease_acquire(_PAUSED_SWEEP_LEASE_NAME, "other-replica", ttl_s=90)

        await mgr._reap_once()

        row = mgr._repo.get_session(session.id)
        assert row.sandbox_id == "sbx-sweep", "sweep must have skipped — lease held elsewhere"
        assert "sbx-sweep" not in mgr._provider.destroyed

    asyncio.run(_run())


def test_paused_sweep_runs_when_lease_acquired_and_releases_after(tmp_path):
    """The normal (uncontended) path: this replica acquires the lease,
    performs the sweep, and releases the lease afterwards — a subsequent
    acquirer must not have to wait out the TTL."""

    async def _run():
        mgr = _make_pause_manager(tmp_path, linger_seconds=0)
        monkeypatch_workdir(mgr)
        session = _paused_expired_session(mgr)

        await mgr._reap_once()

        row = mgr._repo.get_session(session.id)
        assert row.sandbox_id is None, "sweep must have destroyed/cleared the expired sandbox"
        assert "sbx-sweep" in mgr._provider.destroyed

        # Released, not just expired — a fresh acquirer gets it immediately.
        assert coordination().lease_acquire(_PAUSED_SWEEP_LEASE_NAME, "someone-else", ttl_s=90) is True

    asyncio.run(_run())


def test_paused_sweep_releases_routing_lease(tmp_path):
    """A session torn down via the paused-sandbox-TTL sweep's destroy path
    (`self._live.pop(session.id, None)` directly, bypassing kill()) must
    have its routing lease released immediately rather than left to
    self-heal at the lease's own TTL (Minor finding)."""
    from app.chat import routing

    async def _run():
        mgr = _make_pause_manager(tmp_path, linger_seconds=0)
        monkeypatch_workdir(mgr)
        session = _paused_expired_session(mgr)

        # Simulate the session having previously claimed its routing lease
        # (the normal spawn/resume path) before it was paused.
        gw = routing.this_gateway_id()
        assert routing.claim_session(session.id, gw, ttl_s=180) is True
        assert routing.owner_of(session.id) == gw

        await mgr._reap_once()

        assert routing.owner_of(session.id) is None, "paused-sweep teardown must release the routing lease"
        # Freed immediately, not just expired — another gateway can claim
        # it right away instead of waiting out the TTL.
        assert routing.claim_session(session.id, "other-gateway:999", ttl_s=60) is True

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Wave-2F task 1: session routing leases (app/chat/routing.py) wiring
# ---------------------------------------------------------------------------


def test_spawn_claims_routing_lease(manager: ChatManager):
    """_spawn_live claims `chat:{chat_id}` for this gateway as soon as the
    session is registered in self._live."""
    from app.chat import routing

    async def _run():
        handle = FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(manager.attach(s.id, ws))
        await asyncio.sleep(0.05)

        assert routing.owner_of(s.id) == routing.this_gateway_id()

        await manager.kill(s.id, reason="test_done")
        handle.emit_eof()
        await attach_task

    asyncio.run(_run())


def test_kill_releases_routing_lease(manager: ChatManager):
    """kill() releases the routing lease so a takeover (or a later respawn
    on another gateway) doesn't have to wait out the TTL."""
    from app.chat import routing

    async def _run():
        handle = FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(manager.attach(s.id, ws))
        await asyncio.sleep(0.05)
        assert routing.owner_of(s.id) is not None

        await manager.kill(s.id, reason="test_done")
        handle.emit_eof()
        await attach_task

        assert routing.owner_of(s.id) is None
        # Freed, not just expired — another gateway could claim it right away.
        assert routing.claim_session(s.id, "other-gateway:999", ttl_s=60) is True

    asyncio.run(_run())


def test_renew_routing_leases_keeps_ownership(manager: ChatManager):
    """_renew_routing_leases (invoked from _reap_once's ~60s tick) extends
    the lease for every non-DEAD live session without changing ownership."""
    from app.chat import routing

    async def _run():
        handle = FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(manager.attach(s.id, ws))
        await asyncio.sleep(0.05)
        gw = routing.this_gateway_id()
        assert routing.owner_of(s.id) == gw

        await manager._renew_routing_leases()

        assert routing.owner_of(s.id) == gw

        await manager.kill(s.id, reason="test_done")
        handle.emit_eof()
        await attach_task

    asyncio.run(_run())


def test_spawn_continues_when_routing_lease_contended(manager: ChatManager):
    """Another gateway holding `chat:{chat_id}` for a session that was
    never actually spawned anywhere (no sandbox_id/runner_pid persisted —
    e.g. a bare claim_session call, as simulated here) must not block this
    replica from serving it: attach() now runs the wave-2F task 5
    cross-gateway takeover path (steal the lease, no-op destroy since there
    is no old sandbox, fresh spawn), so the session ends up live here AND
    this replica ends up the genuine lease owner — not just "serving
    despite a lost claim" (task 1's original mechanism-only posture, now
    superseded by task 5's real takeover)."""
    from app.chat import routing

    async def _run():
        handle = FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)

        assert routing.claim_session(s.id, "other-gateway:999", ttl_s=60) is True

        ws = FakeWS()
        attach_task = asyncio.create_task(manager.attach(s.id, ws))
        await asyncio.sleep(0.05)

        assert s.id in manager._live  # served locally via takeover
        assert routing.owner_of(s.id) == routing.this_gateway_id(), "takeover claims the lease for real"

        await manager.kill(s.id, reason="test_done")
        handle.emit_eof()
        await attach_task

    asyncio.run(_run())


def test_renew_outage_keeps_serving_but_genuine_steal_tears_down(manager: ChatManager, monkeypatch):
    """Critical-3: `renew_session` returning False is ambiguous by design
    (see app.chat.routing's module docstring) — it collapses "another
    gateway genuinely stole the lease" and "the coordination backend is
    unreachable right now" into the same False. `_renew_routing_leases`
    must disambiguate with a second, independent `owner_of` read and only
    tear the local session down when that read POSITIVELY shows a
    different, concrete gateway holding it.

    Scenario A: the coordination backend itself is unreachable (both
    `lease_renew` and `lease_owner` raise `CoordinationUnavailable`, which
    `app.chat.routing` degrades to False/None respectively) — there is no
    positive proof of loss, so the session must keep being served locally.

    Scenario B: a genuine steal — a different, concrete gateway actually
    holds the lease (claimed for real against the same shared coordination
    backend) — renew fails AND `owner_of` positively names someone else.
    This must tear the session down.

    Load-bearing: reverting the Critical-3 fix in
    `ChatManager._renew_routing_leases` (`git stash`) makes Scenario A fail
    — the session is torn down on the bare coordination outage even
    though nobody actually took it over.
    """
    import app.chat.routing as routing_mod
    from app.chat import routing
    from app.coordination.base import CoordinationUnavailable

    async def _run():
        handle = FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(manager.attach(s.id, ws))
        # Poll (not a fixed 50ms sleep) for attach to register the session —
        # under CI xdist CPU contention the bare sleep flaked this assert.
        await _wait_until(lambda: s.id in manager._live)
        assert s.id in manager._live

        # --- Scenario A: coordination-backend outage. Both renew_session
        # and owner_of degrade the same way — no way to positively
        # attribute the failed renew to a genuine steal, so this must NOT
        # tear the session down.
        class _BrokenBackend:
            def lease_renew(self, *a, **k):
                raise CoordinationUnavailable("boom")

            def lease_owner(self, *a, **k):
                raise CoordinationUnavailable("boom")

        with monkeypatch.context() as m:
            m.setattr(routing_mod, "coordination", lambda: _BrokenBackend())
            await manager._renew_routing_leases()

        assert s.id in manager._live, (
            "a renew failure that degrades from a coordination-backend outage "
            "(not a positively-confirmed steal) must NOT tear the session down"
        )
        assert manager._live[s.id].state != SessionState.DEAD

        # --- Scenario B: genuine steal against the REAL (memory) backend —
        # a different, concrete gateway actually now holds the lease.
        gw = routing.this_gateway_id()
        assert routing.owner_of(s.id) == gw, "sanity: still ours after the outage blip in Scenario A"
        routing.release_session(s.id, gw)
        assert routing.claim_session(s.id, "other-gateway:999", ttl_s=60) is True

        await manager._renew_routing_leases()

        assert s.id not in manager._live, (
            "a renew failure WITH owner_of positively showing a different gateway must tear the session down"
        )

        handle.killed = True
        try:
            await asyncio.wait_for(attach_task, timeout=1.0)
        except asyncio.TimeoutError:
            attach_task.cancel()

    asyncio.run(_run())


def test_routing_lease_calls_offloaded_to_thread(manager: ChatManager):
    """Important finding: _claim_routing_lease/_renew_routing_leases must
    run the coordination-backend lease call via asyncio.to_thread, not
    synchronously on the event loop — under the redis backend each is a
    blocking socket round-trip (WATCH/MULTI/EXEC) that would otherwise
    stall the whole process for every live session on every reaper tick.

    Proof: install a coordination backend whose lease_acquire/lease_renew
    block synchronously for a noticeable duration, then confirm a
    concurrently-running coroutine keeps making progress (its tick counter
    advances) while the lease call is in flight. If the lease call ran
    on-loop instead of via to_thread, the ticker would be starved and the
    counter would stay near zero.
    """
    import time as _time

    import app.coordination.factory as factory
    from app.coordination.memory import MemoryCoordinationBackend

    class _SlowLeaseBackend(MemoryCoordinationBackend):
        """MemoryCoordinationBackend whose lease primitives block
        synchronously — simulates a slow Redis round-trip."""

        def __init__(self, delay: float) -> None:
            super().__init__()
            self.delay = delay

        def lease_acquire(self, name, holder_id, *, ttl_s):
            _time.sleep(self.delay)
            return super().lease_acquire(name, holder_id, ttl_s=ttl_s)

        def lease_renew(self, name, holder_id, *, ttl_s):
            _time.sleep(self.delay)
            return super().lease_renew(name, holder_id, ttl_s=ttl_s)

    async def _run():
        factory._instance = _SlowLeaseBackend(delay=0.3)
        try:
            ticks = 0

            async def _ticker():
                nonlocal ticks
                for _ in range(60):
                    await asyncio.sleep(0.01)
                    ticks += 1

            ticker_task = asyncio.create_task(_ticker())
            await manager._claim_routing_lease("chat-claim-slow")
            await asyncio.sleep(0)  # let the ticker record its latest tick

            # A 0.3s blocking call, if truly offloaded, overlaps with ~30
            # ticker iterations (0.01s each); a call that instead blocked
            # the loop would leave `ticks` near 0.
            assert ticks > 10, f"event loop appears stalled during _claim_routing_lease (ticks={ticks})"

            ticker_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await ticker_task

            # Same proof for the reaper's per-tick renew path — needs a
            # live (non-DEAD) session in self._live to iterate over.
            from datetime import datetime, timezone

            from app.chat.manager import LiveSession

            manager._live["chat-claim-slow"] = LiveSession(
                chat_id="chat-claim-slow",
                user_email="u@x",
                state=SessionState.ACTIVE,
                handle=None,
                started_at=datetime.now(timezone.utc),
                last_activity=datetime.now(timezone.utc),
                sinks=[],
            )
            ticks = 0
            ticker_task = asyncio.create_task(_ticker())
            await manager._renew_routing_leases()
            await asyncio.sleep(0)
            ticker_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await ticker_task
            assert ticks > 10, f"event loop appears stalled during _renew_routing_leases (ticks={ticks})"
        finally:
            reset_coordination_for_tests()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# _resume_live reentrancy guard: concurrent resume must not double-spawn
# ---------------------------------------------------------------------------


def test_concurrent_resume_live_serialized_no_double_spawn(tmp_path, monkeypatch):
    """Two concurrent ``_resume_live`` calls on one PAUSED session (attach()
    racing a simulated inbound-consumer wake) must resume the sandbox
    exactly once.

    Without ``LiveSession._resume_lock`` this races: both calls reach
    ``FakeProvider.resume`` concurrently, which pops the parked handle out
    of ``provider.paused`` — whichever call wins the pop succeeds, and the
    loser's own ``sandbox_id not in self.paused`` check now fails, so it
    falls back to ``_respawn_fresh`` and spawns a SECOND, entirely new
    sandbox. The winner's already-resumed handle is then silently
    overwritten on ``live.handle`` by the loser's fresh spawn and never
    referenced again — an orphaned, still-billable sandbox leak — while the
    winner's own crash-respawn wait task (bound to that now-unreferenced
    handle) is left running, unmonitored, alongside the loser's fresh
    pump/wait pair: 3 alive tasks instead of 2.

    This test is proven load-bearing: reverting the `_resume_lock` guard in
    `ChatManager._resume_live` (`git stash` the fix in `app/chat/manager.py`)
    makes it fail — `len(provider.spawned) == 2` and 3 alive tasks — and it
    passes once the lock guards the method.
    """
    import app.chat.manager as manager_mod

    fake_tickets = _FakeTicketRepo()
    monkeypatch.setattr(manager_mod, "ticket_repo", lambda: fake_tickets)

    mgr = _make_pause_manager(tmp_path, linger_seconds=0)
    monkeypatch_workdir(mgr)
    provider = mgr._provider

    # Inject realistic resume() latency (per the task) so two concurrent
    # _resume_live callers actually interleave inside the provider call
    # instead of one completing before the other is even scheduled.
    orig_resume = provider.resume

    async def _slow_resume(*, sandbox_id, runner_pid, env):
        await asyncio.sleep(0.05)
        return await orig_resume(sandbox_id=sandbox_id, runner_pid=runner_pid, env=env)

    provider.resume = _slow_resume

    async def _run():
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws))
        await asyncio.sleep(0.1)
        assert s.id in mgr._known_protocol_sessions, "precondition: non-legacy (known-protocol) resume path"
        await mgr.detach_sink(s.id, ws)
        await asyncio.sleep(0.15)  # linger=0, pause fires
        assert provider.paused, "precondition: sandbox must be parked (paused) before the race"
        assert len(provider.spawned) == 1
        live = mgr._live[s.id]
        assert live.state == SessionState.PAUSED

        # Two concurrent resume triggers on the SAME PAUSED LiveSession —
        # mirrors attach() (WS reconnect) racing _inbound_consumer_loop's
        # resume-on-wake call, both hitting the same PAUSED session.
        results = await asyncio.gather(
            mgr._resume_live(live),
            mgr._resume_live(live),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                raise r

        assert len(provider.spawned) == 1, (
            f"expected exactly ONE resume/spawn for the session, got {len(provider.spawned)} total spawns "
            "— a second spawn means the race produced a leaked, orphaned sandbox"
        )
        assert not provider.paused, f"no sandbox should be left parked/leaked in the provider: {provider.paused}"
        assert live.state == SessionState.ACTIVE
        alive_tasks = [t for t in live.tasks if not t.done()]
        assert len(alive_tasks) == 2, (
            f"expected exactly 2 live tasks (pump+wait), got {len(alive_tasks)} "
            "— extra tasks are orphaned pump/wait survivors from a double-resume"
        )

        await mgr.kill(s.id, reason="test_done")
        attach_task.cancel()
        try:
            await attach_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# attach() + send_user_message() session-lock guard: concurrent spawn/resume
# decisions for a chat_id not yet in _live must not double-spawn.
# ---------------------------------------------------------------------------


def test_concurrent_attach_and_send_user_message_no_double_spawn(tmp_path, monkeypatch):
    """attach() (WS reconnect) and send_user_message() (e.g. an inbound
    webhook) racing the SAME post-restart chat_id — no LiveSession in
    memory yet, but the repo row still carries sandbox_id/runner_pid from
    before — must resume exactly ONE runner (no second spawn), and the
    message must reach that single runner.

    Setup mirrors ``test_attach_after_restart_resumes_from_repo_row``:
    spawn+pause a session, then ``mgr._live.clear()`` to simulate the
    in-memory state a process restart leaves behind, while the repo row
    keeps its sandbox refs.

    Without wrapping send_user_message's own "no local live session yet"
    resume-from-row decision in the same ``self._get_session_lock(chat_id)``
    attach() uses, both coroutines read ``self._live.get(chat_id)`` as
    ``None``, both see the repo row's sandbox refs, and both call
    ``_resume_from_row`` concurrently and unserialized against each other.
    ``_resume_from_row``'s own ``provider.resume()`` pops the parked handle
    out of the fake provider's ``paused`` dict — only one caller's pop can
    win; the loser's resume raises, and ``_resume_from_row`` reacts by
    destroying the (now-resumed, still-billable) sandbox and clearing its
    ref, and its caller then falls back to a brand new ``_spawn_live`` —
    a second spawn for a session that should have been a pure resume,
    while the winner's freshly-resumed handle is torn down out from under
    it by that same destroy call.

    This test is proven load-bearing: reverting the session-lock wrap
    around send_user_message's spawn/resume decision (``git stash`` the fix
    in ``app/chat/manager.py``) makes it fail — a second entry appears in
    ``provider.spawned`` (or the race raises/corrupts state entirely) —
    and it passes once the decision is serialized under the same
    per-chat_id lock as ``attach()``.
    """
    import app.chat.manager as manager_mod

    fake_tickets = _FakeTicketRepo()
    monkeypatch.setattr(manager_mod, "ticket_repo", lambda: fake_tickets)

    mgr = _make_pause_manager(tmp_path, linger_seconds=0)
    monkeypatch_workdir(mgr)
    provider = mgr._provider

    async def _run():
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws1 = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws1))
        await asyncio.sleep(0.05)
        assert s.id in mgr._known_protocol_sessions, "precondition: non-legacy (known-protocol) resume path"
        await mgr.detach_sink(s.id, ws1)
        await asyncio.sleep(0.15)  # linger=0, pause fires
        assert provider.paused, "precondition: sandbox must be parked (paused) before the race"
        assert len(provider.spawned) == 1

        # Simulate server restart: clear in-memory _live. The repo row
        # keeps its sandbox_id/runner_pid, so both racers below see "no
        # local live session, but a resumable repo row" — exactly the
        # window send_user_message's own decision used to run unlocked.
        mgr._live.clear()

        # Inject realistic resume() latency so attach() and
        # send_user_message() actually interleave inside the race window
        # instead of one completing before the other is even scheduled.
        orig_resume = provider.resume

        async def _slow_resume(*, sandbox_id, runner_pid, env):
            await asyncio.sleep(0.05)
            return await orig_resume(sandbox_id=sandbox_id, runner_pid=runner_pid, env=env)

        provider.resume = _slow_resume

        ws2 = FakeWS()
        attach_task2 = asyncio.create_task(mgr.attach(s.id, ws2))
        send_task = asyncio.create_task(mgr.send_user_message(s.id, "hello after restart"))
        results = await asyncio.gather(attach_task2, send_task, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                raise r

        assert len(provider.spawned) == 1, (
            f"expected NO additional spawn (a pure resume), got {len(provider.spawned)} total spawns "
            "— a second spawn means attach() and send_user_message() raced _resume_from_row independently"
        )
        assert not provider.paused, f"no sandbox should be left parked/leaked in the provider: {provider.paused}"
        live = mgr._live[s.id]
        assert live.state == SessionState.ACTIVE
        alive_tasks = [t for t in live.tasks if not t.done()]
        assert len(alive_tasks) == 2, (
            f"expected exactly 2 live tasks (pump+wait), got {len(alive_tasks)} "
            "— extra tasks are orphaned pump/wait survivors from a double-resume/double-spawn race"
        )

        # The message must have reached the single (resumed) runner's
        # stdin exactly once — not lost, not duplicated onto an orphaned
        # second runner.
        handle = live.handle
        payloads = [json.loads(b.decode()) for b in handle._stdin_buf]
        user_msgs = [p for p in payloads if p.get("type") == "user_msg"]
        assert len(user_msgs) == 1, f"expected exactly one user_msg delivered, got {user_msgs}"
        assert user_msgs[0]["text"] == "hello after restart"

        await mgr.kill(s.id, reason="test_done")
        for t in [attach_task, attach_task2]:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    asyncio.run(_run())
