"""ChatManager tests — Task 5.1: create_session (+ skeletons for 5.2).

Uses asyncio.run() per the project convention (no pytest-asyncio required).
See tests/test_chat_subprocess_provider.py for precedent.
"""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import duckdb
import pytest

from src.db import _ensure_schema

from app.chat.config import ChatConfig
from app.chat.manager import ChatManager, ConcurrencyCapHit
from app.chat.persistence import ChatRepository
from app.chat.types import Surface
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
# Task 5.2 skeletons — skipped until attach is implemented
# ---------------------------------------------------------------------------

@pytest.mark.skip("attach implemented in 5.2")
def test_concurrency_cap_enforced(manager: ChatManager):
    async def _run():
        await manager.create_session(user_email="u@x", surface=Surface.WEB)
        await manager.attach(...)  # placeholder — real attach in Task 5.2

    asyncio.run(_run())


@pytest.mark.skip("attach implemented in 5.2")
def test_attach_pumps_tokens_to_ws(manager: ChatManager):
    pass


@pytest.mark.skip("attach implemented in 5.2")
def test_send_writes_to_stdin(manager: ChatManager):
    pass


@pytest.mark.skip("attach implemented in 5.2")
def test_cancel_emits_synthetic_tool_result(manager: ChatManager):
    pass


@pytest.mark.skip("attach implemented in 5.2")
def test_crash_respawns_with_notice(manager: ChatManager):
    pass
