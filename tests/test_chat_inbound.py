"""Tests for app/chat/inbound.py + ChatManager's cross-gateway inbound
message routing (wave-2F task 4).

Two ``ChatManager`` instances sharing one ``ChatRepository`` (same DuckDB
connection) and the process-wide ``coordination()`` singleton simulate two
gateway replicas — the same "two simulated managers, one shared backend"
convention ``tests/test_chat_routing.py`` already established for the
routing-lease primitives themselves (task 1). ``app.chat.routing.
this_gateway_id`` is monkeypatched around each manager's calls to give the
two a distinct identity, since within one test process the real
``default_holder_id()`` (hostname:pid) is identical for both.

Uses asyncio.run() per the project convention (no pytest-asyncio required)
— see tests/test_chat_manager.py.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import duckdb
import pytest

from src.db import _ensure_schema
from tests.chat_fakes import FakeHandle

import app.chat.routing as routing_mod
from app.chat import inbound
from app.chat.config import ChatConfig
from app.chat.manager import ChatManager
from app.chat.persistence import ChatRepository
from app.chat.types import Surface
from app.chat.workdir import WorkdirManager
from app.coordination.base import CoordinationUnavailable
from app.coordination.factory import coordination, reset_coordination_for_tests


@pytest.fixture(autouse=True)
def _reset_coordination():
    """chat-in / chat-in-seq keys and routing leases all live in the
    coordination-backend singleton, which persists across tests unless
    reset — same rationale as tests/test_chat_routing.py."""
    reset_coordination_for_tests()
    yield
    reset_coordination_for_tests()


def _make_workdir_mgr(tmp_path: Path, repo: ChatRepository) -> WorkdirManager:
    bundled = tmp_path / "bundled"
    bundled.mkdir(parents=True)
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


def _make_manager(tmp_path: Path, repo: ChatRepository) -> ChatManager:
    workdir_mgr = _make_workdir_mgr(tmp_path, repo)
    provider = MagicMock()
    provider.spawn = AsyncMock()
    return ChatManager(
        provider=provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        config=ChatConfig(enabled=True, concurrency_per_user=2),
    )


@pytest.fixture
def two_gateways(tmp_path: Path) -> tuple[ChatManager, ChatManager]:
    """Two independent ChatManager instances sharing one ChatRepository/
    DuckDB connection — simulated gateway replicas A and B. Neither
    manager's ``_live`` dict is shared (each is a fresh instance), matching
    two separate gateway PROCESSES that happen to share both the
    coordination backend and the underlying session storage."""
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    mgr_a = _make_manager(tmp_path / "gw-a", repo)
    mgr_b = _make_manager(tmp_path / "gw-b", repo)
    return mgr_a, mgr_b


def _as_gateway(monkeypatch: pytest.MonkeyPatch, gateway_id: str) -> None:
    monkeypatch.setattr(routing_mod, "this_gateway_id", lambda: gateway_id)


async def _spawn_owned_session(mgr: ChatManager) -> tuple[str, FakeHandle]:
    """Create + attach a session on ``mgr`` so it becomes the live owner —
    claims the routing lease under whatever gateway id is currently patched
    onto app.chat.routing.this_gateway_id."""
    handle = FakeHandle()
    mgr._provider.spawn = AsyncMock(return_value=handle)
    session = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
    ws = MagicMock()
    ws.send_json = AsyncMock()
    asyncio.create_task(mgr.attach(session.id, ws))
    await asyncio.sleep(0.05)
    return session.id, handle


def _stdin_texts(handle: FakeHandle) -> list[str]:
    out = []
    for b in handle._stdin_buf:
        frame = json.loads(b.decode("utf-8"))
        if frame.get("type") == "user_msg":
            out.append(frame["text"])
    return out


async def _wait_until(predicate, *, attempts: int = 40, interval: float = 0.05) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(interval)
    assert predicate(), "condition never became true"


# ---------------------------------------------------------------------------
# app.chat.inbound primitives
# ---------------------------------------------------------------------------


class TestInboundPrimitives:
    def test_publish_inbound_assigns_increasing_seq(self):
        async def _run():
            seq1 = await inbound.publish_inbound("chat-x", "one")
            seq2 = await inbound.publish_inbound("chat-x", "two")
            assert seq1 == 1
            assert seq2 == 2

        asyncio.run(_run())

    def test_read_new_returns_entries_after_seq_in_order(self):
        async def _run():
            await inbound.publish_inbound("chat-y", "a")
            await inbound.publish_inbound("chat-y", "b")
            await inbound.publish_inbound("chat-y", "c")
            entries = inbound.read_new("chat-y", after_seq=1)
            assert [e["text"] for e in entries] == ["b", "c"]
            assert [e["seq"] for e in entries] == [2, 3]

        asyncio.run(_run())

    def test_read_new_after_seq_zero_returns_everything(self):
        async def _run():
            await inbound.publish_inbound("chat-z", "a")
            entries = inbound.read_new("chat-z", after_seq=0)
            assert len(entries) == 1
            assert entries[0]["text"] == "a"

        asyncio.run(_run())

    def test_read_new_unknown_chat_id_returns_empty(self):
        assert inbound.read_new("chat-never-published", after_seq=0) == []

    def test_publish_inbound_raises_inbound_publish_failed_on_coordination_unavailable(self, monkeypatch):
        class _Broken:
            def incr(self, *a, **k):
                raise CoordinationUnavailable("boom")

        monkeypatch.setattr(inbound, "coordination", lambda: _Broken())

        async def _run():
            with pytest.raises(inbound.InboundPublishFailed):
                await inbound.publish_inbound("chat-broken", "hi")

        asyncio.run(_run())

    def test_read_new_degrades_to_empty_on_coordination_unavailable(self, monkeypatch):
        class _Broken:
            def stream_read(self, *a, **k):
                raise CoordinationUnavailable("boom")

        monkeypatch.setattr(inbound, "coordination", lambda: _Broken())
        assert inbound.read_new("chat-broken-2", after_seq=0) == []

    def test_subscribe_notify_returns_none_on_coordination_unavailable(self, monkeypatch):
        class _Broken:
            def subscribe(self, *a, **k):
                raise CoordinationUnavailable("boom")

        monkeypatch.setattr(inbound, "coordination", lambda: _Broken())
        assert inbound.subscribe_notify("chat-broken-3", lambda m: None) is None


# ---------------------------------------------------------------------------
# ChatManager cross-gateway routing
# ---------------------------------------------------------------------------


class TestCrossGatewayRouting:
    def test_owner_delivers_directly_no_stream_write(self, two_gateways, monkeypatch):
        """Memory-mode / owner path: the message goes straight to the local
        runner's stdin, and the inbound coordination stream is never
        touched at all — verifies no double-delivery machinery kicks in
        when this gateway already owns the session."""
        mgr_a, _mgr_b = two_gateways

        async def _run():
            _as_gateway(monkeypatch, "gw-a")
            chat_id, handle = await _spawn_owned_session(mgr_a)

            await mgr_a.send_user_message(chat_id, "hello")

            assert _stdin_texts(handle) == ["hello"]
            assert inbound.read_new(chat_id, after_seq=0) == []

            await mgr_a.kill(chat_id, reason="test_done")

        asyncio.run(_run())

    def test_non_owner_publishes_to_stream_instead_of_spawning(self, two_gateways, monkeypatch):
        mgr_a, mgr_b = two_gateways

        async def _run():
            _as_gateway(monkeypatch, "gw-a")
            chat_id, _handle_a = await _spawn_owned_session(mgr_a)

            _as_gateway(monkeypatch, "gw-b")
            await mgr_b.send_user_message(chat_id, "from-b")

            # gw-b never spawned a competing runner for this session.
            assert mgr_b._live.get(chat_id) is None
            mgr_b._provider.spawn.assert_not_called()

            entries = inbound.read_new(chat_id, after_seq=0)
            assert [e["text"] for e in entries] == ["from-b"]

            _as_gateway(monkeypatch, "gw-a")
            await mgr_a.kill(chat_id, reason="test_done")

        asyncio.run(_run())

    def test_message_published_on_gateway_b_reaches_owner_a_runner_once(self, two_gateways, monkeypatch):
        """The headline scenario: a message published on gateway B while A
        owns the session reaches A's runner, exactly once."""
        mgr_a, mgr_b = two_gateways

        async def _run():
            _as_gateway(monkeypatch, "gw-a")
            chat_id, handle_a = await _spawn_owned_session(mgr_a)

            _as_gateway(monkeypatch, "gw-b")
            await mgr_b.send_user_message(chat_id, "hi-from-b")

            await _wait_until(lambda: len(_stdin_texts(handle_a)) >= 1)
            assert _stdin_texts(handle_a) == ["hi-from-b"]

            # Give the consumer a few more ticks to prove it doesn't
            # redeliver the same already-consumed entry.
            await asyncio.sleep(0.2)
            assert _stdin_texts(handle_a) == ["hi-from-b"]

            _as_gateway(monkeypatch, "gw-a")
            await mgr_a.kill(chat_id, reason="test_done")

        asyncio.run(_run())

    def test_ordering_preserved_across_multiple_forwarded_messages(self, two_gateways, monkeypatch):
        mgr_a, mgr_b = two_gateways

        async def _run():
            _as_gateway(monkeypatch, "gw-a")
            chat_id, handle_a = await _spawn_owned_session(mgr_a)

            _as_gateway(monkeypatch, "gw-b")
            await mgr_b.send_user_message(chat_id, "one")
            await mgr_b.send_user_message(chat_id, "two")
            await mgr_b.send_user_message(chat_id, "three")

            await _wait_until(lambda: len(_stdin_texts(handle_a)) >= 3)
            assert _stdin_texts(handle_a) == ["one", "two", "three"]

            _as_gateway(monkeypatch, "gw-a")
            await mgr_a.kill(chat_id, reason="test_done")

        asyncio.run(_run())

    def test_dedup_on_redelivery(self, two_gateways, monkeypatch):
        """An at-least-once redelivery of an already-consumed inbound seq
        (e.g. a retried publish that actually landed) must not be applied
        to the runner twice."""
        mgr_a, mgr_b = two_gateways

        async def _run():
            _as_gateway(monkeypatch, "gw-a")
            chat_id, handle_a = await _spawn_owned_session(mgr_a)

            _as_gateway(monkeypatch, "gw-b")
            await mgr_b.send_user_message(chat_id, "once-only")

            await _wait_until(lambda: len(_stdin_texts(handle_a)) >= 1)
            assert _stdin_texts(handle_a) == ["once-only"]

            # Simulate a redelivery of the same (already-consumed) entry.
            coordination().stream_append(
                inbound.stream_key(chat_id),
                {"seq": 1, "text": "once-only"},
                maxlen=inbound.STREAM_MAXLEN,
            )
            await asyncio.sleep(0.3)  # a few consumer poll ticks

            assert _stdin_texts(handle_a) == ["once-only"]  # still exactly one delivery

            _as_gateway(monkeypatch, "gw-a")
            await mgr_a.kill(chat_id, reason="test_done")

        asyncio.run(_run())

    def test_memory_mode_direct_path_is_unchanged(self, two_gateways, monkeypatch):
        """Single-gateway (memory backend) sanity: owner_of can never
        differ from this_gateway_id under the memory backend, so a lone
        manager's send_user_message always takes the direct path —
        unchanged behavior from before this task."""
        mgr_a, _mgr_b = two_gateways

        async def _run():
            _as_gateway(monkeypatch, "solo-gw")
            chat_id, handle = await _spawn_owned_session(mgr_a)

            await mgr_a.send_user_message(chat_id, "solo message")

            assert _stdin_texts(handle) == ["solo message"]
            assert inbound.read_new(chat_id, after_seq=0) == []

            await mgr_a.kill(chat_id, reason="test_done")

        asyncio.run(_run())

    def test_coordination_unavailable_on_forward_raises_clean_error(self, two_gateways, monkeypatch):
        """A coordination-backend blip in the publish path must surface as
        a clean, specific InboundPublishFailed — not a crash, and not a
        silently dropped message."""
        mgr_a, mgr_b = two_gateways

        async def _run():
            _as_gateway(monkeypatch, "gw-a")
            chat_id, _handle_a = await _spawn_owned_session(mgr_a)

            _as_gateway(monkeypatch, "gw-b")

            async def _boom(*_a, **_k):
                raise inbound.InboundPublishFailed("boom")

            monkeypatch.setattr(inbound, "publish_inbound", _boom)
            with pytest.raises(inbound.InboundPublishFailed):
                await mgr_b.send_user_message(chat_id, "will not land")

            _as_gateway(monkeypatch, "gw-a")
            await mgr_a.kill(chat_id, reason="test_done")

        asyncio.run(_run())

    def test_inbound_consumer_task_started_and_cancelled_on_kill(self, two_gateways, monkeypatch):
        mgr_a, _mgr_b = two_gateways

        async def _run():
            _as_gateway(monkeypatch, "gw-a")
            chat_id, _handle = await _spawn_owned_session(mgr_a)

            task = mgr_a._live[chat_id].inbound_task
            assert task is not None
            assert not task.done()

            await mgr_a.kill(chat_id, reason="test_done")
            await asyncio.sleep(0.05)
            assert task.cancelled() or task.done()

        asyncio.run(_run())
