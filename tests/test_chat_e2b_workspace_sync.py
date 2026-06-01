"""E2B workspace sync layer tests.

Per Q1 (owner-signed): v1 pushes the entire per-user workspace into the
sandbox at spawn (rsync-style — every file). Cap at 100 MB. Symlinks
(.claude/skills, CLAUDE.md, etc.) are dereferenced so the sandbox sees
real files.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.chat.e2b_workspace_sync import (
    SANDBOX_WHEEL_PATH,
    WorkspaceTooLarge,
    download_workspace,
    upload_agnes_wheel,
    upload_workspace,
)


def _make_fake_sandbox():
    sb = MagicMock()
    sb.files = MagicMock()
    sb.files.write = AsyncMock()
    sb.files.make_dir = AsyncMock(return_value=True)
    sb.files.list = AsyncMock(return_value=[])
    sb.files.read = AsyncMock(return_value=b"")
    return sb


def test_upload_walks_workspace_tree(tmp_path: Path):
    """Every regular file in the workspace tree lands as a files.write call."""

    async def _run():
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "CLAUDE.md").write_text("# greetings")
        (ws / ".claude").mkdir()
        (ws / ".claude" / "hooks").mkdir()
        (ws / ".claude" / "hooks" / "pre_tool_use.py").write_text("print('ok')")

        sb = _make_fake_sandbox()
        await upload_workspace(sb, ws, max_bytes=10 * 1024 * 1024)

        # Every file should be written under /work/
        written_paths = [c.args[0] for c in sb.files.write.await_args_list]
        assert "/work/CLAUDE.md" in written_paths
        assert "/work/.claude/hooks/pre_tool_use.py" in written_paths

    asyncio.run(_run())


def test_upload_dereferences_symlinks(tmp_path: Path):
    """Symlinks are uploaded as the file they point at (not as links)."""

    async def _run():
        # User workspace lives outside the session dir; the session dir
        # contains a symlink at .claude pointing at the workspace .claude.
        user_ws = tmp_path / "user_workspace"
        user_ws.mkdir()
        (user_ws / ".claude").mkdir()
        (user_ws / ".claude" / "skills").mkdir()
        (user_ws / ".claude" / "skills" / "x.md").write_text("skill body")

        session = tmp_path / "session-abc"
        session.mkdir()
        # Symlink the session-level .claude → user workspace .claude
        (session / ".claude").symlink_to(user_ws / ".claude")
        (session / "work").mkdir()

        sb = _make_fake_sandbox()
        await upload_workspace(sb, session, max_bytes=10 * 1024 * 1024)

        written_paths = [c.args[0] for c in sb.files.write.await_args_list]
        written_bodies = {
            c.args[0]: c.args[1] for c in sb.files.write.await_args_list
        }
        # File reached the sandbox via the symlink
        assert "/work/.claude/skills/x.md" in written_paths
        body = written_bodies["/work/.claude/skills/x.md"]
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        assert "skill body" in body

    asyncio.run(_run())


def test_upload_refuses_oversize_workspace(tmp_path: Path):
    """If summed file bytes exceed max_bytes, raise — don't half-upload."""

    async def _run():
        ws = tmp_path / "ws"
        ws.mkdir()
        # 2 MB of data; cap at 1 MB
        (ws / "big.bin").write_bytes(b"x" * (2 * 1024 * 1024))

        sb = _make_fake_sandbox()
        with pytest.raises(WorkspaceTooLarge):
            await upload_workspace(sb, ws, max_bytes=1 * 1024 * 1024)

        # No files should have been pushed
        assert sb.files.write.await_count == 0

    asyncio.run(_run())


def test_upload_skips_hidden_runtime_dirs(tmp_path: Path):
    """`__pycache__`, `.git`, `.venv` etc. don't go to the sandbox."""

    async def _run():
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "good.txt").write_text("ok")
        (ws / "__pycache__").mkdir()
        (ws / "__pycache__" / "foo.pyc").write_bytes(b"\x00\x01")
        (ws / ".git").mkdir()
        (ws / ".git" / "HEAD").write_text("ref: refs/heads/main")
        (ws / ".venv").mkdir()
        (ws / ".venv" / "marker").write_text("x")

        sb = _make_fake_sandbox()
        await upload_workspace(sb, ws, max_bytes=10 * 1024 * 1024)

        written = [c.args[0] for c in sb.files.write.await_args_list]
        assert "/work/good.txt" in written
        assert not any("__pycache__" in p for p in written)
        assert not any(".git/" in p for p in written)
        assert not any(".venv/" in p for p in written)

    asyncio.run(_run())


def test_upload_handles_empty_workspace(tmp_path: Path):
    """An empty workspace is a no-op, not an error."""

    async def _run():
        ws = tmp_path / "empty"
        ws.mkdir()
        sb = _make_fake_sandbox()
        await upload_workspace(sb, ws, max_bytes=1024 * 1024)
        assert sb.files.write.await_count == 0

    asyncio.run(_run())


def test_download_writes_files_back_locally(tmp_path: Path):
    """download_workspace lists /work, reads each file, writes to local path."""

    async def _run():
        # Fake the SDK's list/read pair.  list returns EntryInfo-like
        # objects with .name, .type, .path (matching the e2b SDK shape).
        sb = _make_fake_sandbox()

        def _entry(name, etype, parent=""):
            e = MagicMock()
            e.name = name
            e.type = etype
            e.path = f"{parent}/{name}" if parent else f"/work/{name}"
            return e

        # /work contains: greeting.txt (FILE) and notes/ (DIR with one file)
        listings = {
            "/work": [
                _entry("greeting.txt", "FILE"),
                _entry("notes", "DIR"),
            ],
            "/work/notes": [
                _entry("a.md", "FILE", parent="/work/notes"),
            ],
        }
        contents = {
            "/work/greeting.txt": b"hello",
            "/work/notes/a.md": b"# a",
        }

        async def fake_list(p, **kw):
            return listings.get(p, [])

        async def fake_read(p, **kw):
            return contents.get(p, b"")

        sb.files.list.side_effect = fake_list
        sb.files.read.side_effect = fake_read

        target = tmp_path / "back"
        await download_workspace(sb, target)

        assert (target / "greeting.txt").read_text() == "hello"
        assert (target / "notes" / "a.md").read_text() == "# a"

    asyncio.run(_run())


def test_upload_agnes_wheel_writes_wheel_to_sandbox(tmp_path: Path, monkeypatch):
    """The server's pre-built wheel is read and written to /work/agnes.whl."""

    async def _run():
        wheel = tmp_path / "agnes_the_ai_analyst-0.55.25-py3-none-any.whl"
        wheel.write_bytes(b"PK\x03\x04 fake wheel bytes")

        # Stub the shared wheel-discovery helper to return our fake wheel.
        monkeypatch.setattr(
            "app.api.cli_artifacts._find_wheel", lambda: wheel
        )

        sb = _make_fake_sandbox()
        n = await upload_agnes_wheel(sb)

        assert n == len(b"PK\x03\x04 fake wheel bytes")
        written = {c.args[0]: c.args[1] for c in sb.files.write.await_args_list}
        assert SANDBOX_WHEEL_PATH in written
        assert written[SANDBOX_WHEEL_PATH] == b"PK\x03\x04 fake wheel bytes"

    asyncio.run(_run())


def test_upload_agnes_wheel_noop_when_no_wheel(monkeypatch):
    """A dev image without a built wheel is a no-op (0 bytes), not an error."""

    async def _run():
        monkeypatch.setattr("app.api.cli_artifacts._find_wheel", lambda: None)
        sb = _make_fake_sandbox()
        n = await upload_agnes_wheel(sb)
        assert n == 0
        assert sb.files.write.await_count == 0

    asyncio.run(_run())


def test_workspace_too_large_carries_byte_count(tmp_path: Path):
    """Exception body includes the actual measured size for the error frame."""

    async def _run():
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "a").write_bytes(b"x" * 100)
        (ws / "b").write_bytes(b"y" * 200)
        sb = _make_fake_sandbox()
        with pytest.raises(WorkspaceTooLarge) as ei:
            await upload_workspace(sb, ws, max_bytes=50)
        msg = str(ei.value)
        assert "300" in msg or "100" in msg  # at least the running tally

    asyncio.run(_run())
