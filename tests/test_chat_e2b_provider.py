"""E2BProvider unit tests — mock the e2b SDK at the import boundary.

Per Q7 (owner decision), there's no MockE2BProvider class. Unit tests
mock `app.chat.e2b_provider.AsyncSandbox` directly so we exercise the
real provider code with a fake SDK underneath.

Real-SDK end-to-end coverage lives in `tests/e2e/test_e2b_smoke.py`
(opt-in via `AGNES_E2E_E2B=1` env).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.chat.e2b_provider import E2BProvider, E2BSandboxHandle


def _make_fake_sandbox():
    """Build a fake AsyncSandbox with the SDK 1.x surface the provider uses."""
    sb = MagicMock()
    sb.sandbox_id = "sbx_fake_123"
    sb.kill = AsyncMock(return_value=True)
    sb.set_timeout = AsyncMock()
    # files API
    sb.files = MagicMock()
    sb.files.write = AsyncMock()
    sb.files.read = AsyncMock(return_value="")
    sb.files.list = AsyncMock(return_value=[])
    sb.files.make_dir = AsyncMock(return_value=True)
    # commands API
    sb.commands = MagicMock()
    sb.commands.send_stdin = AsyncMock()
    sb.commands.kill = AsyncMock(return_value=True)
    return sb


def _make_fake_handle(pid: int = 4242):
    h = MagicMock()
    h.pid = pid
    h.kill = AsyncMock(return_value=True)
    h.disconnect = AsyncMock()
    # `wait()` returns a result object with exit_code
    res = MagicMock()
    res.exit_code = 0
    h.wait = AsyncMock(return_value=res)
    return h


def test_provider_spawns_sandbox_and_returns_handle(tmp_path: Path):
    """E2BProvider.spawn → AsyncSandbox.create + commands.run(background=True)."""

    async def _run():
        fake_sb = _make_fake_sandbox()
        fake_handle = _make_fake_handle(pid=1234)
        # commands.run returns the AsyncCommandHandle when background=True
        fake_sb.commands.run = AsyncMock(return_value=fake_handle)

        with patch("app.chat.e2b_provider.AsyncSandbox") as MockSandbox:
            MockSandbox.create = AsyncMock(return_value=fake_sb)

            prov = E2BProvider(
                api_key="sk-e2b-test",
                template_id="agnes-chat",
                sandbox_timeout_seconds=1800,
            )
            handle = await prov.spawn(
                workdir=tmp_path,
                env={"AGNES_TOKEN": "t", "PATH": "/usr/bin"},
                argv=["python3", "-m", "app.chat.runner", "--session-id", "abc"],
            )

            # Sandbox was created with the template + api key + env
            MockSandbox.create.assert_called_once()
            call_kwargs = MockSandbox.create.call_args.kwargs
            assert call_kwargs["template"] == "agnes-chat"
            assert call_kwargs["api_key"] == "sk-e2b-test"
            assert call_kwargs["envs"]["AGNES_TOKEN"] == "t"
            assert call_kwargs["timeout"] == 1800
            # Q4: default open egress
            assert call_kwargs.get("allow_internet_access", True) is True

            # commands.run was called with background=True and the joined argv
            fake_sb.commands.run.assert_called_once()
            run_kwargs = fake_sb.commands.run.call_args.kwargs
            assert run_kwargs["background"] is True
            assert run_kwargs["cwd"] == "/work"
            assert "on_stdout" in run_kwargs and "on_stderr" in run_kwargs

            # Handle exposes the standard SandboxHandle interface
            assert handle.pid == 1234
            assert handle.stdout is not None
            assert handle.stderr is not None
            assert handle.stdin is not None

    asyncio.run(_run())


def test_provider_refuses_when_api_key_missing(tmp_path: Path):
    """spawn() raises if api_key is empty (mirrors Anthropic-gate behavior)."""

    async def _run():
        prov = E2BProvider(api_key="", template_id="agnes-chat")
        with pytest.raises(RuntimeError, match="E2B_API_KEY"):
            await prov.spawn(workdir=tmp_path, env={}, argv=["true"])

    asyncio.run(_run())


def test_provider_refuses_when_template_id_missing(tmp_path: Path):
    """spawn() raises if template_id is empty."""

    async def _run():
        prov = E2BProvider(api_key="sk-test", template_id="")
        with pytest.raises(RuntimeError, match="e2b_template_id"):
            await prov.spawn(workdir=tmp_path, env={}, argv=["true"])

    asyncio.run(_run())


def test_handle_stdout_relays_callback_data():
    """Bytes pushed via the on_stdout callback are readable via handle.stdout."""

    async def _run():
        fake_sb = _make_fake_sandbox()
        fake_handle = _make_fake_handle()
        # Capture the on_stdout callback registered by the provider
        captured_cb = {}

        async def _fake_run(cmd, **kwargs):
            captured_cb["on_stdout"] = kwargs["on_stdout"]
            captured_cb["on_stderr"] = kwargs["on_stderr"]
            return fake_handle

        fake_sb.commands.run = _fake_run

        with patch("app.chat.e2b_provider.AsyncSandbox") as MockSandbox:
            MockSandbox.create = AsyncMock(return_value=fake_sb)
            prov = E2BProvider(api_key="k", template_id="t")
            handle = await prov.spawn(
                workdir=Path("/tmp"), env={}, argv=["true"],
            )

            # Simulate the SDK pushing two stdout lines.
            cb = captured_cb["on_stdout"]
            # The SDK may call sync or async — provider must support both.
            res = cb('{"type":"ready"}\n')
            if asyncio.iscoroutine(res):
                await res
            res2 = cb('{"type":"assistant_message","content":"hi"}\n')
            if asyncio.iscoroutine(res2):
                await res2

            line1 = await handle.stdout.readline()
            line2 = await handle.stdout.readline()
            assert line1 == b'{"type":"ready"}\n'
            assert line2 == b'{"type":"assistant_message","content":"hi"}\n'

    asyncio.run(_run())


def test_handle_stdin_routes_via_send_stdin():
    """handle.stdin.write + drain → sandbox.commands.send_stdin(pid, str)."""

    async def _run():
        fake_sb = _make_fake_sandbox()
        fake_handle = _make_fake_handle(pid=999)
        fake_sb.commands.run = AsyncMock(return_value=fake_handle)

        with patch("app.chat.e2b_provider.AsyncSandbox") as MockSandbox:
            MockSandbox.create = AsyncMock(return_value=fake_sb)
            prov = E2BProvider(api_key="k", template_id="t")
            handle = await prov.spawn(
                workdir=Path("/tmp"), env={}, argv=["true"],
            )

            handle.stdin.write(b'{"type":"user_msg","text":"hello"}\n')
            await handle.stdin.drain()

            fake_sb.commands.send_stdin.assert_awaited_once()
            args, _ = fake_sb.commands.send_stdin.call_args
            assert args[0] == 999  # pid
            assert "hello" in args[1]

    asyncio.run(_run())


def test_handle_kill_kills_command_then_sandbox():
    """kill() sends SIGTERM via send_stdin/EOF, awaits grace, then sandbox.kill()."""

    async def _run():
        fake_sb = _make_fake_sandbox()
        fake_handle = _make_fake_handle()
        fake_sb.commands.run = AsyncMock(return_value=fake_handle)

        with patch("app.chat.e2b_provider.AsyncSandbox") as MockSandbox:
            MockSandbox.create = AsyncMock(return_value=fake_sb)
            prov = E2BProvider(api_key="k", template_id="t")
            handle = await prov.spawn(
                workdir=Path("/tmp"), env={}, argv=["true"],
            )

            await handle.kill(grace_sec=0.05)

            # The handle-level kill is called (E2B sends SIGKILL to the cmd)
            fake_handle.kill.assert_awaited()
            # The whole sandbox is also killed (we don't reuse sandboxes)
            fake_sb.kill.assert_awaited()

    asyncio.run(_run())


def test_handle_wait_returns_exit_code():
    """handle.wait() returns the CommandResult.exit_code as an int."""

    async def _run():
        fake_sb = _make_fake_sandbox()
        fake_handle = _make_fake_handle()
        res = MagicMock()
        res.exit_code = 7
        fake_handle.wait = AsyncMock(return_value=res)
        fake_sb.commands.run = AsyncMock(return_value=fake_handle)

        with patch("app.chat.e2b_provider.AsyncSandbox") as MockSandbox:
            MockSandbox.create = AsyncMock(return_value=fake_sb)
            prov = E2BProvider(api_key="k", template_id="t")
            handle = await prov.spawn(
                workdir=Path("/tmp"), env={}, argv=["true"],
            )

            rc = await handle.wait()
            assert rc == 7

    asyncio.run(_run())


def test_provider_uploads_runner_module(tmp_path: Path):
    """Provider uploads the runner module text into /work/runner.py before run."""

    async def _run():
        fake_sb = _make_fake_sandbox()
        fake_handle = _make_fake_handle()
        fake_sb.commands.run = AsyncMock(return_value=fake_handle)

        with patch("app.chat.e2b_provider.AsyncSandbox") as MockSandbox:
            MockSandbox.create = AsyncMock(return_value=fake_sb)
            prov = E2BProvider(api_key="k", template_id="t", upload_runner=True)
            await prov.spawn(
                workdir=tmp_path, env={}, argv=["python3", "/work/runner.py"],
            )

            # files.write was called at least once — once for the runner
            assert fake_sb.files.write.await_count >= 1
            paths_written = [c.args[0] for c in fake_sb.files.write.await_args_list]
            assert any("runner.py" in p for p in paths_written)

    asyncio.run(_run())


def test_handle_implements_sandbox_handle_protocol():
    """E2BSandboxHandle is structurally compatible with SandboxHandle Protocol."""

    async def _run():
        fake_sb = _make_fake_sandbox()
        fake_handle = _make_fake_handle()
        fake_sb.commands.run = AsyncMock(return_value=fake_handle)

        with patch("app.chat.e2b_provider.AsyncSandbox") as MockSandbox:
            MockSandbox.create = AsyncMock(return_value=fake_sb)
            prov = E2BProvider(api_key="k", template_id="t")
            handle = await prov.spawn(
                workdir=Path("/tmp"), env={}, argv=["true"],
            )

        # Protocol attributes
        assert hasattr(handle, "pid")
        assert hasattr(handle, "stdin")
        assert hasattr(handle, "stdout")
        assert hasattr(handle, "stderr")
        assert hasattr(handle, "wait")
        assert hasattr(handle, "kill")
        assert isinstance(handle, E2BSandboxHandle)

    asyncio.run(_run())
