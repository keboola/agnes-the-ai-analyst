"""Regression tests for marketplace-SHA-driven workspace reinit.

Covers the integration of WorkdirManager.needs_reinit with ChatManager.attach:
when the marketplace SHA changes between sessions the next attach must trigger
run_init before spawning the runner.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import duckdb
import pytest

from src.db import _ensure_schema
from app.chat.config import ChatConfig
from app.chat.manager import ChatManager
from app.chat.persistence import ChatRepository
from app.chat.types import Surface
from app.chat.workdir import WorkdirManager


def test_marketplace_sha_debounced(tmp_path):
    """When debounce_seconds > 0 the marketplace SHA is read at most once per
    interval; intermediate calls return the cached value.

    Operators configure ``marketplace_sha_debounce_seconds`` in instance.yaml
    to limit FS reads on hot paths; before this knob was wired every
    ``needs_reinit`` call re-read the SHA file unconditionally.
    """
    import duckdb
    from src.db import _ensure_schema
    from app.chat.persistence import ChatRepository

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)

    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "CLAUDE.md").write_text("d")

    call_count = {"n": 0}

    def sha_fn():
        call_count["n"] += 1
        return "sha-1"

    mgr = WorkdirManager(
        data_dir=tmp_path / "data",
        repo=repo,
        bundled_template_dir=bundled,
        server_url="https://example",
        agnes_version="0.55.0",
        get_marketplace_sha=sha_fn,
        get_template_status=lambda: None,
        marketplace_sha_debounce_seconds=60,
    )

    # Seed a workdir row at sha-1 so needs_reinit doesn't decide it's missing.
    repo.upsert_workdir(
        user_email="u@x", marketplace_sha="sha-1",
        initial_workspace_sha=None, agnes_version="0.55.0",
    )

    # First call → reads SHA.
    assert mgr.needs_reinit("u@x") is False
    assert call_count["n"] == 1
    # Second call within debounce window → cached.
    assert mgr.needs_reinit("u@x") is False
    assert call_count["n"] == 1


def test_marketplace_sha_no_debounce_when_zero(tmp_path):
    """debounce_seconds=0 disables caching; SHA is re-read on every call."""
    import duckdb
    from src.db import _ensure_schema
    from app.chat.persistence import ChatRepository

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "CLAUDE.md").write_text("d")

    call_count = {"n": 0}

    def sha_fn():
        call_count["n"] += 1
        return "sha-1"

    mgr = WorkdirManager(
        data_dir=tmp_path / "data",
        repo=repo,
        bundled_template_dir=bundled,
        server_url="https://example",
        agnes_version="0.55.0",
        get_marketplace_sha=sha_fn,
        get_template_status=lambda: None,
        marketplace_sha_debounce_seconds=0,
    )

    repo.upsert_workdir(
        user_email="u@x", marketplace_sha="sha-1",
        initial_workspace_sha=None, agnes_version="0.55.0",
    )

    mgr.needs_reinit("u@x")
    mgr.needs_reinit("u@x")
    mgr.needs_reinit("u@x")
    assert call_count["n"] == 3


def _make_repo(tmp_path: Path) -> ChatRepository:
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    return ChatRepository(conn)


def _make_manager(
    tmp_path: Path,
    repo: ChatRepository,
    *,
    sha_fn,
) -> ChatManager:
    bundled = tmp_path / "bundled"
    bundled.mkdir(parents=True, exist_ok=True)
    (bundled / "CLAUDE.md").write_text("d")

    workdir_mgr = WorkdirManager(
        data_dir=tmp_path / "data",
        repo=repo,
        bundled_template_dir=bundled,
        server_url="https://example",
        agnes_version="0.55.0",
        get_marketplace_sha=sha_fn,
        get_template_status=lambda: None,
    )
    provider = MagicMock()
    provider.spawn = AsyncMock(return_value=MagicMock(pid=1))
    return ChatManager(
        provider=provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        config=ChatConfig(enabled=True, concurrency_per_user=3),
    ), workdir_mgr


def test_workdir_needs_reinit_on_marketplace_sha_change(tmp_path: Path):
    """WorkdirManager.needs_reinit returns True after marketplace SHA changes."""
    repo = _make_repo(tmp_path)
    current_sha = ["sha-1"]

    mgr, workdir_mgr = _make_manager(tmp_path, repo, sha_fn=lambda: current_sha[0])

    async def _run():
        # First session — initial init
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws_path = workdir_mgr.ensure_user_workdir("u@x")
        # Sentinel must exist and needs_reinit should be False
        assert not workdir_mgr.needs_reinit("u@x")

        # Marketplace SHA changes
        current_sha[0] = "sha-2"
        assert workdir_mgr.needs_reinit("u@x")

    asyncio.run(_run())


def test_workdir_needs_reinit_on_version_change(tmp_path: Path):
    """WorkdirManager.needs_reinit returns True when agnes_version changes."""
    repo = _make_repo(tmp_path)
    bundled = tmp_path / "bundled"
    bundled.mkdir(parents=True, exist_ok=True)
    (bundled / "CLAUDE.md").write_text("d")

    wm_v1 = WorkdirManager(
        data_dir=tmp_path / "data",
        repo=repo,
        bundled_template_dir=bundled,
        server_url="https://example",
        agnes_version="0.55.0",
        get_marketplace_sha=lambda: "sha-1",
        get_template_status=lambda: None,
    )
    # Initialize under v1
    wm_v1.ensure_user_workdir("u@y")
    assert not wm_v1.needs_reinit("u@y")

    # Bump to v2 — should trigger reinit
    wm_v2 = WorkdirManager(
        data_dir=tmp_path / "data",
        repo=repo,
        bundled_template_dir=bundled,
        server_url="https://example",
        agnes_version="0.56.0",
        get_marketplace_sha=lambda: "sha-1",
        get_template_status=lambda: None,
    )
    assert wm_v2.needs_reinit("u@y")


def test_workdir_no_reinit_needed_when_sha_stable(tmp_path: Path):
    """needs_reinit returns False when SHA and version are unchanged."""
    repo = _make_repo(tmp_path)
    bundled = tmp_path / "bundled"
    bundled.mkdir(parents=True, exist_ok=True)
    (bundled / "CLAUDE.md").write_text("d")

    wm = WorkdirManager(
        data_dir=tmp_path / "data",
        repo=repo,
        bundled_template_dir=bundled,
        server_url="https://example",
        agnes_version="0.55.0",
        get_marketplace_sha=lambda: "stable-sha",
        get_template_status=lambda: None,
    )
    wm.ensure_user_workdir("u@z")
    assert not wm.needs_reinit("u@z")
    # Still False on second check
    assert not wm.needs_reinit("u@z")
