"""Tests for outbound frame replay on WS reconnect (wave-2F task 3).

Three layers, matching the implementation:

- ``TestStreamContract`` — ``CoordinationBackend.stream_append``/
  ``stream_read`` against BOTH implementations (memory, fakeredis), same
  parametrized-fixture pattern as tests/test_coordination_contract.py.
- ``TestReplaySince`` — ``app.chat.replay.append_frame``/``replay_since``
  unit-level, against the memory coordination singleton.
- ``TestWsReconnectIntegration`` — the real ``app.api.chat.ws_stream``
  route function called directly (no FastAPI TestClient WS transport —
  see the module-level ``_RouteWS`` stub) against a real ``ChatManager``
  with a ``FakeHandle``, exercising the actual emit -> stream-append ->
  detach -> reconnect -> replay path end to end.

Uses asyncio.run() per the project convention (no pytest-asyncio
required) — see tests/test_chat_manager.py / test_chat_frame_envelope.py.
"""

from __future__ import annotations

import asyncio
import types
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import duckdb
import pytest

fakeredis = pytest.importorskip("fakeredis")

from starlette.websockets import WebSocketDisconnect  # noqa: E402

from src.db import _ensure_schema  # noqa: E402
from tests.chat_fakes import FakeHandle, FakeWS  # noqa: E402

import app.api.chat as chat_mod  # noqa: E402
from app.chat.config import ChatConfig  # noqa: E402
from app.chat.manager import ChatManager  # noqa: E402
from app.chat.persistence import ChatRepository  # noqa: E402
from app.chat.replay import ReplayOutcome, append_frame, replay_since  # noqa: E402
from app.chat.frame_seq import stamp_frame  # noqa: E402
from app.chat.types import Surface  # noqa: E402
from app.chat.workdir import WorkdirManager  # noqa: E402
from app.coordination.base import CoordinationUnavailable  # noqa: E402
from app.coordination.factory import coordination, reset_coordination_for_tests  # noqa: E402
from app.coordination.memory import MemoryCoordinationBackend  # noqa: E402
from app.coordination.redis_backend import RedisCoordinationBackend  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_coordination():
    """The chat-seq counter and chat-out stream both live in the
    coordination backend singleton, which persists across tests unless
    reset — same rationale as tests/test_chat_frame_envelope.py."""
    reset_coordination_for_tests()
    yield
    reset_coordination_for_tests()


# ---------------------------------------------------------------------------
# CoordinationBackend.stream_append / stream_read contract
# ---------------------------------------------------------------------------


def _memory_backend() -> MemoryCoordinationBackend:
    return MemoryCoordinationBackend()


def _fakeredis_backend() -> RedisCoordinationBackend:
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    return RedisCoordinationBackend(client)


_BUILDERS = {"memory": _memory_backend, "fakeredis": _fakeredis_backend}


@pytest.fixture(params=["memory", "fakeredis"])
def backend(request):
    b = _BUILDERS[request.param]()
    yield b
    close = getattr(b, "close", None)
    if callable(close):
        close()


class TestStreamContract:
    def test_append_then_read_returns_all_in_order(self, backend):
        for i in range(1, 4):
            backend.stream_append("s1", {"seq": i, "text": f"f{i}"}, maxlen=100)
        entries = backend.stream_read("s1")
        assert [e["seq"] for e in entries] == [1, 2, 3]

    def test_after_seq_filters_to_strictly_greater(self, backend):
        for i in range(1, 6):
            backend.stream_append("s2", {"seq": i}, maxlen=100)
        entries = backend.stream_read("s2", after_seq=3)
        assert [e["seq"] for e in entries] == [4, 5]

    def test_after_seq_beyond_latest_returns_empty(self, backend):
        backend.stream_append("s3", {"seq": 1}, maxlen=100)
        assert backend.stream_read("s3", after_seq=99) == []

    def test_read_missing_key_returns_empty_not_error(self, backend):
        assert backend.stream_read("never-appended") == []
        assert backend.stream_read("never-appended", after_seq=0) == []

    def test_maxlen_evicts_oldest(self, backend):
        for i in range(1, 8):
            backend.stream_append("s4", {"seq": i}, maxlen=3)
        entries = backend.stream_read("s4")
        # Only the 3 most-recently-appended entries survive.
        assert [e["seq"] for e in entries] == [5, 6, 7]

    def test_maxlen_eviction_then_after_seq_filter_shows_the_gap(self, backend):
        """The classic replay-gap detection signal: after eviction, the
        oldest retained entry's seq is > after_seq + 1, which is exactly
        what app.chat.replay.replay_since checks to decide full_refresh."""
        for i in range(1, 8):
            backend.stream_append("s5", {"seq": i}, maxlen=3)
        entries = backend.stream_read("s5", after_seq=1)
        assert entries, "expected the 3 retained entries to still show up"
        assert entries[0]["seq"] != 2  # seq 2 was evicted — gap, not contiguous

    def test_stream_append_returns_an_id(self, backend):
        entry_id = backend.stream_append("s6", {"seq": 1}, maxlen=10)
        assert entry_id
        assert isinstance(entry_id, str)

    def test_stream_read_sorts_by_seq_despite_out_of_order_append(self, backend):
        """MINOR fix (2026-07-18): append_frame now runs OUTSIDE
        ChatManager._broadcast_lock (see app.chat.manager._broadcast), so
        two concurrent broadcasts for the same session can complete their
        appends in an order that doesn't match their (locked) stamps.
        stream_read must correct for that on read, sorting by the frame's
        own ``seq`` field rather than trusting append order."""
        backend.stream_append("s7", {"seq": 5}, maxlen=100)
        backend.stream_append("s7", {"seq": 3}, maxlen=100)
        backend.stream_append("s7", {"seq": 4}, maxlen=100)
        entries = backend.stream_read("s7")
        assert [e["seq"] for e in entries] == [3, 4, 5]

    def test_after_seq_filter_applies_after_sorting_out_of_order_appends(self, backend):
        backend.stream_append("s8", {"seq": 5}, maxlen=100)
        backend.stream_append("s8", {"seq": 2}, maxlen=100)
        backend.stream_append("s8", {"seq": 4}, maxlen=100)
        backend.stream_append("s8", {"seq": 1}, maxlen=100)
        entries = backend.stream_read("s8", after_seq=2)
        assert [e["seq"] for e in entries] == [4, 5]


# ---------------------------------------------------------------------------
# app.chat.replay.append_frame / replay_since — unit level
# ---------------------------------------------------------------------------


class TestReplaySince:
    def test_zero_last_seq_is_a_noop_not_a_gap(self):
        async def _run():
            outcome = await replay_since("chat_a", 0)
            assert outcome == ReplayOutcome(frames=[], full_refresh=False)

        asyncio.run(_run())

    def test_emit_n_frames_then_reconnect_replays_after_last_seq_in_order(self):
        async def _run():
            frames = []
            for i in range(5):
                frame = stamp_frame("chat_b", {"type": "token", "text": f"t{i}"})
                await append_frame("chat_b", frame)
                frames.append(frame)
            # Client last saw seq=2 (frames[1]); reconnect should replay
            # seq 3, 4, 5 (frames[2:]) in order.
            outcome = await replay_since("chat_b", 2)
            assert outcome.full_refresh is False
            assert [f["seq"] for f in outcome.frames] == [3, 4, 5]
            assert [f["text"] for f in outcome.frames] == ["t2", "t3", "t4"]

        asyncio.run(_run())

    def test_last_seq_equal_to_current_returns_no_frames_no_refresh(self):
        async def _run():
            frame = stamp_frame("chat_c", {"type": "token"})
            await append_frame("chat_c", frame)
            outcome = await replay_since("chat_c", 1)
            assert outcome == ReplayOutcome(frames=[], full_refresh=False)

        asyncio.run(_run())

    def test_last_seq_older_than_evicted_window_triggers_full_refresh(self):
        async def _run():
            import app.chat.replay as replay_mod

            # Shrink maxlen for this test so eviction is reachable quickly.
            original_maxlen = replay_mod.STREAM_MAXLEN
            replay_mod.STREAM_MAXLEN = 3
            try:
                for i in range(7):
                    frame = stamp_frame("chat_d", {"type": "token", "text": f"t{i}"})
                    await append_frame("chat_d", frame)
                # last_seq=1 is long past the retained window (only seq
                # 5,6,7 survive maxlen=3) — must not return a partial/wrong
                # replay.
                outcome = await replay_since("chat_d", 1)
                assert outcome.full_refresh is True
                assert outcome.frames == []
            finally:
                replay_mod.STREAM_MAXLEN = original_maxlen

        asyncio.run(_run())

    def test_empty_stream_after_reset_triggers_full_refresh_not_error(self):
        """Simulates a coordination-backend FLUSHALL: the whole singleton
        (counter + stream) is dropped mid-session, so the client's
        last_seq now exceeds what a freshly-reset backend can prove."""

        async def _run():
            frame = stamp_frame("chat_e", {"type": "token"})
            await append_frame("chat_e", frame)
            assert frame["seq"] == 1

            reset_coordination_for_tests()  # FLUSHALL-equivalent

            outcome = await replay_since("chat_e", 1)
            assert outcome.full_refresh is True
            assert outcome.frames == []

        asyncio.run(_run())

    def test_coordination_unavailable_degrades_to_no_replay_not_an_error(self, monkeypatch):
        """A transient backend blip at reconnect time must not raise out
        of replay_since — it degrades to 'nothing to replay' (see
        app.chat.replay's module docstring for the rationale)."""

        async def _run():
            frame = stamp_frame("chat_f", {"type": "token"})
            await append_frame("chat_f", frame)

            def _raise_peek(*_a, **_kw):
                raise CoordinationUnavailable("blip")

            monkeypatch.setattr("app.chat.replay.peek_seq", _raise_peek)
            outcome = await replay_since("chat_f", 1)
            assert outcome == ReplayOutcome(frames=[], full_refresh=False)

        asyncio.run(_run())

    def test_append_frame_failure_is_swallowed(self, monkeypatch):
        """append_frame must never raise — a stream-append failure must
        not break live delivery (see app.chat.manager._broadcast, which
        calls this with no try/except of its own)."""

        async def _run():
            def _raise(*_a, **_kw):
                raise CoordinationUnavailable("blip")

            monkeypatch.setattr(coordination(), "stream_append", _raise)
            frame = stamp_frame("chat_g", {"type": "token"})
            await append_frame("chat_g", frame)  # must not raise

        asyncio.run(_run())

    def test_private_stamped_frame_hole_does_not_trigger_full_refresh(self):
        """IMPORTANT fix (2026-07-18): a frame that consumes a seq via
        stamp_frame but is never appended to the stream (e.g. the per-sink
        ``ready``/``runner_not_ready`` frames ChatManager._seat_sink /
        add_sink / app.api.chat send directly to exactly one connection —
        never broadcast, so never handed to append_frame) creates a
        legitimate hole in the stream's seq numbering. That hole must not
        false-positive a full_refresh: the client never received that
        frame live in the first place (it was private to a different
        connection), so there is nothing for THIS client to recover — the
        available entries should simply be replayed as-is."""

        async def _run():
            f1 = stamp_frame("chat_h", {"type": "token", "text": "t1"})
            await append_frame("chat_h", f1)  # seq=1, appended
            stamp_frame("chat_h", {"type": "ready"})  # seq=2, PRIVATE — never appended
            f3 = stamp_frame("chat_h", {"type": "token", "text": "t3"})
            await append_frame("chat_h", f3)  # seq=3, appended

            outcome = await replay_since("chat_h", 1)
            assert outcome.full_refresh is False
            assert [f["seq"] for f in outcome.frames] == [3]
            assert outcome.frames[0]["text"] == "t3"

        asyncio.run(_run())

    def test_genuine_eviction_below_maxlen_window_still_triggers_full_refresh(self):
        """Complements the private-frame-hole test above: a hole from a
        private frame is harmless, but last_seq pointing BELOW the
        retained MAXLEN window is a genuine, unrecoverable gap and must
        still trigger full_refresh — the eviction-based check
        (last_seq + 1 < min_retained_seq) must not become so permissive
        that it stops catching real evictions."""

        async def _run():
            import app.chat.replay as replay_mod

            original_maxlen = replay_mod.STREAM_MAXLEN
            replay_mod.STREAM_MAXLEN = 3
            try:
                for i in range(6):
                    frame = stamp_frame("chat_i", {"type": "token", "text": f"t{i}"})
                    await append_frame("chat_i", frame)
                # Only seq 4,5,6 retained (maxlen=3); last_seq=1 needs seq 2,
                # long since evicted.
                outcome = await replay_since("chat_i", 1)
                assert outcome.full_refresh is True
                assert outcome.frames == []
            finally:
                replay_mod.STREAM_MAXLEN = original_maxlen

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Memory-mode end-to-end: emit -> detach -> reconnect via the real ws_stream
# route function
# ---------------------------------------------------------------------------


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


class _RouteWS:
    """Minimal stand-in for the ``fastapi.WebSocket`` the real
    ``app.api.chat.ws_stream``/``ws_join`` route functions receive.

    Only implements what those routes actually touch: ``.app.state.chat_manager``,
    ``accept``/``send_json``/``close``, and a ``receive_json`` that raises
    ``WebSocketDisconnect`` immediately — the reconnect scenario here only
    cares about what gets sent between accept() and the reader loop
    starting (the replay + attach()'s own ready/turn_buffer replay), so an
    instantly-disconnecting reader loop is the cleanest way to capture
    exactly that window without needing a real ASGI WS transport.
    """

    def __init__(self, manager: ChatManager) -> None:
        self.sent: list[dict] = []
        self.closed: Optional[tuple[int, str]] = None
        self.app = types.SimpleNamespace(state=types.SimpleNamespace(chat_manager=manager))

    async def accept(self) -> None:
        pass

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    async def receive_json(self) -> dict:
        raise WebSocketDisconnect(code=1000)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)


class TestWsReconnectIntegration:
    def test_reconnect_after_turn_completes_replays_missed_frames_before_ready(self, tmp_path: Path):
        """The clean case: the turn that produced the missed frames has
        already finished (turn_buffer cleared) by the time the client
        reconnects, so everything the client missed comes ONLY from the
        replay stream — no turn_buffer resend to reason about."""
        manager = _make_manager(tmp_path)

        async def _run():
            handle = FakeHandle()
            manager._provider.spawn = AsyncMock(return_value=handle)

            s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
            ws1 = FakeWS()
            await manager.attach(s.id, ws1)
            await asyncio.sleep(0.02)
            handle.emit({"type": "token", "text": "a"})
            await asyncio.sleep(0.02)
            handle.emit({"type": "token", "text": "b"})
            await asyncio.sleep(0.02)
            handle.emit({"type": "token", "text": "c"})
            await asyncio.sleep(0.02)
            handle.emit({"type": "done"})  # clears turn_buffer
            await asyncio.sleep(0.05)

            # ws1 saw: ready(1), token a(2), token b(3), token c(4), done(5).
            seqs_seen = [m["seq"] for m in ws1.sent if isinstance(m.get("seq"), int)]
            assert seqs_seen == [1, 2, 3, 4, 5]

            # Client only rendered up through seq=2 (token a) before the
            # connection silently dropped (no clean detach signal reached
            # the server yet) — detach now, then reconnect claiming
            # last_seq=2.
            await manager.detach_sink(s.id, ws1)

            ticket = chat_mod._issue_ticket(s.id, "u@x")
            ws2 = _RouteWS(manager)
            await chat_mod.ws_stream(ws2, s.id, ticket, last_seq=2)

            # Replayed frames (seq 3, 4, 5 = token b, token c, done) come
            # first, in order, before the fresh `ready` frame attach()
            # sends — and NOT token a again (already at last_seq).
            replayed_types = [(f.get("type"), f.get("seq")) for f in ws2.sent if f.get("type") != "ready"]
            assert replayed_types == [("token", 3), ("token", 4), ("done", 5)]
            assert [f.get("text") for f in ws2.sent if f.get("type") == "token"] == ["b", "c"]
            ready_index = next(i for i, f in enumerate(ws2.sent) if f["type"] == "ready")
            assert ready_index == len(ws2.sent) - 1, "ready frame must be last (turn_buffer empty, nothing to add)"
            assert ws2.closed is None

            await manager.kill(s.id, reason="test_done")
            handle.emit_eof()

        asyncio.run(_run())

    def test_reconnect_mid_turn_replay_does_not_double_send_turn_buffer_frames(self, tmp_path: Path):
        """A reconnect while a turn is STILL in-flight: attach()'s own
        _seat_sink unconditionally replays the whole turn_buffer regardless
        of last_seq (pre-existing wave-2F task 2 behavior — it has no
        notion of last_seq). The replay-stream contribution for this same
        chat must defer entirely to that resend rather than ALSO sending
        the overlapping frames — otherwise the client would see the
        in-flight tail three times over (live delivery + stream replay +
        turn_buffer replay) instead of the expected two (turn_buffer
        replay is a known, client-deduped resend; a third copy would not
        be)."""
        manager = _make_manager(tmp_path)

        async def _run():
            handle = FakeHandle()
            manager._provider.spawn = AsyncMock(return_value=handle)

            s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
            ws1 = FakeWS()
            await manager.attach(s.id, ws1)
            await asyncio.sleep(0.02)
            handle.emit({"type": "token", "text": "a"})
            await asyncio.sleep(0.02)
            handle.emit({"type": "token", "text": "b"})
            await asyncio.sleep(0.02)
            handle.emit({"type": "token", "text": "c"})
            await asyncio.sleep(0.05)
            # Turn still in flight — no "done"/"assistant_message" yet, so
            # turn_buffer still holds all three token frames.
            assert manager._live[s.id].turn_buffer, "test setup expects an in-flight turn"

            await manager.detach_sink(s.id, ws1)

            ticket = chat_mod._issue_ticket(s.id, "u@x")
            ws2 = _RouteWS(manager)
            await chat_mod.ws_stream(ws2, s.id, ticket, last_seq=2)

            # Every frame appears exactly once — no duplicate seq from the
            # replay stream ALSO sending what the turn_buffer resend
            # already covers.
            token_frames = [f for f in ws2.sent if f.get("type") == "token"]
            token_seqs = [f["seq"] for f in token_frames]
            assert len(token_seqs) == len(set(token_seqs)), f"duplicate seq in replay: {token_seqs}"
            # The turn_buffer resend (attach()/_seat_sink) is the sole
            # source here — all three tokens, since it has no notion of
            # last_seq.
            assert [f["text"] for f in token_frames] == ["a", "b", "c"]

            await manager.kill(s.id, reason="test_done")
            handle.emit_eof()

        asyncio.run(_run())

    def test_reconnect_with_last_seq_zero_gets_no_replay(self, tmp_path: Path):
        """A client with no baseline (fresh WS, e.g. the very first open)
        must not trigger any replay attempt — omitted/0 last_seq is a
        no-op, not a gap (see app.chat.replay.replay_since)."""
        manager = _make_manager(tmp_path)

        async def _run():
            handle = FakeHandle()
            manager._provider.spawn = AsyncMock(return_value=handle)
            s = await manager.create_session(user_email="u@x", surface=Surface.WEB)

            ticket = chat_mod._issue_ticket(s.id, "u@x")
            ws = _RouteWS(manager)
            await chat_mod.ws_stream(ws, s.id, ticket, last_seq=0)

            # Only the fresh `ready` frame from attach()/_seat_sink — no
            # full_refresh, no replayed tokens (there weren't any anyway).
            assert [f["type"] for f in ws.sent] == ["ready"]

            await manager.kill(s.id, reason="test_done")
            handle.emit_eof()

        asyncio.run(_run())

    def test_reconnect_past_evicted_window_gets_full_refresh(self, tmp_path: Path):
        manager = _make_manager(tmp_path)

        async def _run():
            import app.chat.replay as replay_mod

            original_maxlen = replay_mod.STREAM_MAXLEN
            replay_mod.STREAM_MAXLEN = 2
            try:
                handle = FakeHandle()
                manager._provider.spawn = AsyncMock(return_value=handle)
                s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
                ws1 = FakeWS()
                await manager.attach(s.id, ws1)
                await asyncio.sleep(0.02)
                for i in range(5):
                    handle.emit({"type": "token", "text": f"t{i}"})
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0.05)
                await manager.detach_sink(s.id, ws1)

                # last_seq=1 (the `ready` frame) is long past the 2-entry
                # retained window — must get full_refresh, not a partial
                # or wrong replay.
                ticket = chat_mod._issue_ticket(s.id, "u@x")
                ws2 = _RouteWS(manager)
                await chat_mod.ws_stream(ws2, s.id, ticket, last_seq=1)

                assert ws2.sent[0]["type"] == "full_refresh"
                # Nothing else preceded it, and attach() still proceeds to
                # seat the sink normally afterwards.
                assert any(f["type"] == "ready" for f in ws2.sent[1:])

                await manager.kill(s.id, reason="test_done")
                handle.emit_eof()
            finally:
                replay_mod.STREAM_MAXLEN = original_maxlen

        asyncio.run(_run())

    def test_reconnect_after_coordination_reset_gets_full_refresh(self, tmp_path: Path):
        """FLUSHALL-equivalent mid-session: the coordination singleton is
        dropped (counter + stream both gone), so the client's remembered
        last_seq is now unverifiable — full_refresh, not silent data loss."""
        manager = _make_manager(tmp_path)

        async def _run():
            handle = FakeHandle()
            manager._provider.spawn = AsyncMock(return_value=handle)
            s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
            ws1 = FakeWS()
            await manager.attach(s.id, ws1)
            await asyncio.sleep(0.02)
            handle.emit({"type": "token", "text": "a"})
            await asyncio.sleep(0.05)
            await manager.detach_sink(s.id, ws1)

            seqs_seen = [m["seq"] for m in ws1.sent if isinstance(m.get("seq"), int)]
            last_seq = max(seqs_seen)

            reset_coordination_for_tests()  # drops chat-seq + chat-out entirely

            ticket = chat_mod._issue_ticket(s.id, "u@x")
            ws2 = _RouteWS(manager)
            await chat_mod.ws_stream(ws2, s.id, ticket, last_seq=last_seq)
            assert ws2.sent[0]["type"] == "full_refresh"

            await manager.kill(s.id, reason="test_done")
            handle.emit_eof()

        asyncio.run(_run())

    def test_frame_broadcast_during_seat_and_replay_window_is_not_lost(self, tmp_path: Path):
        """CRITICAL fix repro (2026-07-18): the reconnect path used to
        compute the gap replay (a snapshot of replay_since) BEFORE
        attach() seated the reconnecting connection as a live sink. A
        frame broadcast in that window landed in neither the snapshot
        (already read) nor live delivery (not seated yet) — silently
        lost, with no way for the client to detect the gap.

        This reproduces the exact window: monkeypatch ``replay_since`` (as
        called from ``app.api.chat._flush_gap_replay``) to broadcast a
        fresh frame the instant it's invoked — which, under the fix, runs
        strictly AFTER ``attach()`` has already seated the gate as a live
        sink, so the race frame must reach the gate's buffer directly
        rather than depend on the stream read racing it. Asserts the frame
        is received exactly once (no loss, no duplicate) and that overall
        delivery to the socket is seq-ordered (no reordering introduced by
        the gate's buffer/replay merge)."""
        manager = _make_manager(tmp_path)

        async def _run():
            handle = FakeHandle()
            manager._provider.spawn = AsyncMock(return_value=handle)

            s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
            ws1 = FakeWS()
            await manager.attach(s.id, ws1)
            await asyncio.sleep(0.02)
            handle.emit({"type": "token", "text": "a"})
            await asyncio.sleep(0.02)
            handle.emit({"type": "done"})  # clears turn_buffer
            await asyncio.sleep(0.05)

            seqs_seen = [m["seq"] for m in ws1.sent if isinstance(m.get("seq"), int)]
            last_seq = max(seqs_seen)  # client is fully caught up

            await manager.detach_sink(s.id, ws1)

            ticket = chat_mod._issue_ticket(s.id, "u@x")
            ws2 = _RouteWS(manager)

            original_replay_since = chat_mod.replay_since

            async def _racing_replay_since(chat_id, ls):
                # Simulate a broadcast landing exactly in the window this
                # bug used to leave open — by the time this runs (called
                # from _flush_gap_replay), attach() has ALREADY seated the
                # gate as a live sink (the fix), so this broadcast must not
                # be lost.
                await manager._broadcast(manager._live[s.id], {"type": "token", "text": "race"})
                return await original_replay_since(chat_id, ls)

            chat_mod.replay_since = _racing_replay_since
            try:
                await chat_mod.ws_stream(ws2, s.id, ticket, last_seq=last_seq)
            finally:
                chat_mod.replay_since = original_replay_since

            race_frames = [f for f in ws2.sent if f.get("text") == "race"]
            assert len(race_frames) == 1, f"race frame lost or duplicated: {ws2.sent}"

            # No out-of-order delivery at the socket: seq must be
            # non-decreasing across everything sent to the reconnecting
            # client.
            seqs = [f["seq"] for f in ws2.sent if isinstance(f.get("seq"), int)]
            assert seqs == sorted(seqs), f"frames delivered out of seq order: {seqs}"

            await manager.kill(s.id, reason="test_done")
            handle.emit_eof()

        asyncio.run(_run())

    def test_race_frame_during_mid_turn_reconnect_is_not_lost(self, tmp_path: Path):
        """Same CRITICAL race as above, but during a MID-TURN reconnect
        (turn_buffer non-empty at seat time) — the gate must merge the
        turn_buffer resend, the race frame, and the reconnect's own
        `ready` frame into one seq-ordered, duplicate-free delivery."""
        manager = _make_manager(tmp_path)

        async def _run():
            handle = FakeHandle()
            manager._provider.spawn = AsyncMock(return_value=handle)

            s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
            ws1 = FakeWS()
            await manager.attach(s.id, ws1)
            await asyncio.sleep(0.02)
            handle.emit({"type": "token", "text": "a"})
            await asyncio.sleep(0.02)
            handle.emit({"type": "token", "text": "b"})
            await asyncio.sleep(0.05)
            # Turn still in flight.
            assert manager._live[s.id].turn_buffer

            seqs_seen = [m["seq"] for m in ws1.sent if isinstance(m.get("seq"), int)]
            last_seq = max(seqs_seen)

            await manager.detach_sink(s.id, ws1)

            ticket = chat_mod._issue_ticket(s.id, "u@x")
            ws2 = _RouteWS(manager)

            original_replay_since = chat_mod.replay_since

            async def _racing_replay_since(chat_id, ls):
                await manager._broadcast(manager._live[s.id], {"type": "token", "text": "race"})
                return await original_replay_since(chat_id, ls)

            chat_mod.replay_since = _racing_replay_since
            try:
                await chat_mod.ws_stream(ws2, s.id, ticket, last_seq=last_seq)
            finally:
                chat_mod.replay_since = original_replay_since

            race_frames = [f for f in ws2.sent if f.get("text") == "race"]
            assert len(race_frames) == 1, f"race frame lost or duplicated: {ws2.sent}"
            seqs = [f["seq"] for f in ws2.sent if isinstance(f.get("seq"), int)]
            assert seqs == sorted(seqs), f"frames delivered out of seq order: {seqs}"
            # No duplicate seqs anywhere in the delivery (turn_buffer resend
            # + race broadcast + reconnect ready must not double-count).
            assert len(seqs) == len(set(seqs)), f"duplicate seq in delivery: {seqs}"

            await manager.kill(s.id, reason="test_done")
            handle.emit_eof()

        asyncio.run(_run())
