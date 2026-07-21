"""E2B workspace sync layer tests.

Per Q1 (owner-signed): v1 pushes the entire per-user workspace into the
sandbox at spawn (rsync-style — every file). Cap at 100 MB. Symlinks
(.claude/skills, CLAUDE.md, etc.) are dereferenced so the sandbox sees
real files.

Transport: one gzipped tarball + in-sandbox ``tar -x`` (single round-trip);
per-file ``files.write`` remains as a fallback when the tar step fails.
"""

from __future__ import annotations

import asyncio
import io
import tarfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.chat.e2b_workspace_sync import (
    SANDBOX_WHEEL_DIR,
    SANDBOX_WHEEL_READY,
    SANDBOX_WORKSPACE_READY,
    SANDBOX_WORKSPACE_TARBALL,
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
    sb.commands = MagicMock()
    sb.commands.run = AsyncMock()
    return sb


def _written(sb) -> dict[str, bytes]:
    """Map of files.write calls: sandbox path → payload."""
    return {c.args[0]: c.args[1] for c in sb.files.write.await_args_list}


def _tar_members(sb) -> dict[str, tarfile.TarInfo]:
    """Extract the uploaded workspace tarball's members keyed by name."""
    blob = _written(sb)[SANDBOX_WORKSPACE_TARBALL]
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        return {m.name: m for m in tar.getmembers()}


def _tar_body(sb, name: str) -> bytes:
    blob = _written(sb)[SANDBOX_WORKSPACE_TARBALL]
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        fh = tar.extractfile(name)
        assert fh is not None
        return fh.read()


def test_upload_packs_workspace_tree_into_one_tarball(tmp_path: Path):
    """The whole tree travels as ONE files.write (the tarball) + one tar -x
    command — not one round-trip per file."""

    async def _run():
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "CLAUDE.md").write_text("# greetings")
        (ws / ".claude").mkdir()
        (ws / ".claude" / "hooks").mkdir()
        (ws / ".claude" / "hooks" / "pre_tool_use.py").write_text("print('ok')")

        sb = _make_fake_sandbox()
        await upload_workspace(sb, ws, max_bytes=10 * 1024 * 1024)

        members = _tar_members(sb)
        assert "CLAUDE.md" in members
        assert ".claude/hooks/pre_tool_use.py" in members
        # Exactly two writes: the tarball + the ready sentinel. No per-file
        # round-trips on the happy path.
        assert set(_written(sb)) == {SANDBOX_WORKSPACE_TARBALL, SANDBOX_WORKSPACE_READY}
        # Extraction command targets /work and cleans the archive up.
        cmd = sb.commands.run.await_args_list[0].args[0]
        assert SANDBOX_WORKSPACE_TARBALL in cmd
        assert "-C /work" in cmd

    asyncio.run(_run())


def test_upload_writes_ready_sentinel_last(tmp_path: Path):
    """SANDBOX_WORKSPACE_READY lands after the tree is in place — the runner
    gates the agent-CLI spawn on it."""

    async def _run():
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "a.txt").write_text("a")

        sb = _make_fake_sandbox()
        await upload_workspace(sb, ws, max_bytes=1024 * 1024)

        paths = [c.args[0] for c in sb.files.write.await_args_list]
        assert paths[-1] == SANDBOX_WORKSPACE_READY

    asyncio.run(_run())


def test_upload_falls_back_to_per_file_writes(tmp_path: Path):
    """A failed in-sandbox extraction degrades to the legacy per-file loop —
    the workspace still arrives, and the ready sentinel is still written."""

    async def _run():
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "CLAUDE.md").write_text("# greetings")

        sb = _make_fake_sandbox()
        sb.commands.run.side_effect = RuntimeError("tar: not found")
        await upload_workspace(sb, ws, max_bytes=1024 * 1024)

        written = _written(sb)
        assert written["/work/CLAUDE.md"] == b"# greetings"
        assert SANDBOX_WORKSPACE_READY in written

    asyncio.run(_run())


def test_upload_preserves_exec_bit(tmp_path: Path):
    """Hook/scripts permissions survive the tar transport."""

    async def _run():
        ws = tmp_path / "workspace"
        ws.mkdir()
        script = ws / "scripts_run.sh"
        script.write_text("#!/bin/sh\necho hi\n")
        script.chmod(0o755)

        sb = _make_fake_sandbox()
        await upload_workspace(sb, ws, max_bytes=1024 * 1024)

        member = _tar_members(sb)["scripts_run.sh"]
        assert member.mode & 0o111

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

        assert ".claude/skills/x.md" in _tar_members(sb)
        assert b"skill body" in _tar_body(sb, ".claude/skills/x.md")

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

        # No files should have been pushed — not even the ready sentinel
        # (the caller tears the sandbox down on this exception).
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

        members = _tar_members(sb)
        assert "good.txt" in members
        assert not any("__pycache__" in n for n in members)
        assert not any(".git/" in n for n in members)
        assert not any(".venv/" in n for n in members)

    asyncio.run(_run())


def test_upload_handles_empty_workspace(tmp_path: Path):
    """An empty workspace is a no-op upload — but the ready sentinel is
    still written so the runner's bounded wait terminates promptly."""

    async def _run():
        ws = tmp_path / "empty"
        ws.mkdir()
        sb = _make_fake_sandbox()
        total = await upload_workspace(sb, ws, max_bytes=1024 * 1024)
        assert total == 0
        assert [c.args[0] for c in sb.files.write.await_args_list] == [SANDBOX_WORKSPACE_READY]
        sb.commands.run.assert_not_awaited()

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


def test_upload_agnes_wheel_preserves_pep427_filename(tmp_path: Path, monkeypatch):
    """The wheel is staged under its original PEP 427 name (pip rejects a
    renamed wheel) in the dedicated dir outside /work (so it isn't synced back)."""

    async def _run():
        wheel = tmp_path / "agnes_the_ai_analyst-0.55.25-py3-none-any.whl"
        wheel.write_bytes(b"PK\x03\x04 fake wheel bytes")

        # Stub the shared wheel-discovery helper to return our fake wheel.
        monkeypatch.setattr("app.api.cli_artifacts._find_wheel", lambda: wheel)

        sb = _make_fake_sandbox()
        dest = await upload_agnes_wheel(sb)

        expected = f"{SANDBOX_WHEEL_DIR}/{wheel.name}"
        assert dest == expected
        # Staged outside the synced workspace dir, and never flattened to a
        # version-less name pip would reject.
        assert not dest.startswith("/work")
        assert dest.endswith(".whl") and "0.55.25" in dest
        written = _written(sb)
        assert expected in written
        assert written[expected] == b"PK\x03\x04 fake wheel bytes"
        # Sentinel written so the runner's wait terminates.
        assert SANDBOX_WHEEL_READY in written

    asyncio.run(_run())


def test_upload_agnes_wheel_noop_when_no_wheel(monkeypatch):
    """A dev image without a built wheel returns None — but still writes the
    sentinel so the runner doesn't block on its bounded wait."""

    async def _run():
        monkeypatch.setattr("app.api.cli_artifacts._find_wheel", lambda: None)
        sb = _make_fake_sandbox()
        dest = await upload_agnes_wheel(sb)
        assert dest is None
        written = [c.args[0] for c in sb.files.write.await_args_list]
        assert written == [SANDBOX_WHEEL_READY]

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
